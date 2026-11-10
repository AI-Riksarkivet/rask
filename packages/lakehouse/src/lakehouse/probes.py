"""The ONE operational-probe router: ``/livez`` (dependency-free) + ``/readyz`` (state-gated).

These two are for a process supervisor / load balancer: livez proves the event
loop is responsive, readyz reports lifecycle gating off ``app.state`` plus — optionally —
one service-specific hard-dependency check.

Before this module the same twenty lines lived, hand-rolled, in six places and had already drifted
three ways: the gate order (shutdown-first vs startup-first), the body shape (a bare
``{"status": ...}`` dict vs the ``Readiness`` model), and — in one copy — a sync ``def``
handler, which puts liveness on the blocking threadpool where it queues behind heavy Arrow/Lance
work and fails exactly when the pod is busiest. One home, one shape, ``async def``.

:func:`make_probes_router` takes the service's ``ready_check``: its hard dependency (lineage's feed, read through the
catalog) or a fact worth reporting (the catalog's resolved namespace id), asked once the lifecycle gates pass.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from http import HTTPStatus

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from lakehouse.lifecycle import is_draining, is_started
from lakehouse.schemas.health import Liveness, Readiness, ReadinessStatus


#: A service-specific readiness check, run only AFTER the lifecycle gates pass. Returns the
#: ``Readiness`` body to serve: ``status=ready`` → 200, anything else (``degraded``) → 503.
type ReadyCheck = Callable[[Request], Awaitable[Readiness]]
#: A process's own liveness answer, for one whose probes do not run on the loop its work runs on: without it ``/livez``
#: proves only that the loop serving the probe turns. It must not wait or touch a dependency.
type LiveCheck = Callable[[], bool]


def _serve(body: Readiness) -> JSONResponse:
    status = HTTPStatus.OK if body.status is ReadinessStatus.ready else HTTPStatus.SERVICE_UNAVAILABLE
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"))


def make_probes_router(ready_check: ReadyCheck, live_check: LiveCheck | None = None) -> APIRouter:
    """Build the probe router around the service's readiness check, and its liveness check when it has one."""
    probes = APIRouter(tags=["health"])

    @probes.get("/livez", response_model=Liveness)
    async def livez() -> JSONResponse:
        # async, not sync def: the probe must run ON the event loop, never queued behind the
        # blocking data-plane threadpool — no I/O happens here, so there is nothing to yield for.
        alive = live_check is None or live_check()
        return JSONResponse(status_code=HTTPStatus.OK if alive else HTTPStatus.SERVICE_UNAVAILABLE, content=Liveness().model_dump(mode="json") if alive else {})

    @probes.get("/readyz", response_model=Readiness)
    async def readyz(request: Request) -> JSONResponse:
        state = request.app.state
        # Drain first: once the lifespan flips the drain flag, that is the answer regardless of
        # anything else, and the check must never touch a dependency that is already closing.
        #
        # Both reads go through `lakehouse.lifecycle`, which is also what sets them — the flag
        # names were an untyped convention between this handler and eleven hand-rolled lifespans,
        # and five apps never held up their end (SK-09).
        if is_draining(state):
            return _serve(Readiness(status=ReadinessStatus.shutting_down))
        if not is_started(state):
            return _serve(Readiness(status=ReadinessStatus.starting))
        return _serve(await ready_check(request))

    return probes
