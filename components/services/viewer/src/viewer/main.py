"""Viewer FastAPI app — factory + uvicorn entry.

Importing this module instantiates a module-level `app = create_app()` so
`uvicorn viewer.main:app` / `fastapi run viewer.main:app` work out of the box.
Tests call `create_app()` to build a fresh app with their own env.
"""

import argparse
import os

import uvicorn
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles

from dotenv import load_dotenv

from storage import derive_hcp_creds
from viewer.api.v1.endpoints import ray, spa
from viewer.api.v1.router import api_router
from viewer.core.config import Settings
from viewer.core.exceptions import register_handlers
from viewer.core.lifespan import make_lifespan
from viewer.core.middleware import register_middleware


_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_PORT = 8888


def _generate_unique_id(route: APIRoute) -> str:
    """Build OpenAPI operation IDs as `<tag>-<name>` so generated TS clients read cleanly."""
    if route.tags:
        return f"{route.tags[0]}-{route.name}"
    return route.name


def create_app() -> FastAPI:
    load_dotenv()
    derive_hcp_creds()
    settings = Settings()  # type: ignore[call-arg]  # values loaded from env at runtime

    app = FastAPI(
        title="viewer",
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
    app.include_router(ray.proxy_router)

    if settings.resolved_spa_build.is_dir():
        build = settings.resolved_spa_build
        app.mount("/_app", StaticFiles(directory=build / "_app"), name="spa-app")
        app.include_router(spa.router)

    return app


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="viewer FastAPI server")
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

    uvicorn.run("viewer.main:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
