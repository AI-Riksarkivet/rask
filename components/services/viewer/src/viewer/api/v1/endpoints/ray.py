"""Ray Dashboard endpoints.

Two surfaces:
  - Viewer's normalized `/api/ray/*` (health/jobs/cluster) — Pydantic responses
    backed by `ray.job_submission.JobSubmissionClient` where the SDK models it.
  - Reverse-proxy paths Ray's bundled JS expects (`/api/v0/*`, `/api/jobs/*`,
    `/logs/*`, `/ray-dashboard/*`, plus a few exact paths) — pure plumbing,
    `include_in_schema=False`.
"""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from viewer.api.dependencies import HttpDep, RayClientDep, SettingsDep
from viewer.schemas.ray import RayClusterPayload, RayHealth, RayJobsPayload
from viewer.services import ray_dashboard


router = APIRouter(tags=["ray"])
proxy_router = APIRouter(include_in_schema=False)

_PROXY_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"]


@router.get("/api/ray/health")
async def ray_health(client: RayClientDep, settings: SettingsDep) -> RayHealth:
    return await ray_dashboard.health(client, settings.ray_dashboard_url)


@router.get("/api/ray/jobs")
async def ray_jobs(client: RayClientDep, settings: SettingsDep) -> RayJobsPayload:
    return await ray_dashboard.list_jobs(client, settings.ray_dashboard_url)


@router.get("/api/ray/cluster")
async def ray_cluster(http: HttpDep, settings: SettingsDep) -> RayClusterPayload:
    return await ray_dashboard.cluster_status(http, settings.ray_dashboard_url)


async def _proxy(request: Request, path: str) -> Response:
    settings = request.app.state.settings
    http = request.app.state.http
    body = await request.body()
    content, status, hdrs = await ray_dashboard.proxy(
        http,
        settings.ray_dashboard_url,
        path,
        request.method,
        request.url.query,
        dict(request.headers),
        body,
    )
    return Response(content=content, status_code=status, headers=hdrs)


def _register_proxy(prefix: str) -> None:
    """Forward `<prefix>` and `<prefix>/{path:path}` to the Ray Dashboard."""
    suffix = prefix.lstrip("/")

    async def catchall(request: Request, path: str) -> Response:
        return await _proxy(request, f"{suffix}/{path}")

    async def catchall_root(request: Request) -> Response:
        return await _proxy(request, suffix)

    proxy_router.add_api_route(f"{prefix}/{{path:path}}", catchall, methods=_PROXY_METHODS, name=f"ray-proxy-{prefix}")
    proxy_router.add_api_route(prefix, catchall_root, methods=["GET", "HEAD"], name=f"ray-proxy-{prefix}-root")


def _register_exact(path: str) -> None:
    suffix = path.lstrip("/")

    async def exact(request: Request) -> Response:
        return await _proxy(request, suffix)

    proxy_router.add_api_route(path, exact, methods=["GET", "POST", "HEAD"], name=f"ray-proxy-{path}")


@proxy_router.api_route("/ray-dashboard/{path:path}", methods=_PROXY_METHODS)
async def ray_dashboard_spa(request: Request, path: str) -> Response:
    return await _proxy(request, path)


@proxy_router.api_route("/ray-dashboard", methods=["GET", "HEAD"])
async def ray_dashboard_root(request: Request) -> Response:
    return await _proxy(request, "")


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
