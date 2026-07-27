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


class Liveness(BaseModel):
    status: LivenessStatus = LivenessStatus.ok


class Readiness(BaseModel):
    status: ReadinessStatus
    # Optional per-component status map (e.g. {"db": "healthy"}); only the
    # ready/shutting-down responses populate it. Defaults empty so the gating
    # responses ``Readiness(status=ReadinessStatus.starting)`` stay constructible.
    components: dict[str, str] = Field(default_factory=dict)
