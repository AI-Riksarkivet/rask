"""Producer discovery from RAY SERVE — the registry's live source of truth.

Model endpoints in this estate are and will always be Ray Serve deployments on the ray
cluster; a hand-maintained env registry can only ever lag what is actually deployed. So the
registry's primary source is the Serve control plane itself: the dashboard's
``GET /api/serve/applications/`` (the v2 REST API, response schema
``ray.serve.schema.ServeInstanceDetails``) lists every application with its ``route_prefix``,
``status`` and per-deployment ``deployment_config.user_config``.

THE CONVENTION: a deployment that serves labeling declares itself by carrying a ``labeling``
key in its ``user_config``::

    user_config:
      labeling:
        producer: sam           # optional — the deployment name, lowercased, is the default
        returns: [polygon]
        inputs: [points, region]

``user_config`` is chosen deliberately: Serve passes it to the deployment's ``reconfigure``
method and it is UPDATABLE WITHOUT RESTARTING REPLICAS — so a model's declared contract can be
corrected live, and deploying a model IS registering it. Nothing here imports ``ray``: the
response is walked structurally (the schema is pinned by Ray's own ``extra="forbid"`` models,
and a structural walk survives additive evolution).

Only RUNNING applications are offered — per the Serve status vocabulary, RUNNING means
"all deployments are healthy", so the app-level filter is the whole health check (a
DEPLOYING/DEPLOY_FAILED/DELETING app is not an endpoint anyone can call).

DEPLOYMENT DISCIPLINE (from the REST API docs): ``PUT /api/serve/applications/`` is
declarative and REPLACES — "removes all applications not listed in the new config". One
writer owns the Serve config: under KubeRay that is the RayService CR's serveConfigV2, and
ad-hoc ``serve deploy`` PUTs against a cluster carrying /transcribe + /htrflow would DELETE
them unless the full application list rides along. Same single-writer rule as the Helm
release; discovery only ever READS. The env registry (``MEDIA_ASSIST_BACKENDS``) stays as the OPERATOR OVERRIDE:
an env entry with the same producer name wins over discovery, because config is intent and
discovery is observation. Discovery failing (cluster down, dashboard unreachable) degrades to
the env registry + mock — the same fail-honest posture as everything else in this plane.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from service_kit.media.config import AssistBackend


logger = logging.getLogger(__name__)

APPLICATIONS_PATH = "/api/serve/applications/"
#: How long a discovery result is served before the dashboard is asked again. Discovery sits
#: on the producers-listing and assist request paths; per-request polling would hammer the
#: dashboard for an answer that changes on deploy cadence, not request cadence.
DISCOVERY_TTL_S = 15.0

_cache: dict[str, tuple[float, dict[str, AssistBackend]]] = {}


def discovered_backends(
    http: httpx.Client | None,
    discovery_url: str | None,
    proxy_url: str | None,
    *,
    ttl: float = DISCOVERY_TTL_S,
    now: float | None = None,
) -> dict[str, AssistBackend]:
    """The producers Ray Serve is CURRENTLY serving, as registry entries.

    Returns ``{}`` when discovery is off (no URL), the client is absent, or the dashboard
    cannot be read — the caller falls back to the env registry and the mock, and the failure
    is logged rather than raised: an unreachable control plane must not take assist down."""
    if not discovery_url or http is None:
        return {}
    at = time.monotonic() if now is None else now
    cached = _cache.get(discovery_url)
    if cached is not None and at - cached[0] < ttl:
        return cached[1]
    try:
        resp = http.get(discovery_url.rstrip("/") + APPLICATIONS_PATH, timeout=5.0)
        resp.raise_for_status()
        details = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("serve discovery unreachable (%s); registry falls back to config", e)
        return {}
    backends = parse_applications(details, proxy_url or _proxy_base(discovery_url, details))
    _cache[discovery_url] = (at, backends)
    return backends


def parse_applications(details: Any, proxy_base: str) -> dict[str, AssistBackend]:
    """Walk ``ServeInstanceDetails`` structurally: RUNNING applications whose deployments
    declare a ``labeling`` block become registry entries addressed at the Serve HTTP proxy
    under the application's ``route_prefix``."""
    backends: dict[str, AssistBackend] = {}
    applications = details.get("applications") if isinstance(details, dict) else None
    if not isinstance(applications, dict):
        return backends
    for app in applications.values():
        if not isinstance(app, dict) or app.get("status") != "RUNNING":
            continue
        route_prefix = app.get("route_prefix")
        if not route_prefix:
            continue  # explicitly not exposed over HTTP — not callable, not offered
        url = proxy_base.rstrip("/") + route_prefix
        for name, deployment in (app.get("deployments") or {}).items():
            declared = _labeling_of(deployment)
            if declared is None:
                continue
            producer = str(declared.get("producer") or name).lower()
            backends[producer] = AssistBackend(
                url=url,
                returns=[str(r) for r in declared.get("returns") or []],
                inputs=[str(i) for i in declared.get("inputs") or []],
            )
    return backends


def _labeling_of(deployment: Any) -> dict[str, Any] | None:
    if not isinstance(deployment, dict):
        return None
    user_config = (deployment.get("deployment_config") or {}).get("user_config")
    labeling = user_config.get("labeling") if isinstance(user_config, dict) else None
    return labeling if isinstance(labeling, dict) else None


def _proxy_base(discovery_url: str, details: Any) -> str:
    """The Serve HTTP proxy base, when the operator did not pin one: the dashboard's host
    with the port Serve itself reports in ``http_options`` (default 8000)."""
    port = 8000
    if isinstance(details, dict):
        options = details.get("http_options")
        if isinstance(options, dict) and isinstance(options.get("port"), int):
            port = options["port"]
    scheme, _, rest = discovery_url.partition("://")
    host = rest.split("/")[0].rsplit(":", 1)[0]
    return f"{scheme}://{host}:{port}"


def reset_cache() -> None:
    """Test seam — the module cache is process-global on purpose (one dashboard, one truth)."""
    _cache.clear()
