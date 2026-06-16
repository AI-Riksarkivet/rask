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
from service_kit.dependencies import SettingsDep, get_settings  # noqa: F401  (re-exported for core endpoints)
from service_kit.exceptions import ServiceUnavailableError
from storage import S3Client


def get_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.http


def get_s3(request: Request) -> S3Client:
    s3 = request.app.state.s3
    if s3 is None:
        raise ServiceUnavailableError("S3 client not configured (HCP_ENDPOINT missing)")
    return s3


def get_catalog_tbl(request: Request) -> AsyncTable | None:
    return request.app.state.catalog_tbl


def get_ray_client(request: Request) -> JobSubmissionClient | None:
    return request.app.state.ray_client


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
