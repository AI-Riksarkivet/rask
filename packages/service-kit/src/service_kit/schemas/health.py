"""Probe response models for ``/livez`` and ``/readyz``."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class LivenessStatus(StrEnum):
    ok = "ok"


class ReadinessStatus(StrEnum):
    starting = "starting"
    ready = "ready"
    shutting_down = "shutting_down"
    #: Started, not draining, but a hard dependency is unhealthy (e.g. lineage's AGE
    #: pool/graph) — a 503 that pulls the pod from rotation without failing liveness.
    degraded = "degraded"


class Liveness(BaseModel):
    status: LivenessStatus = LivenessStatus.ok


class Readiness(BaseModel):
    status: ReadinessStatus
    # Optional per-component status map (e.g. {"db": "healthy"}); only the
    # ready/shutting-down responses populate it. Defaults empty so the gating
    # responses ``Readiness(status=ReadinessStatus.starting)`` stay constructible.
    components: dict[str, str] = Field(default_factory=dict)
