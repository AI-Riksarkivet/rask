"""The ONE operational-probe router: ``/livez`` (dependency-free) + ``/readyz`` (state-gated).

Separate from the frontend-facing ``/api/health`` badge (whose body is a service-specific
contract). These two are for a process supervisor / load balancer: livez proves the event
loop is responsive, readyz reports lifecycle gating off ``app.state`` plus — optionally —
one service-specific hard-dependency check.

Before this module the same twenty lines lived, hand-rolled, in six places (catalog, lineage,
the two medallion apps, compaction, and ``service_kit.media.probes``) and had already drifted
three ways: the gate order (shutdown-first vs startup-first), the body shape (a bare
``{"status": ...}`` dict vs the ``Readiness`` model), and — in the media copy — a sync ``def``
handler, which puts liveness on the blocking threadpool where it queues behind heavy Arrow/Lance
work and fails exactly when the pod is busiest. One home, one shape, ``async def``.

Mount the module-level :data:`router` when readiness is pure lifecycle gating; call
:func:`make_probes_router` with a ``ready_check`` when the service has a hard dependency
(lineage's AGE graph) or a fact worth reporting (the catalog's resolved namespace id).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from http import HTTPStatus

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from service_kit.schemas.health import Liveness, Readiness, ReadinessStatus


#: A service-specific readiness check, run only AFTER the lifecycle gates pass. Returns the
#: ``Readiness`` body to serve: ``status=ready`` → 200, anything else (``degraded``) → 503.
type ReadyCheck = Callable[[Request], Awaitable[Readiness]]


def _serve(body: Readiness) -> JSONResponse:
    status = HTTPStatus.OK if body.status is ReadinessStatus.ready else HTTPStatus.SERVICE_UNAVAILABLE
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def make_probes_router(ready_check: ReadyCheck | None = None) -> APIRouter:
    """Build the probe router, optionally with a service-specific readiness check."""
    probes = APIRouter(tags=["health"])

    @probes.get("/livez")
    async def livez() -> Liveness:
        # async, not sync def: the probe must run ON the event loop, never queued behind the
        # blocking data-plane threadpool — no I/O happens here, so there is nothing to yield for.
        return Liveness()

    @probes.get("/readyz", response_model=Readiness)
    async def readyz(request: Request) -> JSONResponse:
        state = request.app.state
        # Drain first: once the lifespan flips shutting_down, that is the answer regardless of
        # anything else, and the check must never touch a dependency that is already closing.
        if getattr(state, "shutting_down", False):
            return _serve(Readiness(status=ReadinessStatus.shutting_down))
        if not getattr(state, "startup_complete", False):
            return _serve(Readiness(status=ReadinessStatus.starting))
        if ready_check is None:
            return _serve(Readiness(status=ReadinessStatus.ready))
        return _serve(await ready_check(request))

    return probes


#: The lifecycle-only probe router — mount this unless the service has a hard dependency to check.
router = make_probes_router()
