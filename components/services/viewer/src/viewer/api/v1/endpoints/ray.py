"""Ray Dashboard endpoints.

Two routers:

  - `router` — viewer's normalized `/api/v1/ray/*` (health/jobs/cluster),
    Pydantic responses. Mounted under the v1 API prefix.

  - `proxy_router` — transparent reverse proxy for the Ray Serve status API
    (`/api/serve/*`), which the SPA's /serve page reads raw. These are Ray's
    URLs, not viewer's, so the proxy_router is mounted at the **root** (no
    `/api/v1/` prefix) and `include_in_schema=False` keeps it off the OpenAPI
    document. (The embedded-dashboard proxy paths — /ray-dashboard, /api/v0,
    /logs, … — were removed along with the /embed page.)

    The proxy uses `api_route(methods=…)` deliberately — `core-conventions.md`
    § One HTTP operation per function targets application routes, not
    transparent reverse proxies.
"""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ray_kit import dashboard as ray_dashboard
from ray_kit.schemas import (
    RayActorsPayload,
    RayClusterPayload,
    RayHealth,
    RayJobLogsPayload,
    RayJobsPayload,
    RayLogsPayload,
    RayOverviewPayload,
    RayTasksPayload,
)
from viewer.api.dependencies import HttpDep, RayClientDep, SettingsDep


router = APIRouter(prefix="/ray", tags=["ray"])
proxy_router = APIRouter(include_in_schema=False)

_PROXY_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"]


@router.get("/health")
async def ray_health(client: RayClientDep, settings: SettingsDep) -> RayHealth:
    return await ray_dashboard.health(client, settings.ray_dashboard_url)


@router.get("/jobs")
async def ray_jobs(client: RayClientDep, settings: SettingsDep) -> RayJobsPayload:
    return await ray_dashboard.list_jobs(client, settings.ray_dashboard_url)


@router.get("/jobs/{submission_id}/logs")
async def ray_job_logs(client: RayClientDep, submission_id: str, tail: int = 2000) -> RayJobLogsPayload:
    return await ray_dashboard.job_logs(client, submission_id, tail)


@router.get("/cluster")
async def ray_cluster(http: HttpDep, settings: SettingsDep) -> RayClusterPayload:
    return await ray_dashboard.cluster_status(http, settings.ray_dashboard_url)


@router.get("/actors")
async def ray_actors(http: HttpDep, settings: SettingsDep) -> RayActorsPayload:
    return await ray_dashboard.list_actors(http, settings.ray_dashboard_url)


@router.get("/tasks")
async def ray_tasks(http: HttpDep, settings: SettingsDep) -> RayTasksPayload:
    return await ray_dashboard.list_tasks(http, settings.ray_dashboard_url)


@router.get("/overview")
async def ray_overview(http: HttpDep, settings: SettingsDep) -> RayOverviewPayload:
    return await ray_dashboard.overview(http, settings.ray_dashboard_url)


@router.get("/logs")
async def ray_logs(
    http: HttpDep,
    settings: SettingsDep,
    node_id: str,
    filename: str | None = None,
    lines: int = 200,
) -> RayLogsPayload:
    return await ray_dashboard.logs(http, settings.ray_dashboard_url, node_id, filename, lines)


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


# Only the Serve status API is still proxied — the SPA's /serve page reads it
# raw. Everything else moved to normalized /api/ray/* endpoints, and the
# embedded dashboard (which needed /ray-dashboard, /api/v0, /logs, …) is gone.
for _prefix in ("/api/serve",):
    _register_proxy(_prefix)
