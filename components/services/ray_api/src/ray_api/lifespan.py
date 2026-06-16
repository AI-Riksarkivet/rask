"""ray-api lifespan — build the dashboard HTTP client + the Ray Job SDK client
on app.state. No DB/Lance/S3/orchestrator. Tolerant of an unreachable dashboard
(build_client returns None)."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
from anyio import to_thread
from fastapi import FastAPI

from ray_kit import build_client
from service_kit.config import Settings


log = logging.getLogger(__name__)


def make_lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        app.state.http = httpx.AsyncClient(timeout=settings.http_timeout)
        app.state.ray_client = await to_thread.run_sync(build_client, settings.ray_dashboard_url)
        log.info("startup_complete")
        try:
            yield
        finally:
            await app.state.http.aclose()
            log.info("shutdown_complete")

    return lifespan
