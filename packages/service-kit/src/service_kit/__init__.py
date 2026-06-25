"""Shared factory for rask backend services (was backends/_common.py).

`make_service_app` builds a FastAPI app with shared config, exception handlers,
middleware, and logging. The lifespan is injectable: stateless services get the
minimal `default_lifespan` (settings only); services needing resources (DB, Lance,
Ray, S3) pass their own factory, e.g. `core.lifespan.make_lifespan`.
"""

import logging
import os
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Annotated

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, FastAPI, Request


if TYPE_CHECKING:
    from dapr.clients import DaprClient

from service_kit.config import Settings
from service_kit.exceptions import register_handlers
from service_kit.middleware import register_middleware
from service_kit.slash import SlashToleranceMiddleware
from storage import derive_hcp_creds


def _setup_logging() -> None:
    """Send `core.*` and `backends.*` loggers to stdout (mirrors core.main)."""
    level = os.environ.get("RASK_LOG_LEVEL", "INFO").upper()
    for name in ("core", "backends"):
        logger = logging.getLogger(name)
        logger.setLevel(level)
        if not any(h.get_name() == "rask-stdout" for h in logger.handlers):
            handler = logging.StreamHandler(sys.stdout)
            handler.set_name("rask-stdout")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s — %(message)s"))
            logger.addHandler(handler)
        logger.propagate = False


def build_settings() -> Settings:
    load_dotenv()
    derive_hcp_creds()
    return Settings.model_validate({})


def _import_dapr_client() -> "type[DaprClient]":
    # Lazy import: the dapr SDK is only required when RASK_DAPR_ENABLED is true.
    from dapr.clients import DaprClient

    return DaprClient


def build_dapr_client(settings: Settings) -> "DaprClient | None":
    """Dapr SDK client at the local sidecar, or None when Dapr is disabled."""
    if not settings.dapr_enabled:
        return None
    dapr_client_cls = _import_dapr_client()
    return dapr_client_cls(f"http://127.0.0.1:{settings.dapr_http_port}")


def get_dapr(request: Request) -> "DaprClient | None":
    return request.app.state.dapr


DaprClientDep = Annotated["DaprClient | None", Depends(get_dapr)]


def default_lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    """Minimal lifespan for stateless services: expose `settings` on `app.state`.

    Services that need resources (DB, Lance, Ray, S3) inject their own factory.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = settings
        yield

    return lifespan


type LifespanFactory = Callable[[Settings], Callable[[FastAPI], AbstractAsyncContextManager[None]]]


def make_service_app(
    *,
    title: str,
    routers: Sequence[APIRouter],
    proxy_router: APIRouter | None = None,
    lifespan: LifespanFactory | None = None,
) -> FastAPI:
    """Build a backend FastAPI app with shared config/handlers/middleware.

    `routers` are mounted under `settings.api_prefix`; `proxy_router` (the Ray
    Serve proxy) is mounted at the root, matching `core.main`. The lifespan
    defaults to the minimal `default_lifespan` unless `lifespan=` is passed.
    """
    _setup_logging()
    settings = build_settings()
    base_factory: LifespanFactory = lifespan if lifespan is not None else default_lifespan

    def lifespan_factory(s: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
        base = base_factory(s)

        @asynccontextmanager
        async def wrapped(app: FastAPI) -> AsyncIterator[None]:
            app.state.dapr = build_dapr_client(s)
            try:
                async with base(app):
                    yield
            finally:
                if app.state.dapr is not None:
                    app.state.dapr.close()

        return wrapped

    app = FastAPI(
        title=title,
        version="0.1.0",
        lifespan=lifespan_factory(settings),
        docs_url=f"{settings.api_prefix}/docs",
        redoc_url=f"{settings.api_prefix}/redoc",
        openapi_url=f"{settings.api_prefix}/openapi.json",
    )
    register_handlers(app)
    register_middleware(app, settings)

    for router in routers:
        app.include_router(router, prefix=settings.api_prefix)
    if proxy_router is not None:
        app.include_router(proxy_router)

    # Dapr drops trailing slashes; resolve the variant in-process instead of a
    # 307 redirect that would leak the service's in-pod address (see slash.py).
    app.router.redirect_slashes = False
    app.add_middleware(SlashToleranceMiddleware, routes_provider=lambda: app.router.routes)

    from service_kit.otel import setup_otel

    setup_otel(app, service_name=title, settings=settings)

    return app
