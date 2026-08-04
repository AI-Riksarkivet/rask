"""Shared FastAPI dependencies for the maintenance service (Annotated type aliases)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Request

from maintenance.core.config import MaintenanceSettings, get_settings
from maintenance.core.lineage_emit import MaintenanceEmitter


SettingsDep = Annotated[MaintenanceSettings, Depends(get_settings)]


def get_lineage_emitter(request: Request) -> MaintenanceEmitter:
    """The process-wide lineage emitter built in the lifespan (a no-op when emission is disabled)."""
    return request.app.state.lineage_emitter


LineageEmitterDep = Annotated[MaintenanceEmitter, Depends(get_lineage_emitter)]


def get_fga_client(request: Request) -> Any:  # noqa: ANN401 — OpenFgaClient | None; the SDK ships no protocol
    """The read-only OpenFGA client built in the lifespan, or ``None`` when FGA is off/unwired.

    ``None`` is a legitimate value, not an error: the reconciler degrades that category to UNAVAILABLE
    with a reason rather than failing the whole report. A drift report that 500s because one of three
    stores is unreachable tells you nothing about the other two.
    """
    return getattr(request.app.state, "fga_client", None)


FgaClientDep = Annotated[Any, Depends(get_fga_client)]


def get_s3_client(request: Request) -> Any:  # noqa: ANN401 — boto3 client has no public stub
    """The boto3 client the reconciler lists buckets with, or ``None`` when unwired (→ UNAVAILABLE)."""
    return getattr(request.app.state, "s3_client", None)


S3ClientDep = Annotated[Any, Depends(get_s3_client)]
