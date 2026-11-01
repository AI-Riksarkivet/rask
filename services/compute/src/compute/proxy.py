"""Transparent reverse proxy for the Ray Serve status API (`/api/serve/*`), which
the SPA's /serve page reads raw. Mounted at the root (no /api/v1 prefix),
include_in_schema=False. Uses api_route(methods=…) deliberately — this is a
transparent proxy, not an application route."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from compute import security
from compute.dependencies import ComputeSettingsDep, HttpDep
from ray_kit import dashboard


# GATED AT THE ROUTER. The proxy is one explicit route, so nothing else the Ray dashboard serves
# is reachable through it (a `{path:path}` catch-all would relay every GET the dashboard adds).
router = APIRouter(include_in_schema=False, dependencies=[Depends(security.require_read)])

# READ-ONLY on purpose. The SPA's /serve page only *reads* Serve status, and this
# proxy is unauthenticated (see docs/architecture: no app auth). Ray's dashboard
# exposes write verbs on the same surface — PUT /api/serve/applications/ deploys an
# app with a caller-chosen import_path/runtime_env, i.e. arbitrary code execution on
# the cluster — so forwarding anything but GET/HEAD would hand that to any caller who
# can reach the ingress. Never widen this without an auth layer in front of /api.
_PROXY_METHODS = ["GET", "HEAD"]

# The one Serve path the SPA and the flows catalog read. Both spellings reach the same handler:
# intermediaries normalize the trailing slash away, and `_canonical` restores it for Ray.
_APPLICATIONS_PATHS = ("/api/serve/applications", "/api/serve/applications/")


def _canonical(path: str) -> str:
    """Ray's Serve REST API is trailing-slash-strict — `/api/serve/applications/`
    is the resource; the slash-less form 404s. Intermediaries on the request path
    (notably the dapr sidecar's service-invoke) normalize the trailing slash away,
    so this proxy — which exists solely to forward Ray's Serve status API — restores
    it. Keeps the compute service correct on its own behind any proxy, not just direct calls."""
    return path if path.endswith("/") else path + "/"


async def applications(request: Request, http: HttpDep, settings: ComputeSettingsDep) -> Response:
    body = await request.body()
    resp = await dashboard.proxy(
        http,
        settings.ray_dashboard_url,
        _canonical("api/serve/applications"),
        request.method,
        request.url.query,
        dict(request.headers),
        body,
    )
    # `resp.headers` are already consistent with the DECODED `resp.content` — ray_kit strips the
    # compressed-transfer `content-encoding`/`content-length` at its own boundary (`_RESPONSE_STRIP`)
    # — so they relay verbatim and Starlette computes the length for the bytes actually sent.
    return Response(content=resp.content, status_code=resp.status_code, headers=dict(resp.headers))


for _path in _APPLICATIONS_PATHS:
    router.add_api_route(_path, applications, methods=_PROXY_METHODS, name="serve-applications-proxy")
