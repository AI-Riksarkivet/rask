"""The frontend-facing health badge — process liveness only.

Deliberately not a readiness that probes the sidecar or the state store: this is the path the chart's
default `healthPath` (`/api/health`) points at, and a probe that fails when a DEPENDENCY is briefly
unreachable turns a blip into a restart loop. The operational pair (`/livez` + `/readyz`, root-mounted
so a supervisor need not know this service's api prefix) is where per-component reporting lives.

The response model is the estate-wide `service_kit.schemas.health.Liveness` — one probe body for every
service, one place to change it.
"""

from fastapi import APIRouter

from service_kit.schemas.health import Liveness


router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> Liveness:
    # async, not sync def: a liveness probe must run ON the event loop, never queued behind the
    # blocking threadpool — else it fails exactly when the pod is busiest.
    return Liveness()
