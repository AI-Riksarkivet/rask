"""Annotator service entry — the lance-ns thin-``main.py`` template.

Module-level ``app``; ALL construction in ``lifespan`` onto ``app.state``
(importing this module does zero I/O). Problem+json handlers, CORS middleware,
``/livez`` + ``/readyz`` gated on ``startup_complete``/``shutting_down``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from common.core.handlers import register_handlers
from common.core.middleware import register_middleware
from common.core.probes import router as probes_router
from common.obs import configure_app_logging
from common.state import AppState, dataset_handle
from fastapi import FastAPI

from annotator.api.v1.router import router as api_router
from annotator.core.config import get_annotator_settings


logger = logging.getLogger(__name__)

configure_app_logging()  # INFO audit/lifecycle logs reach OTLP (lance-ns obs contract)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_annotator_settings()
    state = AppState(settings=settings, http=httpx.Client())
    app.state.resources = state
    try:
        handle = dataset_handle(state)  # fail-fast open of the default descriptor
        logger.info("annotator: default dataset %s ready (%d tables)", handle.id, len(handle.descriptor.tables))
    except Exception:
        # /livez stays green; per-request resolution surfaces the problem as a domain 404.
        logger.exception("annotator: default dataset failed to open — serving degraded")
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


app = FastAPI(title="lance-media annotator", lifespan=lifespan)
register_handlers(app)
register_middleware(app, get_annotator_settings())
app.include_router(probes_router)
app.include_router(api_router)


def run() -> None:
    import uvicorn

    s = get_annotator_settings()
    uvicorn.run("annotator.main:app", host=s.host, port=s.service_port)
