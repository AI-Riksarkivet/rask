"""Application lifespan — build once, dispose once.

Resources are stashed on `app.state` so dependencies pull them without
recreating per request. Tolerant of missing optional deps (HCP, LanceDB
tables, batches.db) so tests run offline.

If `RASK_ORCHESTRATOR_ENABLED=1`, also start the orchestrator loop as a
lifespan-managed `asyncio.Task` — see `viewer/services/orchestrator_loop.py`.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from typing import TYPE_CHECKING

import httpx
import lancedb
from anyio import to_thread
from fastapi import FastAPI
from lancedb.db import AsyncConnection
from lancedb.table import AsyncTable

from storage import s3_client
from viewer.core.config import Settings
from viewer.core.db import make_engine, make_sessionmaker
from viewer.services.orchestrator_loop import run_loop as run_orchestrator_loop
from viewer.services.ray_dashboard import build_client as build_ray_client


if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


log = logging.getLogger(__name__)


def _build_s3(settings: Settings) -> "S3Client | None":
    if not settings.hcp_endpoint:
        return None
    return s3_client(endpoint=settings.hcp_endpoint)


async def _open_lance_table(db: AsyncConnection, name: str) -> AsyncTable | None:
    try:
        return await db.open_table(name)
    except (OSError, RuntimeError, ValueError) as exc:
        log.warning("could not open lancedb table %s: %s", name, exc)
        return None


async def _open_lancedb(
    settings: Settings,
) -> tuple[AsyncConnection | None, AsyncTable | None, AsyncTable | None]:
    storage_options = settings.lance_storage_options()
    if storage_options is None:
        log.info("lancedb skipped — HCP credentials not configured")
        return None, None, None
    try:
        db = await lancedb.connect_async(settings.lance_db_uri, storage_options=storage_options)
    except (OSError, RuntimeError) as exc:
        log.warning("could not connect to lancedb at %s: %s", settings.lance_db_uri, exc)
        return None, None, None
    lines = await _open_lance_table(db, settings.lines_table)
    catalog = await _open_lance_table(db, settings.catalog_table)
    return db, lines, catalog


def make_lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.startup_complete = False
        app.state.shutting_down = False

        app.state.http = httpx.AsyncClient(timeout=settings.http_timeout)
        app.state.s3 = _build_s3(settings)
        app.state.lance_db, app.state.lines_tbl, app.state.catalog_tbl = await _open_lancedb(settings)
        app.state.ray_client = await to_thread.run_sync(build_ray_client, settings.ray_dashboard_url)

        app.state.db_engine = make_engine(settings)
        app.state.db_sessionmaker = make_sessionmaker(app.state.db_engine)

        orchestrator_task: asyncio.Task[None] | None = None
        if settings.orchestrator_enabled:
            orchestrator_task = asyncio.create_task(run_orchestrator_loop(app), name="orchestrator-loop")

        app.state.startup_complete = True
        log.info("startup_complete")

        try:
            yield
        finally:
            app.state.shutting_down = True
            if orchestrator_task is not None:
                orchestrator_task.cancel()
                with suppress(asyncio.CancelledError):
                    await orchestrator_task
            await app.state.http.aclose()
            if app.state.lines_tbl is not None:
                app.state.lines_tbl.close()
            if app.state.catalog_tbl is not None:
                app.state.catalog_tbl.close()
            if app.state.lance_db is not None:
                app.state.lance_db.close()
            await app.state.db_engine.dispose()
            log.info("shutdown_complete")

    return lifespan
