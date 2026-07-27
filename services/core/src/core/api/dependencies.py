"""Shared DI types.

Each `*Dep` is an `Annotated[T, Depends(...)]` so route signatures stay terse.
Resources come from `app.state` (set in lifespan), never module-level globals.
"""

from typing import Annotated

import httpx
from fastapi import Depends, Request
from lancedb.table import AsyncTable

from service_kit.dependencies import SettingsDep, get_settings  # noqa: F401  (re-exported for core endpoints)


def get_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.http


def get_catalog_tbl(request: Request) -> AsyncTable | None:
    return request.app.state.catalog_tbl


HttpDep = Annotated[httpx.AsyncClient, Depends(get_http)]
CatalogTblDep = Annotated[AsyncTable | None, Depends(get_catalog_tbl)]
