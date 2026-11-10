"""Shared FastAPI dependencies for the maintenance service (Annotated type aliases)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from maintenance.core.config import MaintenanceSettings, get_settings
from maintenance.services.queue import TaskQueue


SettingsDep = Annotated[MaintenanceSettings, Depends(get_settings)]


def get_task_queue(request: Request) -> TaskQueue | None:
    """The work queue the lifespan connected for the write-event lane's publishes, or ``None`` on a stack with no bus."""
    return getattr(request.app.state, "task_queue", None)


TaskQueueDep = Annotated[TaskQueue | None, Depends(get_task_queue)]
