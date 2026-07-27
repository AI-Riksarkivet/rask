"""Shared DI types.

Each `*Dep` is an `Annotated[T, Depends(...)]` so route signatures stay terse.
Resources come from `app.state` (set in lifespan), never module-level globals.
"""

from collections.abc import AsyncIterator
from typing import Annotated

import httpx
from fastapi import Depends, Request
from lancedb.table import AsyncTable
from sqlmodel.ext.asyncio.session import AsyncSession

from ray_kit import JobSubmissionClient
from ray_kit import build_client as build_ray_client
from service_kit.dependencies import SettingsDep, get_settings  # noqa: F401  (re-exported for core endpoints)
from service_kit.exceptions import ServiceUnavailableError
from storage import S3Client


def get_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.http


def get_s3(request: Request) -> S3Client:
    s3 = request.app.state.s3
    if s3 is None:
        raise ServiceUnavailableError("S3 client not configured (set RASK_S3_ENDPOINT_URL)")
    return s3


def get_catalog_tbl(request: Request) -> AsyncTable | None:
    return request.app.state.catalog_tbl


def get_ray_client(request: Request) -> JobSubmissionClient | None:
    # The client is built once in the lifespan, but there's no startup ordering
    # guarantee vs the Ray head: a freshly-provisioned project's core-api boots
    # before its RayService exists, so build_client returns None until the
    # dashboard is reachable. Rebuild lazily when still None and cache a live
    # client, so /chunks/{id}/submit recovers on its own with no pod restart.
    # (Mirrors ray_api's get_ray_client; FastAPI runs sync deps in a threadpool,
    # so the blocking build doesn't stall the event loop.)
    client = request.app.state.ray_client
    if client is None:
        client = build_ray_client(request.app.state.settings.ray_dashboard_url)
        request.app.state.ray_client = client
    return client


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    sessionmaker = request.app.state.db_sessionmaker
    async with sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


HttpDep = Annotated[httpx.AsyncClient, Depends(get_http)]
S3Dep = Annotated[S3Client, Depends(get_s3)]
CatalogTblDep = Annotated[AsyncTable | None, Depends(get_catalog_tbl)]
RayClientDep = Annotated[JobSubmissionClient | None, Depends(get_ray_client)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
