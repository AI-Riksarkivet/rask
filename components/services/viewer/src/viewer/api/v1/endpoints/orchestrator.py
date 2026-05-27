from fastapi import APIRouter

from viewer.api.dependencies import HttpDep, RayClientDep, SessionDep, SettingsDep
from viewer.schemas.orchestrator import OrchestratorState
from viewer.services import orchestrator as orchestrator_service


router = APIRouter(tags=["orchestrator"])


@router.get("/api/orchestrator/state")
async def orchestrator_state(
    settings: SettingsDep,
    http: HttpDep,
    client: RayClientDep,
    session: SessionDep,
) -> OrchestratorState:
    return await orchestrator_service.derive_state(http, client, settings.ray_dashboard_url, session)
