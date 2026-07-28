"""ray service lifespan — build the dashboard HTTP client + the Ray Job SDK client
on app.state. No DB/Lance/S3/orchestrator. Tolerant of an unreachable dashboard
(build_client returns None)."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
from anyio import to_thread
from fastapi import FastAPI

from ray_kit import build_client
from ray_kit.auth import auth_headers
from service_kit.config import Settings


log = logging.getLogger(__name__)


def make_lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        # auth_headers(): Bearer token as client-default headers when the cluster runs
        # RAY_AUTH_MODE=token (RASK_RAY_AUTH_TOKEN / RAY_AUTH_TOKEN); {} otherwise. The
        # proxy strips inbound Authorization so this default is what reaches Ray.
        app.state.http = httpx.AsyncClient(timeout=settings.http_timeout, headers=auth_headers())
        app.state.ray_client = await to_thread.run_sync(build_client, settings.ray_dashboard_url)
        log.info("startup_complete")
        try:
            yield
        finally:
            await app.state.http.aclose()
            log.info("shutdown_complete")

    return lifespan
