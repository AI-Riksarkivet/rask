"""Shared FastAPI dependencies for the compaction service (Annotated type aliases)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from maintenance.core.config import CompactionSettings, get_settings
from maintenance.core.lineage_emit import MaintenanceEmitter


SettingsDep = Annotated[CompactionSettings, Depends(get_settings)]


def get_lineage_emitter(request: Request) -> MaintenanceEmitter:
    """The process-wide lineage emitter built in the lifespan (a no-op when emission is disabled)."""
    return request.app.state.lineage_emitter


LineageEmitterDep = Annotated[MaintenanceEmitter, Depends(get_lineage_emitter)]
