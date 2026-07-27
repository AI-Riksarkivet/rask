"""search-api lifespan — open the Lance lines table + S3 client, expose on app.state.

Stateful in exactly one dimension (the lines table); no DB/Ray/orchestrator.
Tolerant of missing S3 creds / table so tests run offline (lines_tbl = None).
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import lancedb
from fastapi import FastAPI
from lancedb.db import AsyncConnection
from lancedb.table import AsyncTable

from service_kit.config import Settings
from storage import S3Client, s3_client


log = logging.getLogger(__name__)


def _build_s3(settings: Settings) -> S3Client | None:
    if not settings.s3_endpoint_url:
        return None
    return s3_client(endpoint=settings.s3_endpoint_url)


async def _open_lines_table(settings: Settings) -> tuple[AsyncConnection | None, AsyncTable | None]:
    storage_options = settings.lance_storage_options()
    if storage_options is None:
        log.info("lancedb skipped — S3 credentials not configured")
        return None, None
    try:
        db = await lancedb.connect_async(settings.lance_db_uri, storage_options=storage_options)
    except (OSError, RuntimeError) as exc:
        log.warning(f"could not connect to lancedb at {settings.lance_db_uri}: {exc}")
        return None, None
    try:
        tbl = await db.open_table(settings.lines_table)
    except (OSError, RuntimeError, ValueError) as exc:
        log.warning(f"could not open lancedb table {settings.lines_table}: {exc}")
        return db, None
    return db, tbl


def make_lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.s3 = _build_s3(settings)
        app.state.lance_db, app.state.lines_tbl = await _open_lines_table(settings)
        log.info("startup_complete")
        try:
            yield
        finally:
            if app.state.lines_tbl is not None:
                app.state.lines_tbl.close()
            if app.state.lance_db is not None:
                app.state.lance_db.close()
            log.info("shutdown_complete")

    return lifespan
