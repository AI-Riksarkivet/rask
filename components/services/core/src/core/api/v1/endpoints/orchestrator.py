from fastapi import APIRouter, Request

from core.api.dependencies import HttpDep, RayClientDep, SessionDep, SettingsDep
from core.lifespan import create_orchestrator_task, is_orchestrator_running, stop_orchestrator_task
from core.schemas.orchestrator import OrchestratorControlResponse, OrchestratorState
from core.services.orchestrator import derive_state


router = APIRouter(prefix="/orchestrator", tags=["orchestrator"])


@router.get("/state")
async def orchestrator_state(
    request: Request,
    settings: SettingsDep,
    http: HttpDep,
    client: RayClientDep,
    session: SessionDep,
) -> OrchestratorState:
    state = await derive_state(
        http=http,
        client=client,
        dashboard_url=settings.ray_dashboard_url,
        session=session,
    )
    state.running = is_orchestrator_running(request.app)
    return state


@router.post("/start")
async def orchestrator_start(request: Request) -> OrchestratorControlResponse:
    """Start the periodic orchestrator loop. Idempotent — calling while
    already running is a no-op."""
    app = request.app
    if not is_orchestrator_running(app):
        app.state.orchestrator_task = create_orchestrator_task(app)
    return OrchestratorControlResponse(running=True)


@router.post("/stop")
async def orchestrator_stop(request: Request) -> OrchestratorControlResponse:
    """Cancel the orchestrator loop. Already-submitted Ray jobs keep running
    on the cluster; only the periodic tick stops. Idempotent."""
    await stop_orchestrator_task(request.app)
    return OrchestratorControlResponse(running=False)
