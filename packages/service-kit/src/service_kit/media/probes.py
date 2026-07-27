"""Operational probes: ``/livez`` (dependency-free) and ``/readyz`` (state-gated).

Separate from ``/api/health`` (the frontend status badge, owned by the system
feature package — its shape is load-bearing and unchanged). These two are for a
process supervisor / load balancer: livez proves the event loop is responsive;
readyz reports startup/shutdown gating off ``app.state``.
"""

from http import HTTPStatus

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from service_kit.schemas.health import Liveness, Readiness, ReadinessStatus


router = APIRouter(tags=["probes"])


@router.get("/livez")
def livez() -> Liveness:
    return Liveness()


@router.get("/readyz")
def readyz(request: Request) -> JSONResponse:
    state = request.app.state
    if not getattr(state, "startup_complete", False):
        body = Readiness(status=ReadinessStatus.starting)
        return JSONResponse(status_code=HTTPStatus.SERVICE_UNAVAILABLE, content=body.model_dump())
    if getattr(state, "shutting_down", False):
        body = Readiness(status=ReadinessStatus.shutting_down)
        return JSONResponse(status_code=HTTPStatus.SERVICE_UNAVAILABLE, content=body.model_dump())
    return JSONResponse(status_code=HTTPStatus.OK, content=Readiness(status=ReadinessStatus.ready).model_dump())
