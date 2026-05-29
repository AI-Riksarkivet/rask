"""Thin wrapper around Ray's Job Submission SDK + Dashboard HTTP.

The SDK (`JobSubmissionClient`) is sync and constructs with a connection check.
We cache one instance on `app.state.ray_client` in lifespan and call its
methods from the event loop via `anyio.to_thread.run_sync`.

Two surfaces stay on raw httpx because the SDK doesn't model them:
  - `cluster_status` (uses `/api/cluster_status` + `/nodes`)
  - `proxy` (forwards arbitrary paths for the iframe)

Unreachable-Ray cases return `ok=False` so the UI can render "offline" instead
of bubbling 5xx.
"""

import logging
import re
from http import HTTPStatus

import httpx
import ray
import requests
from anyio import to_thread
from ray.exceptions import AuthenticationError
from ray.job_submission import JobSubmissionClient

from viewer.schemas.ray import ProxyResponse, RayClusterPayload, RayHealth, RayJob, RayJobsPayload, RayNode


log = logging.getLogger(__name__)

# Errors meaning "Ray is unreachable / refused us", raised by the Job SDK at runtime.
# `requests.*` subclass OSError (NOT builtin ConnectionError); the SDK only translates
# its construct-time check to builtin ConnectionError, so live calls can still raise
# requests exceptions directly. AuthenticationError (a RayError) surfaces on 401/403
# from an authenticated cluster. Shared with submission.py + orchestrator/derive.py.
RAY_TRANSIENT_ERRORS = (RuntimeError, ConnectionError, requests.exceptions.RequestException, AuthenticationError)

_BATCH_RE = re.compile(r"--batch[\s=]+(\S+)")
_ERROR_MSG_MAX_LEN = 400  # truncate exception strings so they fit in one log/UI line

# Ray Dashboard JSON keys we read from /api/cluster_status and /nodes responses.
# Grouped here so a Ray-side rename is a one-spot edit.
_RAY_KEY_DATA = "data"
_RAY_KEY_CLUSTER_STATUS = "clusterStatus"
_RAY_KEY_LOAD_METRICS = "loadMetricsReport"
_RAY_KEY_USAGE = "usage"
_RAY_KEY_SUMMARY = "summary"
_RAY_KEY_NODE_LOGICAL_RESOURCES = "nodeLogicalResources"
_RAY_KEY_RAYLET = "raylet"
_RAY_KEY_NODE_ID = "nodeId"
_RAY_KEY_RESOURCES_TOTAL = "resourcesTotal"
_RAY_KEY_NODE_MANAGER_ADDRESS = "nodeManagerAddress"
_RAY_KEY_IP = "ip"
_RAY_KEY_RAYLET_STATE = "state"
_RAYLET_STATE_ALIVE = "ALIVE"
_NODES_VIEW_PARAM = "view=summary"
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


def _parse_batches(entrypoint: str | None) -> list[str]:
    if not entrypoint:
        return []
    return _BATCH_RE.findall(entrypoint)


def build_client(dashboard_url: str) -> JobSubmissionClient | None:
    """Construct the SDK client. Returns None if Ray's dashboard is unreachable.

    The constructor issues HTTP calls to verify the API version. Ray's SDK
    documents `RuntimeError` for protocol failures and raises `ConnectionError`
    when the dashboard isn't listening; we treat both as "offline".
    """
    try:
        return JobSubmissionClient(address=dashboard_url)
    except RAY_TRANSIENT_ERRORS as exc:
        log.info(f"Ray dashboard unreachable at {dashboard_url}: {exc}")
        return None


async def health(client: JobSubmissionClient | None, dashboard_url: str) -> RayHealth:
    if client is None:
        return RayHealth(ok=False, dashboard_url=dashboard_url, error="Ray dashboard unreachable")
    try:
        await to_thread.run_sync(client.get_version)
    except RAY_TRANSIENT_ERRORS as exc:
        return RayHealth(ok=False, dashboard_url=dashboard_url, error=f"{type(exc).__name__}: {exc!s}"[:_ERROR_MSG_MAX_LEN])
    # get_version() is called purely as a liveness probe (its return is the
    # Jobs-API version, not the cluster Ray version — see RayHealth).
    return RayHealth(ok=True, dashboard_url=dashboard_url, client_ray_version=ray.__version__)


async def list_jobs(client: JobSubmissionClient | None, dashboard_url: str) -> RayJobsPayload:
    if client is None:
        return RayJobsPayload(ok=False, dashboard_url=dashboard_url, error="Ray dashboard unreachable")
    try:
        details = await to_thread.run_sync(client.list_jobs)
    except RAY_TRANSIENT_ERRORS as exc:
        return RayJobsPayload(ok=False, dashboard_url=dashboard_url, error=f"{type(exc).__name__}: {exc!s}"[:_ERROR_MSG_MAX_LEN])
    jobs: list[RayJob] = []
    for d in details:
        if d is None:
            continue
        # `.dict()` (V1 API) is required: Ray ships `JobDetails` as a Pydantic V1 model
        # so `model_dump()` doesn't exist. See `schemas/ray.py` for the rationale.
        payload = d.dict()
        payload["batches"] = _parse_batches(d.entrypoint)
        payload["logs_url"] = f"{dashboard_url}/#/jobs/{d.submission_id}" if d.submission_id else None
        jobs.append(RayJob.model_validate(payload))
    jobs.sort(key=lambda j: getattr(j, "start_time", None) or 0, reverse=True)
    return RayJobsPayload(ok=True, dashboard_url=dashboard_url, jobs=jobs)


def _parse_res_value(s: str) -> float:
    m = re.match(r"^([0-9.]+)([A-Za-z]*)$", s.strip())
    if not m:
        return 0.0
    return float(m.group(1)) * _BYTE_UNITS.get(m.group(2), 1) if m.group(2) else float(m.group(1))


def _parse_logical(text: str) -> dict[str, float]:
    used: dict[str, float] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if "/" not in line or " " not in line:
            continue
        ratio, _, name = line.rpartition(" ")
        used_s, _, _total_s = ratio.partition("/")
        used[name] = _parse_res_value(used_s)
    return used


async def cluster_status(http: httpx.AsyncClient, dashboard_url: str) -> RayClusterPayload:
    total = {"CPU": 0.0, "GPU": 0.0, "memory": 0.0}
    used = {"CPU": 0.0, "GPU": 0.0, "memory": 0.0}
    nodes: list[RayNode] = []

    try:
        cs_resp = await http.get(f"{dashboard_url}/api/cluster_status")
        cs_resp.raise_for_status()
        cs = (cs_resp.json().get(_RAY_KEY_DATA) or {}).get(_RAY_KEY_CLUSTER_STATUS, {})
        usage = (cs.get(_RAY_KEY_LOAD_METRICS) or {}).get(_RAY_KEY_USAGE) or {}
        for k in total:
            pair = usage.get(k)
            if isinstance(pair, list) and len(pair) == 2:
                used[k] = float(pair[0] or 0)
                total[k] = float(pair[1] or 0)

        try:
            ns_resp = await http.get(f"{dashboard_url}/nodes?{_NODES_VIEW_PARAM}")
            ns_resp.raise_for_status()
            data = ns_resp.json().get(_RAY_KEY_DATA) or {}
            summary = data.get(_RAY_KEY_SUMMARY) or []
            logical = data.get(_RAY_KEY_NODE_LOGICAL_RESOURCES) or {}
            for n in summary:
                raylet = n.get(_RAY_KEY_RAYLET) or {}
                node_id = raylet.get(_RAY_KEY_NODE_ID)
                if not node_id:
                    continue
                parsed_used = _parse_logical(logical.get(node_id, ""))
                rtotal = raylet.get(_RAY_KEY_RESOURCES_TOTAL) or {}
                nodes.append(
                    RayNode(
                        node_id=node_id,
                        node_ip=raylet.get(_RAY_KEY_NODE_MANAGER_ADDRESS) or n.get(_RAY_KEY_IP),
                        alive=raylet.get(_RAY_KEY_RAYLET_STATE) == _RAYLET_STATE_ALIVE,
                        resources_total={k: float(rtotal.get(k, 0) or 0) for k in total},
                        resources_used={k: parsed_used.get(k, 0.0) for k in total},
                    )
                )
        except httpx.HTTPError:
            log.debug("per-node detail unavailable; aggregates still returned", exc_info=True)
    except httpx.HTTPError as exc:
        return RayClusterPayload(ok=False, dashboard_url=dashboard_url, error=f"{type(exc).__name__}: {exc!s}"[:_ERROR_MSG_MAX_LEN])

    return RayClusterPayload(
        ok=True,
        dashboard_url=dashboard_url,
        node_count=len(nodes),
        alive_count=sum(1 for n in nodes if n.alive),
        total_resources=total,
        used_resources=used,
        nodes=nodes,
    )


async def proxy(
    http: httpx.AsyncClient,
    dashboard_url: str,
    path: str,
    method: str,
    query: bytes | str,
    headers: dict[str, str],
    body: bytes,
) -> ProxyResponse:
    """Forward an arbitrary path to the Ray Dashboard, same-origin to the browser."""
    url = f"{dashboard_url}/{path.lstrip('/')}"
    fwd = {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP and k.lower() != "host"}
    qs = query.decode() if isinstance(query, bytes) else query
    try:
        resp = await http.request(method, url, params=qs, headers=fwd, content=body or None)
    except httpx.HTTPError as exc:
        return ProxyResponse(
            content=f"ray dashboard unreachable: {exc}".encode(),
            status_code=HTTPStatus.BAD_GATEWAY,
            headers={"content-type": "text/plain"},
        )
    return ProxyResponse(
        content=resp.content,
        status_code=resp.status_code,
        headers={k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP},
    )
