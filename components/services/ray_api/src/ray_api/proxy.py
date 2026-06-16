"""Transparent reverse proxy for the Ray Serve status API (`/api/serve/*`), which
the SPA's /serve page reads raw. Mounted at the root (no /api/v1 prefix),
include_in_schema=False. Uses api_route(methods=…) deliberately — this is a
transparent proxy, not an application route."""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ray_api.dependencies import HttpDep
from ray_kit import dashboard
from service_kit.dependencies import SettingsDep


router = APIRouter(include_in_schema=False)

_PROXY_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"]


async def _proxy(request: Request, http: HttpDep, settings: SettingsDep, path: str) -> Response:
    body = await request.body()
    resp = await dashboard.proxy(
        http,
        settings.ray_dashboard_url,
        path,
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
