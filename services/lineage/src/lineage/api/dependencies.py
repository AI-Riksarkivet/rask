"""Shared FastAPI dependencies (Annotated type aliases) for the lineage service."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from lineage.core.config import LineageSettings, get_settings
from lineage.services.repository import LineageRepository


SettingsDep = Annotated[LineageSettings, Depends(get_settings)]


def get_repository(request: Request) -> LineageRepository:
    """The lineage repository, built once in the lifespan over the AGE pool."""
    return request.app.state.repository


RepositoryDep = Annotated[LineageRepository, Depends(get_repository)]


def get_publisher(request: Request) -> object | None:
    """The relay's Dapr publisher, or ``None`` when this deployment runs without the outbox.

    ``None`` is a real answer, not a missing dependency: the publisher exists only to re-publish drained
    events, and a deployment with no ``outbox_uri`` never drains. Returning it rather than raising keeps
    the drain's own guard the single place that decides whether a re-publish is possible.
    """
    return getattr(request.app.state, "dapr", None)


PublisherDep = Annotated[object | None, Depends(get_publisher)]
