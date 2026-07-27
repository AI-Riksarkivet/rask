"""Application lifespan — build once, dispose once.

Resources are stashed on `app.state` so dependencies pull them without recreating per request.
Tolerant of missing optional deps (LanceDB tables) so tests run offline.

Post-P7a the core package is a transitional husk: health + the EAD catalog search. The batches DB,
the Ray client, and the orchestrator loop are gone — ingestion is the medallion IIIF producer and HTR
runs as cascade compute on the lakehouse (docs/architecture/lance-ns-merge.md P7). The husk retires
with the R6/R20 media wave, when lance `search` serves a catalog-governed EAD table.
"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
import lancedb
from fastapi import FastAPI
from lancedb.db import AsyncConnection
from lancedb.table import AsyncTable

from service_kit.config import Settings


log = logging.getLogger(__name__)


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
        log.info("lancedb skipped — S3 credentials not configured")
        return None, None
    try:
        db = await lancedb.connect_async(settings.lance_db_uri, storage_options=storage_options)
    except (OSError, RuntimeError) as exc:
        log.warning(f"could not connect to lancedb at {settings.lance_db_uri}: {exc}")
        return None, None
    catalog = await _open_lance_table(db, settings.catalog_table)
    return db, catalog


def make_lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.http = httpx.AsyncClient(timeout=settings.http_timeout)
        app.state.lance_db, app.state.catalog_tbl = await _open_lancedb(settings)

        log.info("startup_complete")

        try:
            yield
        finally:
            await app.state.http.aclose()
            if app.state.catalog_tbl is not None:
                app.state.catalog_tbl.close()
            if app.state.lance_db is not None:
                app.state.lance_db.close()
            log.info("shutdown_complete")

    return lifespan
