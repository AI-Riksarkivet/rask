"""The controlplane's health endpoint — process liveness only (the k8s reachability
check belongs to the projects reader, not this probe).

The response model is the estate-wide ``service_kit.schemas.health.Liveness``, not a
locally re-declared ``Health``: one probe body for every service, one place to change it.
"""

from fastapi import APIRouter

from service_kit.schemas.health import Liveness


router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> Liveness:
    # async, not sync def: a liveness probe must run ON the event loop, never queued
    # behind the blocking threadpool — else it fails exactly when the pod is busiest.
    return Liveness()
