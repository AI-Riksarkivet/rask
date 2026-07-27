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
