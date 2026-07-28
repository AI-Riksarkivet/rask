"""Ray Dashboard endpoints — viewer's normalized `/api/v1/ray/*` (health, jobs,
cluster, …). Thin shell over ray_kit.dashboard."""

from fastapi import APIRouter

from compute.dependencies import HttpDep, RayClientDep
from ray_kit import dashboard
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
from service_kit.dependencies import SettingsDep


router = APIRouter(prefix="/ray", tags=["ray"])


@router.get("/health")
async def ray_health(client: RayClientDep, settings: SettingsDep) -> RayHealth:
    return await dashboard.health(client, settings.ray_dashboard_url)


@router.get("/jobs")
async def ray_jobs(client: RayClientDep, settings: SettingsDep) -> RayJobsPayload:
    return await dashboard.list_jobs(client, settings.ray_dashboard_url)


@router.get("/jobs/{submission_id}/logs")
async def ray_job_logs(client: RayClientDep, submission_id: str, tail: int = 2000) -> RayJobLogsPayload:
    return await dashboard.job_logs(client, submission_id, tail)


@router.get("/cluster")
async def ray_cluster(http: HttpDep, settings: SettingsDep) -> RayClusterPayload:
    return await dashboard.cluster_status(http, settings.ray_dashboard_url)


@router.get("/actors")
async def ray_actors(http: HttpDep, settings: SettingsDep) -> RayActorsPayload:
    return await dashboard.list_actors(http, settings.ray_dashboard_url)


@router.get("/tasks")
async def ray_tasks(http: HttpDep, settings: SettingsDep) -> RayTasksPayload:
    return await dashboard.list_tasks(http, settings.ray_dashboard_url)


@router.get("/overview")
async def ray_overview(http: HttpDep, settings: SettingsDep) -> RayOverviewPayload:
    return await dashboard.overview(http, settings.ray_dashboard_url)


@router.get("/logs")
async def ray_logs(
    http: HttpDep,
    settings: SettingsDep,
    node_id: str,
    filename: str | None = None,
    lines: int = 200,
) -> RayLogsPayload:
    return await dashboard.logs(http, settings.ray_dashboard_url, node_id, filename, lines)
