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
from dapr.ext.fastapi import DaprActor
from fastapi import FastAPI

from annotator.api.v1.router import router as api_router
from annotator.core.config import get_annotator_settings
from annotator.projects.actor import AnnotationTaskActor
from annotator.projects.project_actor import AnnotationProjectActor
from annotator.projects.tenant_actor import TenantProjectsActor
from service_kit.exceptions import register_handlers
from service_kit.governed import fga
from service_kit.governed.oidc import OIDCVerifier
from service_kit.media.middleware import register_middleware
from service_kit.media.state import AppState, dataset_handle
from service_kit.obs import configure_app_logging
from service_kit.probes import router as probes_router


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

    # Governed auth. Both are built here and read per-request off app.state; when either is enabled
    # but absent, `annotator.api.security` raises 503 rather than degrading to open access. A failure
    # is logged and the attribute left UNSET on purpose — a half-built auth layer must not look
    # configured, and 503 is the honest answer until it is fixed.
    if settings.oidc_enabled and settings.oidc_issuer and settings.oidc_audience:
        try:
            app.state.oidc = OIDCVerifier(
                settings.oidc_issuer,
                settings.oidc_audience,
                settings.oidc_cache_ttl,
                leeway=settings.oidc_leeway,
                allow_insecure=settings.oidc_allow_insecure,
                # Split-horizon (reverse-proxied IdP): fetch discovery/JWKS in-cluster while tokens
                # keep the public issuer string. Same wiring as the catalog, deliberately.
                discovery_overrides=({settings.oidc_issuer: settings.oidc_discovery_url} if settings.oidc_discovery_url else None),
            )
            logger.info("annotator: OIDC verifier ready (issuer=%s)", settings.oidc_issuer)
        except Exception:
            logger.exception("annotator: OIDC verifier failed to build — guarded routes will 503")
    if settings.fga_enabled:
        try:
            store_id, model_id = settings.fga_store_id, settings.fga_model_id
            if not (store_id and model_id):
                store_id, model_id = await fga.provision(settings.fga_api_url)
                logger.info("annotator: openfga provisioned store=%s model=%s", store_id, model_id)
            app.state.fga = fga.make_client(settings.fga_api_url, store_id, model_id, timeout_seconds=settings.fga_timeout_seconds)
            logger.info("annotator: FGA client ready (%s)", settings.fga_api_url)
        except Exception:
            logger.exception("annotator: FGA client failed to build — authorized routes will 503")

    # Register the actor type with the sidecar. This is the estate's FIRST actor: `lance-statestore`
    # has carried `actorStateStore: "true"` scoped to catalog+annotator since it was provisioned, and
    # nothing used it. Registration must happen in the LIFESPAN, not at import — `register_actor`
    # calls the sidecar's actor API, so doing it at module scope would make importing this module
    # perform I/O and fail wherever daprd is absent (tests, `--help`, an image build).
    #
    # A failure here is logged and left non-fatal: the read-plane annotation routes do not need
    # actors, so a task-plane outage must not take the media surface down with it. The task
    # endpoints surface it as a 503 instead.
    if actor_ext is not None:
        try:
            await actor_ext.register_actor(AnnotationTaskActor)
            await actor_ext.register_actor(AnnotationProjectActor)
            await actor_ext.register_actor(TenantProjectsActor)
            app.state.actors_registered = True
            logger.info("annotator: AnnotationTaskActor + AnnotationProjectActor + TenantProjectsActor registered with the sidecar")
        except Exception:
            app.state.actors_registered = False
            logger.exception("annotator: actor registration failed — the task plane will 503")

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

# Mount the actor HTTP surface the sidecar calls back on (/dapr/config, /actors/...). Constructing
# DaprActor only adds routes — it performs no I/O — so it is safe at import; the REGISTRATION that
# does talk to the sidecar happens in the lifespan above. Built here rather than inside the lifespan
# because routes added after startup are not served.
actor_ext: DaprActor | None
try:
    actor_ext = DaprActor(app)
except Exception:  # pragma: no cover - a broken ext must not take the read plane down
    actor_ext = None
    logger.exception("annotator: could not mount the actor routes — the task plane is unavailable")

app.include_router(probes_router)
app.include_router(api_router)


def run() -> None:
    import uvicorn

    s = get_annotator_settings()
    uvicorn.run("annotator.main:app", host=s.host, port=s.service_port)
