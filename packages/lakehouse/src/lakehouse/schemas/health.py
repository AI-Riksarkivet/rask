"""Probe response models for ``/livez`` and ``/readyz``."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class LivenessStatus(StrEnum):
    ok = "ok"


class ReadinessStatus(StrEnum):
    starting = "starting"
    ready = "ready"
    #: Serving, while a dependency the service rides through does not answer (`lakehouse.health`): a 200 that keeps the
    #: pod in rotation and names the dependency in ``components``, for a dashboard or an alert to act on.
    degraded = "degraded"
    #: Started, not draining, but a dependency or a fact the service cannot serve without does not hold: a 503 that pulls
    #: the pod from rotation without failing liveness.
    unhealthy = "unhealthy"
    shutting_down = "shutting_down"


class Liveness(BaseModel):
    status: LivenessStatus = LivenessStatus.ok


class Readiness(BaseModel):
    status: ReadinessStatus
    # Each component's verdict (e.g. {"store": "healthy"}), or why it does not hold. Defaults empty so the lifecycle's
    # answers, ``Readiness(status=ReadinessStatus.starting)``, stay constructible.
    components: dict[str, str] = Field(default_factory=dict)
