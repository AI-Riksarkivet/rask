"""core FastAPI app — factory + uvicorn entry (post-P7a transitional husk).

Importing this module instantiates a module-level `app = create_app()` so
`uvicorn core.main:app` works out of the box. Tests call `create_app()` to build a fresh app with
their own env. The husk serves health + the EAD catalog search only; it retires with the R6/R20
media wave (docs/architecture/lance-ns-merge.md P7d).
"""

import argparse
import logging
import os
import sys

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles

from core.api.v1.endpoints import spa
from core.api.v1.router import api_router
from core.lifespan import make_lifespan
from service_kit.config import Settings
from service_kit.exceptions import register_handlers
from service_kit.middleware import register_middleware
from storage import derive_hcp_creds


_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_PORT = 8888


def _generate_unique_id(route: APIRoute) -> str:
    """Build OpenAPI operation IDs as `<tag>-<name>` so generated TS clients read cleanly."""
    if route.tags:
        return f"{route.tags[0]}-{route.name}"
    return route.name


def _setup_logging() -> None:
    """Route the app's own loggers (`core.*`) to stdout at the configured level.

    Honours RASK_LOG_LEVEL (default INFO). Idempotent: tests call create_app() repeatedly.
    """
    level = os.environ.get("RASK_LOG_LEVEL", "INFO").upper()
    logger = logging.getLogger("core")
    logger.setLevel(level)
    if not any(h.get_name() == "rask-stdout" for h in logger.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.set_name("rask-stdout")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s — %(message)s"))
        logger.addHandler(handler)
    logger.propagate = False


def create_app() -> FastAPI:
    _setup_logging()
    load_dotenv()
    derive_hcp_creds()
    settings = Settings.model_validate({})

    app = FastAPI(
        title="core",
        version="0.1.0",
        lifespan=make_lifespan(settings),
        docs_url=f"{settings.api_prefix}/docs",
        redoc_url=f"{settings.api_prefix}/redoc",
        openapi_url=f"{settings.api_prefix}/openapi.json",
        generate_unique_id_function=_generate_unique_id,
    )
    register_handlers(app)
    register_middleware(app, settings)

    app.include_router(api_router, prefix=settings.api_prefix)

    if settings.resolved_spa_build.is_dir():
        build = settings.resolved_spa_build
        app.mount("/_app", StaticFiles(directory=build / "_app"), name="spa-app")
        app.include_router(spa.router)

    return app


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="core FastAPI server")
    parser.add_argument("--host", default=_DEFAULT_HOST)
    parser.add_argument("--port", "-p", type=int, default=_DEFAULT_PORT)
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload (dev)")
    args = parser.parse_args()
    uvicorn.run("core.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
