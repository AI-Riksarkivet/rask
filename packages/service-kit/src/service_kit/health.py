"""The estate-wide ``/health`` liveness badge router — the frontend-facing probe the chart's
default ``healthPath`` (``/api/health``) points at.

Distinct from :mod:`service_kit.probes` (the operational ``/livez`` + ``/readyz`` pair): this is a
single dependency-free liveness route whose body is the shared
:class:`service_kit.schemas.health.Liveness` model. Liveness ONLY, deliberately not a readiness that
probes a dependency — a probe that fails when a dependency is briefly unreachable turns a blip into a
restart loop across every pod that touches it.

``make_service_app`` supplies no health route on purpose; every service mounts this one explicitly.
That mount is load-bearing: an ingest deploy once stuck a pod at 1/2 forever because the app passed no
health router and the chart's ``/api/health`` probe 404'd. Each service keeps its own ``health.py`` as
a one-line re-export so the per-service rationale (why liveness, not readiness) stays where it is read.
"""

from __future__ import annotations

from fastapi import APIRouter

from service_kit.schemas.health import Liveness


def make_health_router() -> APIRouter:
    """Build the ``/health`` liveness badge router."""
    router = APIRouter(tags=["health"])

    @router.get("/health")
    async def health() -> Liveness:
        # async, not sync def: a liveness probe must run ON the event loop, never queued behind the
        # blocking threadpool — else it fails exactly when the pod is busiest.
        return Liveness()

    return router
