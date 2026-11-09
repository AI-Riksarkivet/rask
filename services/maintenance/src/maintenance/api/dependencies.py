"""Shared FastAPI dependencies for the maintenance service (Annotated type aliases)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from lakehouse.control_emit import ControlEmitter, NoopControlEmitter
from maintenance.core.config import MaintenanceSettings, get_settings
from maintenance.core.lineage_emit import MaintenanceEmitter
from maintenance.services.queue import TaskQueue


SettingsDep = Annotated[MaintenanceSettings, Depends(get_settings)]


def get_lineage_emitter(request: Request) -> MaintenanceEmitter:
    """The process-wide lineage emitter built in the lifespan (a no-op when emission is disabled)."""
    return request.app.state.lineage_emitter


LineageEmitterDep = Annotated[MaintenanceEmitter, Depends(get_lineage_emitter)]


def get_control_emitter(request: Request) -> ControlEmitter:
    """The control-plane emitter built in the lifespan (a no-op when emission is disabled/unwired).

    Falls back to the no-op rather than ``None`` so every call site is unconditionally safe — a purge
    that had to guard its own announcement would eventually forget to.
    """
    return getattr(request.app.state, "control_emitter", None) or NoopControlEmitter()


ControlEmitterDep = Annotated[ControlEmitter, Depends(get_control_emitter)]


def get_task_queue(request: Request) -> TaskQueue | None:
    """The work queue the lifespan connected, or ``None`` on a stack that runs no worker (no bus configured)."""
    return getattr(request.app.state, "task_queue", None)


TaskQueueDep = Annotated[TaskQueue | None, Depends(get_task_queue)]
