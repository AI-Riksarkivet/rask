"""Application lifespan — build once, dispose once.

Resources are stashed on `app.state` so dependencies pull them without
recreating per request. Tolerant of missing optional deps (HCP, LanceDB
tables, batches.db) so tests run offline.

`app.state.orchestrator_task` holds the orchestrator loop's `asyncio.Task`
(or None when stopped). Lifespan creates it on boot if `RASK_ORCHESTRATOR_AUTOSTART=1`;
the `/api/v1/orchestrator/start` and `/stop` endpoints flip it at runtime.
`create_orchestrator_task(app)` is the single factory both paths use.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress

import httpx
import lancedb
from anyio import to_thread
from fastapi import FastAPI
from lancedb.db import AsyncConnection
from lancedb.table import AsyncTable

from core.db import make_engine, make_sessionmaker
from core.services.orchestrator import run_loop as run_orchestrator_loop
from ray_kit import build_client as build_ray_client
from service_kit.config import Settings
from storage import S3Client, s3_client


log = logging.getLogger(__name__)


def _build_s3(settings: Settings) -> S3Client | None:
    if not settings.hcp_endpoint:
        return None
    return s3_client(endpoint=settings.hcp_endpoint)


async def _open_lance_table(db: AsyncConnection, name: str) -> AsyncTable | None:
    try:
        return await db.open_table(name)
    except (OSError, RuntimeError, ValueError) as exc:
        log.warning(f"could not open lancedb table {name}: {exc}")
        return None


async def _open_lancedb(
    settings: Settings,
) -> tuple[AsyncConnection | None, AsyncTable | None]:
    storage_options = settings.lance_storage_options()
    if storage_options is None:
        log.info("lancedb skipped — HCP credentials not configured")
        return None, None
    try:
        db = await lancedb.connect_async(settings.lance_db_uri, storage_options=storage_options)
    except (OSError, RuntimeError) as exc:
        log.warning(f"could not connect to lancedb at {settings.lance_db_uri}: {exc}")
        return None, None
    catalog = await _open_lance_table(db, settings.catalog_table)
    return db, catalog


def create_orchestrator_task(app: FastAPI) -> asyncio.Task[None]:
    """Spawn the orchestrator loop using the deps stashed on `app.state`.

    Single factory used by lifespan (autostart) and the `/start` endpoint
    (runtime control), so both paths produce identical tasks.
    """
    return asyncio.create_task(
        run_orchestrator_loop(
            settings=app.state.settings,
            sessionmaker=app.state.db_sessionmaker,
            ray_client=app.state.ray_client,
            http=app.state.http,
            s3=app.state.s3,
        ),
        name="orchestrator-loop",
    )


async def stop_orchestrator_task(app: FastAPI) -> None:
    """Cancel the orchestrator loop and clear `app.state.orchestrator_task`.

    Idempotent — calling when already stopped is a no-op. Used by the
    `/stop` endpoint AND by lifespan shutdown so the cancel-and-await
    sequence lives in one place.
    """
    task: asyncio.Task[None] | None = app.state.orchestrator_task
    if task is None:
        return
    if not task.done():
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    app.state.orchestrator_task = None


def is_orchestrator_running(app: FastAPI) -> bool:
    """True iff the orchestrator task exists and hasn't finished."""
    task: asyncio.Task[None] | None = app.state.orchestrator_task
    return task is not None and not task.done()


def make_lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.http = httpx.AsyncClient(timeout=settings.http_timeout)
        app.state.s3 = _build_s3(settings)
        app.state.lance_db, app.state.catalog_tbl = await _open_lancedb(settings)
        app.state.ray_client = await to_thread.run_sync(build_ray_client, settings.ray_dashboard_url)

        app.state.db_engine = make_engine(settings)
        app.state.db_sessionmaker = make_sessionmaker(app.state.db_engine)

        app.state.orchestrator_task = None
        if settings.orchestrator_autostart:
            app.state.orchestrator_task = create_orchestrator_task(app)

        log.info("startup_complete")

        try:
            yield
        finally:
            await stop_orchestrator_task(app)
            await app.state.http.aclose()
            if app.state.catalog_tbl is not None:
                app.state.catalog_tbl.close()
            if app.state.lance_db is not None:
                app.state.lance_db.close()
            await app.state.db_engine.dispose()
            log.info("shutdown_complete")

    return lifespan
