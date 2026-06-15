"""Shared factory for the per-domain backend services.

Every backend is a thin composition over the existing viewer code: it reuses
`viewer.core.lifespan.make_lifespan` (which builds all resources tolerantly —
missing HCP/Lance/DB just yield `None`) and includes only the routers it owns.
The viewer package is imported as a library; we deliberately do NOT import
`viewer.main`, which would instantiate a full viewer app as an import side effect.
"""

import logging
import os
import sys
from collections.abc import Sequence

from dotenv import load_dotenv
from fastapi import APIRouter, FastAPI

from storage import derive_hcp_creds
from viewer.core.config import Settings
from viewer.core.exceptions import register_handlers
from viewer.core.lifespan import make_lifespan
from viewer.core.middleware import register_middleware


def _setup_logging() -> None:
    """Send `viewer.*` and `backends.*` loggers to stdout (mirrors viewer.main)."""
    level = os.environ.get("RASK_LOG_LEVEL", "INFO").upper()
    for name in ("viewer", "backends"):
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


def make_service_app(*, title: str, routers: Sequence[APIRouter], proxy_router: APIRouter | None = None) -> FastAPI:
    """Build a backend FastAPI app sharing the viewer's lifespan and config.

    `routers` are mounted under `settings.api_prefix`; `proxy_router` (the Ray
    Serve proxy) is mounted at the root, matching `viewer.main`.
    """
    _setup_logging()
    settings = build_settings()

    app = FastAPI(
        title=title,
        version="0.1.0",
        lifespan=make_lifespan(settings),
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

    return app
