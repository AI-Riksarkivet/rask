"""The catalog's database for an app a test assembles from the catalog's doors: what the catalog's own lifespan opens
first, on the database ``RASK_DATABASE_*`` names (`tests.postgres_fixtures.catalog_database`), closed with the app."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from catalog.core.database import DatabaseSettings, open_database


@asynccontextmanager
async def catalog_database_lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Open the catalog's database onto ``app.state`` for the app's life."""
    database = open_database(DatabaseSettings())
    app.state.database = database
    try:
        yield
    finally:
        await database.dispose()
