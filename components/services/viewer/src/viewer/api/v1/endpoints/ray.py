"""Ray Dashboard endpoints.

Two routers:

  - `router` — viewer's normalized `/api/v1/ray/*` (health/jobs/cluster),
    Pydantic responses. Mounted under the v1 API prefix.

  - `proxy_router` — reverse-proxy paths Ray's bundled JS expects
    (`/api/v0/*`, `/api/jobs/*`, `/logs/*`, `/ray-dashboard/*`, plus a few
    exact paths). These are Ray's URLs, not viewer's, so the proxy_router
    is mounted at the **root** (no `/api/v1/` prefix) and `include_in_schema=False`
    keeps the proxy off the OpenAPI document.

    The proxy uses `api_route(methods=…)` deliberately — `core-conventions.md`
    § One HTTP operation per function targets application routes, not
    transparent reverse proxies. We don't know what method the iframe will
    issue, so we forward whatever comes in.
"""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from viewer.api.dependencies import HttpDep, RayClientDep, SettingsDep
from viewer.schemas.ray import RayClusterPayload, RayHealth, RayJobsPayload
from viewer.services import ray_dashboard


router = APIRouter(prefix="/ray", tags=["ray"])
proxy_router = APIRouter(include_in_schema=False)

_PROXY_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"]


@router.get("/health")
async def ray_health(client: RayClientDep, settings: SettingsDep) -> RayHealth:
    return await ray_dashboard.health(client, settings.ray_dashboard_url)


@router.get("/jobs")
async def ray_jobs(client: RayClientDep, settings: SettingsDep) -> RayJobsPayload:
    return await ray_dashboard.list_jobs(client, settings.ray_dashboard_url)


@router.get("/cluster")
async def ray_cluster(http: HttpDep, settings: SettingsDep) -> RayClusterPayload:
    return await ray_dashboard.cluster_status(http, settings.ray_dashboard_url)


async def _proxy(request: Request, http: HttpDep, settings: SettingsDep, path: str) -> Response:
    body = await request.body()
    resp = await ray_dashboard.proxy(
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

    proxy_router.add_api_route(f"{prefix}/{{path:path}}", catchall, methods=_PROXY_METHODS, name=f"ray-proxy-{prefix}")
    proxy_router.add_api_route(prefix, catchall_root, methods=["GET", "HEAD"], name=f"ray-proxy-{prefix}-root")


def _register_exact(path: str) -> None:
    suffix = path.lstrip("/")

    async def exact(request: Request, http: HttpDep, settings: SettingsDep) -> Response:
        return await _proxy(request, http, settings, suffix)

    proxy_router.add_api_route(path, exact, methods=["GET", "POST", "HEAD"], name=f"ray-proxy-{path}")


@proxy_router.api_route("/ray-dashboard/{path:path}", methods=_PROXY_METHODS)
async def ray_dashboard_spa(request: Request, http: HttpDep, settings: SettingsDep, path: str) -> Response:
    return await _proxy(request, http, settings, path)


@proxy_router.api_route("/ray-dashboard", methods=["GET", "HEAD"])
async def ray_dashboard_root(request: Request, http: HttpDep, settings: SettingsDep) -> Response:
    return await _proxy(request, http, settings, "")


for _prefix in ("/api/v0", "/api/jobs", "/logs"):
    _register_proxy(_prefix)

for _exact in (
    "/api/cluster_status",
    "/api/version",
    "/api/grafana_health",
    "/api/prometheus_health",
    "/api/authenticate",
    "/api/authentication_mode",
):
    _register_exact(_exact)
