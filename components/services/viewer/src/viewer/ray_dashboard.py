"""Thin proxy + parser around the Ray Dashboard JSON API.

The Ray Dashboard exposes its state at ``http://<head>:8265/api/...``:
  /api/jobs/             list of submitted RayJobs (JobInfo)
  /api/cluster_status    autoscaler view of nodes + resources

We don't expose those directly to the browser (CORS, internal-only port,
schema churn between Ray versions). Instead the FastAPI layer normalizes
both into the small shape ``/batches`` actually wants:

  /api/ray/health        { ok, dashboard_url, error }
  /api/ray/jobs          { ok, jobs: [JobSummary] }
  /api/ray/cluster       { ok, total_resources, used_resources, nodes }

When the dashboard is unreachable we return ``{ "ok": false, ... }`` rather
than 5xx so the UI can render a "Ray not connected" badge.
"""

from __future__ import annotations

import os
import re
from typing import Any

import httpx


_BATCH_RE = re.compile(r"--batch[\s=]+(\S+)")


def _dashboard_url() -> str:
    return os.environ.get("RAY_DASHBOARD_URL", "http://localhost:8265").rstrip("/")


def _http(timeout: float = 3.0) -> httpx.Client:
    return httpx.Client(timeout=timeout)


def _err_payload(exc: Exception) -> dict[str, Any]:
    return {
        "ok": False,
        "dashboard_url": _dashboard_url(),
        "error": f"{type(exc).__name__}: {exc!s}"[:400],
    }


def health() -> dict[str, Any]:
    """Lightweight reachability probe."""
    url = _dashboard_url()
    try:
        with _http() as c:
            resp = c.get(f"{url}/api/version")
            resp.raise_for_status()
            data = resp.json()
        return {"ok": True, "dashboard_url": url, "ray_version": data.get("ray_version")}
    except Exception as e:
        return _err_payload(e)


def _parse_batches(entrypoint: str | None) -> list[str]:
    if not entrypoint:
        return []
    return _BATCH_RE.findall(entrypoint)


def _ms_to_sec(v: Any) -> float | None:  # noqa: ANN401
    """Ray returns timestamps as ms-since-epoch; the UI works in seconds."""
    if v is None:
        return None
    try:
        return float(v) / 1000.0
    except (TypeError, ValueError):
        return None


def _job_summary(raw: dict[str, Any], dashboard_url: str) -> dict[str, Any]:
    """Trim a Ray JobInfo dict down to what the UI actually renders."""
    submission_id = raw.get("submission_id") or raw.get("job_id") or ""
    return {
        "submission_id": submission_id,
        "status": raw.get("status", "UNKNOWN"),  # PENDING|RUNNING|SUCCEEDED|FAILED|STOPPED
        "entrypoint": raw.get("entrypoint"),
        "batches": _parse_batches(raw.get("entrypoint")),
        "start_time": _ms_to_sec(raw.get("start_time")),
        "end_time": _ms_to_sec(raw.get("end_time")),
        "message": (raw.get("message") or "")[:300] or None,
        "error_type": raw.get("error_type"),
        "logs_url": f"{dashboard_url}/#/jobs/{submission_id}" if submission_id else None,
        "metadata": raw.get("metadata") or {},
    }


def list_jobs() -> dict[str, Any]:
    url = _dashboard_url()
    try:
        with _http() as c:
            resp = c.get(f"{url}/api/jobs/")
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return _err_payload(e)

    # Ray returns a list at top level (newer versions) or {jobs: [...]} (older).
    raw_jobs = data.get("jobs") or list(data.values()) if isinstance(data, dict) else data

    jobs = [_job_summary(j, url) for j in raw_jobs if isinstance(j, dict)]
    # Newest first by start_time (None last).
    jobs.sort(key=lambda j: j.get("start_time") or 0, reverse=True)
    return {"ok": True, "dashboard_url": url, "jobs": jobs}


_BYTE_UNITS = {
    "B": 1,
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
    "TB": 1000**4,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
}


def _parse_res_value(s: str) -> float:
    """Parse '0.0', '64.0', '0B', '343.05GiB' into a float (bytes for sizes)."""
    s = s.strip()
    m = re.match(r"^([0-9.]+)([A-Za-z]*)$", s)
    if not m:
        return 0.0
    num = float(m.group(1))
    unit = m.group(2)
    return num * _BYTE_UNITS.get(unit, 1) if unit else num


def _parse_logical(text: str) -> tuple[dict[str, float], dict[str, float]]:
    """Split a multi-line ``nodeLogicalResources`` blob into (used, total) dicts.

    Each line looks like ``0.0/64.0 CPU`` or ``0B/343.05GiB memory`` — the same
    format ``ray status`` prints. The dashboard's own UI consumes this string,
    and parsing it gives us per-node ``used`` numbers that aren't otherwise
    exposed at this nesting level.
    """
    used: dict[str, float] = {}
    total: dict[str, float] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if "/" not in line or " " not in line:
            continue
        ratio, _, name = line.rpartition(" ")
        used_s, _, total_s = ratio.partition("/")
        used[name] = _parse_res_value(used_s)
        total[name] = _parse_res_value(total_s)
    return used, total


_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "x-frame-options",
    "content-security-policy",
}


def proxy(path: str, method: str, query: bytes | str, headers: dict[str, str], body: bytes) -> tuple[bytes, int, dict[str, str]]:
    """Forward an arbitrary path to the Ray Dashboard, same-origin to the browser.

    Used by ``/ray-dashboard/{path:path}`` plus a handful of absolute paths
    Ray's bundled JS hits (``/api/v0/...``, ``/api/jobs``, ``/api/version``,
    ``/api/cluster_status``, ``/logs/...``). Returns ``(body, status, headers)``
    so the FastAPI layer can wrap it in a Response.
    """
    url = _dashboard_url() + "/" + path.lstrip("/")
    fwd = {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP and k.lower() != "host"}
    qs = query.decode() if isinstance(query, bytes) else query
    try:
        with _http(timeout=15.0) as c:
            resp = c.request(method, url, params=qs, headers=fwd, content=body or None)
    except Exception as e:
        return (f"ray dashboard unreachable: {e}".encode(), 502, {"content-type": "text/plain"})
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}
    return (resp.content, resp.status_code, out_headers)


def cluster_status() -> dict[str, Any]:
    """Aggregate cluster + per-node resources.

    Cluster totals come from ``/api/cluster_status`` —
    ``loadMetricsReport.usage`` is already a clean ``{name: [used, total]}``
    dict of floats, so we don't have to parse anything for the headline cards.

    Per-node detail comes from ``/nodes?view=summary``: totals from
    ``raylet.resourcesTotal`` (already floats), used from a parsed
    ``nodeLogicalResources[nodeId]`` string. The earlier implementation read
    ``resources_total`` / ``alive`` at the top level of each summary node, but
    Ray actually nests both under ``raylet`` — so it counted the only node as
    dead and reported 0/0 across the board.
    """
    url = _dashboard_url()
    total = {"CPU": 0.0, "GPU": 0.0, "memory": 0.0}
    used = {"CPU": 0.0, "GPU": 0.0, "memory": 0.0}
    nodes: list[dict[str, Any]] = []

    try:
        with _http() as c:
            cs_resp = c.get(f"{url}/api/cluster_status")
            cs_resp.raise_for_status()
            cs = (cs_resp.json().get("data") or {}).get("clusterStatus", {})
            usage = (cs.get("loadMetricsReport") or {}).get("usage") or {}
            for k in total:
                pair = usage.get(k)
                if isinstance(pair, list) and len(pair) == 2:
                    used[k] = float(pair[0] or 0)
                    total[k] = float(pair[1] or 0)

            try:
                ns_resp = c.get(f"{url}/nodes?view=summary")
                ns_resp.raise_for_status()
                data = ns_resp.json().get("data") or {}
                summary = data.get("summary") or []
                logical = data.get("nodeLogicalResources") or {}
                for n in summary:
                    raylet = n.get("raylet") or {}
                    node_id = raylet.get("nodeId")
                    if not node_id:
                        continue
                    parsed_used, _ = _parse_logical(logical.get(node_id, ""))
                    rtotal = raylet.get("resourcesTotal") or {}
                    nodes.append(
                        {
                            "node_id": node_id,
                            "node_ip": raylet.get("nodeManagerAddress") or n.get("ip"),
                            "alive": raylet.get("state") == "ALIVE",
                            "resources_total": {k: float(rtotal.get(k, 0) or 0) for k in total},
                            "resources_used": {k: parsed_used.get(k, 0.0) for k in total},
                        }
                    )
            except Exception:  # noqa: S110 — per-node detail is best-effort; aggregates above are still valid
                pass
    except Exception as e:
        return _err_payload(e)

    return {
        "ok": True,
        "dashboard_url": url,
        "node_count": len(nodes),
        "alive_count": sum(1 for n in nodes if n.get("alive")),
        "total_resources": total,
        "used_resources": used,
        "nodes": nodes,
    }
