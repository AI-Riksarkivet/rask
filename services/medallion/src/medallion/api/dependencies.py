"""Shared FastAPI dependencies (Annotated type aliases) for the medallion apps."""

from __future__ import annotations

from typing import Annotated

import httpx
from dapr.aio.clients import DaprClient
from fastapi import Depends, Request
from openfga_sdk import OpenFgaClient

from medallion.core.config import MedallionSettings, get_settings


SettingsDep = Annotated[MedallionSettings, Depends(get_settings)]


def get_dapr(request: Request) -> DaprClient:
    """The Dapr client (local sidecar) built once in the app lifespan."""
    return request.app.state.dapr


DaprClientDep = Annotated[DaprClient, Depends(get_dapr)]


def get_fga_client(request: Request) -> OpenFgaClient | None:
    """The wired OpenFGA client from app.state, or ``None`` when FGA isn't provisioned."""
    return getattr(request.app.state, "fga", None)


FgaClientDep = Annotated[OpenFgaClient | None, Depends(get_fga_client)]


def get_catalog_http(request: Request) -> httpx.Client | None:
    """The shared catalog client from the lifespan, or ``None`` where none was built.

    ``None`` rather than raising: the helpers that take it fall back to a per-call client, so a context
    without a wired app (a test, a script) still works. Injected the same way `get_dapr` is, so nothing
    reaches into `app.state` from a handler.
    """
    return getattr(request.app.state, "catalog_http", None)


CatalogHttpDep = Annotated["httpx.Client | None", Depends(get_catalog_http)]
