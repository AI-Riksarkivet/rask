"""The flows service's health endpoint — process liveness only.

Liveness, deliberately not a readiness that probes Ray Serve: a probe that fails when a MODEL is
unreachable turns "no GPU today" into a restart loop, and a flow service with no Serve behind it can
still serve its catalog, validate a graph, and refuse a model node honestly.

The response model is the estate-wide ``service_kit.schemas.health.Liveness`` — one probe body for
every service, one place to change it. `make_service_app` supplies no health route; every service
mounts its own, and the chart probes ``/api/health``.
"""

from fastapi import APIRouter

from service_kit.schemas.health import Liveness


router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> Liveness:
    # async, not sync def: a liveness probe must run ON the event loop, never queued behind the
    # blocking threadpool — else it fails exactly when the pod is busiest.
    return Liveness()
