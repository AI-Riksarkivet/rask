"""compute service lifespan — build the dashboard HTTP client + the Ray Job SDK client
on app.state. No DB/Lance/S3/orchestrator. Tolerant of an unreachable dashboard
(build_client returns None)."""

import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

import httpx
from anyio import to_thread
from fastapi import FastAPI

from compute.dependencies import get_compute_settings
from ray_kit import build_client
from ray_kit.auth import auth_headers
from service_kit.config import Settings
from service_kit.governed.auth_lifespan import attach_auth
from service_kit.governed.dapr_auth import assert_app_token_configured


log = logging.getLogger(__name__)


def make_lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        # auth_headers(): Bearer token as client-default headers when the cluster runs
        # RAY_AUTH_MODE=token (RASK_RAY_AUTH_TOKEN / RAY_AUTH_TOKEN); {} otherwise. The
        # proxy strips inbound Authorization so this default is what reaches Ray.
        app.state.http = httpx.AsyncClient(timeout=settings.http_timeout, headers=auth_headers())
        # Stamp the attempt so get_ray_client's negative cache dates from boot: if Ray is down now,
        # the first request waits out the cooldown rather than immediately restorming the dashboard.
        app.state.ray_client_last_attempt = time.monotonic()
        app.state.ray_client = await to_thread.run_sync(build_client, settings.ray_dashboard_url)
        # FAIL CLOSED ON THE APP TOKEN, like every sibling that hosts a sidecar-delivered route. The
        # prune binding (`pruner.py`) is guarded by `require_dapr_token`, which is a documented no-op
        # while `APP_API_TOKEN` is unset — safe only because this call turns that into a boot refusal
        # once Dapr is on. Measured on the deployed estate 2026-09-02 without it: the sidecar held the
        # token and stamped every delivery, the app container held none, and the guard compared each
        # delivery against an empty string — so a route that deletes terminal Ray jobs was open to
        # any pod in the namespace while reading as sidecar-only. A pod that refuses to start names
        # the missing variable; a pod that starts open names nothing.
        assert_app_token_configured(dapr_enabled=get_compute_settings().dapr_enabled)
        # THE DOOR'S OWN DEPENDENCIES. `compute.security` reads `app.state.oidc` / `app.state.fga`;
        # without this the settings are bound, the routes declare the dependency, and every request
        # answers 503 "Authentication is enabled but unavailable" — which is fail-CLOSED and correct,
        # and also means the service does nothing. Measured on the live estate: that is exactly what
        # shipped when the settings and the gate landed without this line.
        await attach_auth(app, get_compute_settings(), service="compute")
        log.info("startup_complete")
        try:
            yield
        finally:
            await app.state.http.aclose()
            log.info("shutdown_complete")

    return lifespan
