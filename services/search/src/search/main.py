"""Search service entry — the lance-ns thin-``main.py`` template.

Module-level ``app``; ALL construction in ``lifespan`` onto ``app.state``
(importing this module does zero I/O). Problem+json handlers, CORS middleware,
``/livez`` + ``/readyz`` gated on ``startup_complete``/``shutting_down``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from search.api.v1.router import router as api_router
from search.core.config import get_search_settings
from service_kit.media.handlers import register_handlers
from service_kit.media.middleware import register_middleware
from service_kit.media.probes import router as probes_router
from service_kit.media.state import AppState, dataset_handle
from service_kit.obs import configure_app_logging


logger = logging.getLogger(__name__)

configure_app_logging()  # INFO audit/lifecycle logs reach OTLP (lance-ns obs contract)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_search_settings()
    state = AppState(settings=settings, http=httpx.Client())
    app.state.resources = state
    try:
        handle = dataset_handle(state)  # fail-fast open of the default descriptor
        logger.info("search: default dataset %s ready (%d tables)", handle.id, len(handle.descriptor.tables))
    except Exception:
        # /livez stays green; per-request resolution surfaces the problem as a domain 404.
        logger.exception("search: default dataset failed to open — serving degraded")
    app.state.startup_complete = True
    app.state.shutting_down = False
    yield
    app.state.shutting_down = True
    for resource in (state.http, state.embedder, state.reranker):
        close = getattr(resource, "close", None)
        if close is not None:
            try:
                close()
            except Exception:
                logger.warning("error closing %s on shutdown", type(resource).__name__)


app = FastAPI(title="lance-media search", lifespan=lifespan)
register_handlers(app)
register_middleware(app, get_search_settings())
app.include_router(probes_router)
app.include_router(api_router)


def run() -> None:
    import uvicorn

    s = get_search_settings()
    uvicorn.run("search.main:app", host=s.host, port=s.service_port)


def create_search_app(state: AppState) -> FastAPI:
    """Search app around externally-opened state — the test/composition seam."""
    test_app = FastAPI(title="search api")
    test_app.state.resources = state
    register_handlers(test_app)
    test_app.include_router(api_router)
    return test_app
