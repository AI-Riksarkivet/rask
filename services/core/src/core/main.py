"""core FastAPI app — factory + uvicorn entry.

Importing this module instantiates a module-level `app = create_app()` so
`uvicorn core.main:app` / `fastapi run core.main:app` work out of the box.
Tests call `create_app()` to build a fresh app with their own env.
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
from core.models.pipelines import PIPELINE_SPECS
from service_kit.config import PIPELINE_DISABLED, Settings
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

    Without this, `core.*` loggers have no handler, so Python's last-resort only
    emits WARNING+ to stderr — the orchestrator loop's INFO logs (ticks, submits,
    Ray-connect) silently vanish, leaving the scheduled job unobservable. Honours
    RASK_LOG_LEVEL (default INFO). Idempotent: tests call create_app() repeatedly.
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


def _validate_pipeline_settings(settings: Settings) -> None:
    """Fail fast if the configured slot pipelines aren't in the registry.

    The prefetch lane may be disabled (`RASK_PREFETCH_PIPELINE=none`) to run the
    orchestrator HTR-only; the HTR lane must always be a real pipeline.
    """
    if settings.prefetch_pipeline.lower() not in PIPELINE_DISABLED and settings.prefetch_pipeline not in PIPELINE_SPECS:
        raise ValueError(
            f"settings.prefetch_pipeline={settings.prefetch_pipeline!r} is not a registered pipeline; "
            f"choose from {sorted(PIPELINE_SPECS)} or a disable sentinel {sorted(PIPELINE_DISABLED)}"
        )
    if settings.htr_pipeline not in PIPELINE_SPECS:
        raise ValueError(f"settings.htr_pipeline={settings.htr_pipeline!r} is not a registered pipeline; choose from {sorted(PIPELINE_SPECS)}")


def create_app() -> FastAPI:
    _setup_logging()
    load_dotenv()
    derive_hcp_creds()
    settings = Settings.model_validate({})
    _validate_pipeline_settings(settings)

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
    parser.add_argument("--input", "-i", help="Input source URI (s3://bucket or filesystem path)")
    parser.add_argument("--output", "-o", help="Output source URI (s3://bucket or filesystem path)")
    parser.add_argument("--host", default=_DEFAULT_HOST)
    parser.add_argument("--port", "-p", type=int, default=_DEFAULT_PORT)
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload (dev)")
    args = parser.parse_args()

    if args.input:
        os.environ["RASK_VIEWER_INPUT"] = args.input
    if args.output:
        os.environ["RASK_VIEWER_OUTPUT"] = args.output

    if "RASK_VIEWER_INPUT" not in os.environ or "RASK_VIEWER_OUTPUT" not in os.environ:
        raise SystemExit("Set --input and --output (or RASK_VIEWER_INPUT/RASK_VIEWER_OUTPUT env vars).")

    uvicorn.run("core.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
