"""Transparent reverse proxy for the Ray Serve status API (`/api/serve/*`), which
the SPA's /serve page reads raw. Mounted at the root (no /api/v1 prefix),
include_in_schema=False. Uses api_route(methods=…) deliberately — this is a
transparent proxy, not an application route."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from compute import security
from compute.dependencies import HttpDep
from ray_kit import dashboard
from service_kit.dependencies import SettingsDep


# GATED AT THE ROUTER, and per route would not be enough: `_register_proxy` adds a
# `{path:path}` catch-all, so ANYTHING the Ray dashboard serves is reachable through it.
router = APIRouter(include_in_schema=False, dependencies=[Depends(security.require_read)])

# READ-ONLY on purpose. The SPA's /serve page only *reads* Serve status, and this
# proxy is unauthenticated (see docs/architecture: no app auth). Ray's dashboard
# exposes write verbs on the same surface — PUT /api/serve/applications/ deploys an
# app with a caller-chosen import_path/runtime_env, i.e. arbitrary code execution on
# the cluster — so forwarding anything but GET/HEAD would hand that to any caller who
# can reach the ingress. Never widen this without an auth layer in front of /api.
_PROXY_METHODS = ["GET", "HEAD"]


def _canonical(path: str) -> str:
    """Ray's Serve REST API is trailing-slash-strict — `/api/serve/applications/`
    is the resource; the slash-less form 404s. Intermediaries on the request path
    (notably the dapr sidecar's service-invoke) normalize the trailing slash away,
    so this proxy — which exists solely to forward Ray's Serve status API — restores
    it. Keeps the compute service correct on its own behind any proxy, not just direct calls."""
    return path if path.endswith("/") else path + "/"


async def _proxy(request: Request, http: HttpDep, settings: SettingsDep, path: str) -> Response:
    body = await request.body()
    resp = await dashboard.proxy(
        http,
        settings.ray_dashboard_url,
        _canonical(path),
        request.method,
        request.url.query,
        dict(request.headers),
        body,
    )
    return Response(content=resp.content, status_code=resp.status_code, headers=resp.headers)


def _register_proxy(prefix: str) -> None:
    """Forward `<prefix>` and `<prefix>/{path:path}` to the Ray Dashboard."""
    suffix = prefix.lstrip("/")

    async def catchall(request: Request, http: HttpDep, settings: SettingsDep, path: str) -> Response:
        return await _proxy(request, http, settings, f"{suffix}/{path}")

    async def catchall_root(request: Request, http: HttpDep, settings: SettingsDep) -> Response:
        return await _proxy(request, http, settings, suffix)

    router.add_api_route(f"{prefix}/{{path:path}}", catchall, methods=_PROXY_METHODS, name=f"ray-proxy-{prefix}")
    router.add_api_route(prefix, catchall_root, methods=["GET", "HEAD"], name=f"ray-proxy-{prefix}-root")


# Only the Serve status API is proxied — the SPA's /serve page reads it raw.
for _prefix in ("/api/serve",):
    _register_proxy(_prefix)
