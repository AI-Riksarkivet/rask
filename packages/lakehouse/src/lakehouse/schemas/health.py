"""Probe response models for ``/livez`` and ``/readyz``."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class LivenessStatus(StrEnum):
    """What ``/livez`` answers: ``ok`` while the process lives, ``stalled`` once its own check fails."""

    ok = "ok"
    #: The process's own liveness check failed (its work no longer turns), answered 503 so the kubelet restarts it.
    stalled = "stalled"


class ReadinessStatus(StrEnum):
    """Where a service stands in its lifecycle, as ``/readyz`` answers it."""

    starting = "starting"
    ready = "ready"
    #: Serving, while a dependency the service rides through does not answer (`lakehouse.process.health`): a 200 that keeps the
    #: pod in rotation and names the dependency in ``components``, for a dashboard or an alert to act on.
    degraded = "degraded"
    #: Started, not draining, but a dependency or a fact the service cannot serve without does not hold: a 503 that pulls
    #: the pod from rotation without failing liveness.
    unhealthy = "unhealthy"
    shutting_down = "shutting_down"


class Liveness(BaseModel):
    """The body of ``/livez``."""

    status: LivenessStatus = LivenessStatus.ok


class Readiness(BaseModel):
    """The body of ``/readyz``: the service's status and each component's verdict."""

    status: ReadinessStatus
    # Each component's verdict (e.g. {"store": "healthy"}), or why it does not hold. Defaults empty so the lifecycle's
    # answers, ``Readiness(status=ReadinessStatus.starting)``, stay constructible.
    components: dict[str, str] = Field(default_factory=dict)


__all__ = [
    "Liveness",
    "LivenessStatus",
    "Readiness",
    "ReadinessStatus",
]
