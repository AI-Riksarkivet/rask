"""Operational health endpoints (``/livez``, ``/readyz``) shared by both medallion apps.

Cheap liveness/readiness probes — no dependency calls (a Dapr/broker blip must not pull the pod; the
sidecar owns publish retry; no DLQ). Mounted without an API-version prefix on the mover and the producer.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse


router = APIRouter(tags=["health"])


@router.get("/livez")
async def livez() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Gate on the lifespan flags (parity with catalog/lineage) so k8s pulls the pod during boot + drain,
    instead of a static 200 that routes traffic to a not-yet-ready (or terminating) sidecar-subscriber."""
    state = request.app.state
    if getattr(state, "shutting_down", False):
        return JSONResponse(status_code=503, content={"status": "shutting_down"})
    if not getattr(state, "startup_complete", False):
        return JSONResponse(status_code=503, content={"status": "starting"})
    return JSONResponse(status_code=200, content={"status": "ready"})
