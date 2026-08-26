"""controlplane lifespan — build the auth door's dependencies, and nothing else.

This service had NO lifespan: it is stateless (it reads the k8s API per request) and
`make_service_app` gave it the shared default. It needs one now for exactly one reason — the
verifier and the FGA client must exist on `app.state` before a governed route runs, and building an
`OIDCVerifier` fetches discovery, so it cannot happen at import.

Without it the failure is quiet and total: the settings bind, the router declares its dependency, and
every request answers 503 "Authentication is enabled but unavailable". Fail-closed and correct, and
also a service that does nothing. Measured on the live estate 2026-08-26 — that is precisely what
shipped when the settings and the gate landed without this file.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI

from controlplane.dependencies import get_controlplane_settings
from service_kit.config import Settings
from service_kit.governed.auth_lifespan import attach_auth


def make_lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        await attach_auth(app, get_controlplane_settings(), service="controlplane")
        yield

    return lifespan
