"""Viewer service entry — the lance-ns thin-``main.py`` template.

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

from service_kit.exceptions import register_handlers
from service_kit.governed import fga
from service_kit.governed.oidc import OIDCVerifier
from service_kit.media.middleware import register_middleware
from service_kit.media.state import AppState, dataset_handle
from service_kit.obs import configure_app_logging
from service_kit.probes import router as probes_router
from viewer.api.v1.router import router as api_router
from viewer.core.config import get_viewer_settings


logger = logging.getLogger(__name__)

configure_app_logging()  # INFO audit/lifecycle logs reach OTLP (lance-ns obs contract)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_viewer_settings()
    state = AppState(settings=settings, http=httpx.Client())
    app.state.resources = state
    try:
        handle = dataset_handle(state)  # fail-fast open of the default descriptor
        logger.info("viewer: default dataset %s ready (%d tables)", handle.id, len(handle.descriptor.tables))
    except Exception:
        # /livez stays green; per-request resolution surfaces the problem as a domain 404.
        logger.exception("viewer: default dataset failed to open — serving degraded")
    # AUTHN/AUTHZ. Both default OFF, and the service behaves exactly as it always did when they are
    # — the corpus list is only gated once someone turns FGA on. Built here, never at import: an
    # OIDCVerifier fetches discovery, so module scope would make importing this file do I/O.
    #
    # A failure to BUILD is logged and left non-fatal on purpose: the dependency then finds no
    # verifier/client on app.state and answers 503, which is the honest reading. Falling back to a
    # permissive checker instead would turn a broken authorization layer into an open one.
    if settings.oidc_enabled and settings.oidc_issuer and settings.oidc_audience:
        try:
            app.state.oidc = OIDCVerifier(
                settings.oidc_issuer,
                settings.oidc_audience,
                settings.oidc_cache_ttl,
                leeway=settings.oidc_leeway,
                allow_insecure=settings.oidc_allow_insecure,
                discovery_overrides=({settings.oidc_issuer: settings.oidc_discovery_url} if settings.oidc_discovery_url else None),
            )
            logger.info("viewer: OIDC verifier ready (issuer=%s)", settings.oidc_issuer)
        except Exception:
            logger.exception("viewer: OIDC verifier failed to build — guarded routes will 503")
    if settings.fga_enabled:
        try:
            store_id, model_id = settings.fga_store_id, settings.fga_model_id
            if not (store_id and model_id):
                store_id, model_id = await fga.provision(settings.fga_api_url)
                logger.info("viewer: openfga provisioned store=%s model=%s", store_id, model_id)
            app.state.fga = fga.make_client(settings.fga_api_url, store_id, model_id, timeout_seconds=settings.fga_timeout_seconds)
            logger.info("viewer: FGA client ready (%s)", settings.fga_api_url)
        except Exception:
            logger.exception("viewer: FGA client failed to build — authorized routes will 503")

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


app = FastAPI(title="lance-media viewer", lifespan=lifespan)
register_handlers(app)
register_middleware(app, get_viewer_settings())
app.include_router(probes_router)
app.include_router(api_router)


def run() -> None:
    import uvicorn

    s = get_viewer_settings()
    uvicorn.run("viewer.main:app", host=s.host, port=s.service_port)


def create_viewer_state(settings=None) -> AppState:
    """Standalone viewer state — registry + HTTP pool (the test/tooling seam)."""
    from service_kit.media.config import get_settings

    return AppState(settings=settings or get_settings(), http=httpx.Client())


def create_viewer_app(settings=None, state: AppState | None = None) -> FastAPI:
    """Viewer app around shared or standalone state — the test/composition seam."""
    from service_kit.media.config import get_settings

    settings = settings or get_settings()
    test_app = FastAPI(title="viewer api")
    test_app.state.resources = state if state is not None else create_viewer_state(settings)
    register_handlers(test_app)
    register_middleware(test_app, settings)
    test_app.include_router(api_router)
    return test_app
