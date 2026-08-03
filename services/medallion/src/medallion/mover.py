"""A medallion stage mover — one DAG edge, event-driven (FastAPI application entry).

All movers run THIS module (``medallion.mover:app``) and differ only by ``MEDALLION_*`` env: each
subscribes to its upstream stage's trigger topic, emits a standard OpenLineage transform event
(``inputs=[from_dataset]`` → ``outputs=[to_dataset]`` — the ``DERIVED_FROM`` edge), and publishes the next
stage's trigger. So a single producer event cascades bronze→silver→gold (R23: bronze is the first
governed tier — the producer ingests external raw straight into it), and because every hop is a Dapr
publish over the instrumented gRPC client, the W3C trace context propagates → one distributed trace.

Idempotent + best-effort: with ``MEDALLION_COMPUTE_ENABLED`` each stage does a REAL in-process Lance write
(the fake-Ray compute) so the cascade produces data, not just provenance; off, it's a pure lineage emit.
The graph MERGEs on run_id, and a compute/publish outage returns ``RETRY`` so the Dapr sidecar redelivers.
Run: ``uvicorn medallion.mover:app``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from dapr.aio.clients import DaprClient
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

from medallion.api.events import register_stage_route
from medallion.core.config import apply_dapr_secrets, get_settings
from service_kit.governed import fga
from service_kit.governed.dapr_auth import assert_app_token_configured
from service_kit.lakehouse.lance_metrics import instrument_lance_if_available
from service_kit.lakehouse.ns_errors import install_problem_handlers
from service_kit.obs import configure_app_logging
from service_kit.probes import router as health_router


configure_app_logging()  # INFO audit/lifecycle logs reach OTLP (obs audit 2026-07-13)

log = logging.getLogger(__name__)
_settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Fail closed if behind a Dapr sidecar but the app-token is unset — /medallion-event would otherwise be
    # an open forged-trigger path (symmetric with the lineage service). No-op in dev (dapr_enabled off).
    app.state.startup_complete = False
    app.state.shutting_down = False
    assert_app_token_configured(dapr_enabled=_settings.dapr_enabled)
    # Consume the S3 secret from the Dapr secret store when configured (Batch 7 — strict sole source,
    # fails closed; no-op in dev). Threadpool: the fetch blocks + retries while the store seeds.
    await run_in_threadpool(apply_dapr_secrets, _settings)
    instrument_lance_if_available()  # Lance-native IO metrics onto the global MeterProvider
    app.state.dapr = DaprClient()  # local sidecar; persists publishes to NATS JetStream
    app.state.fga = None
    settings = get_settings()
    if settings.fga_enabled:
        # Pinned ids when set (production posture, like catalog/lineage); else provision by store NAME so
        # the mover converges on the catalog's Zanzibar store (idempotent), then check authorization as
        # its own service identity before every transition.
        store_id, model_id = settings.fga_store_id, settings.fga_model_id
        if not (store_id and model_id):
            store_id, model_id = await fga.provision(settings.fga_api_url)
        app.state.fga = fga.make_client(settings.fga_api_url, store_id, model_id, timeout_seconds=settings.fga_timeout_seconds)
    app.state.startup_complete = True
    try:
        yield
    finally:
        app.state.shutting_down = True
        with suppress(Exception):
            await app.state.dapr.close()
        if app.state.fga is not None:
            with suppress(Exception):
                await app.state.fga.close()


app = FastAPI(
    title=f"medallion mover ({_settings.from_namespace}->{_settings.to_namespace})",
    lifespan=lifespan,
    docs_url="/docs" if _settings.docs_enabled else None,  # gate /docs (off in prod), like the catalog
    openapi_url="/openapi.json" if _settings.docs_enabled else None,
)
# Problem+json handlers — parity with catalog/lineage/compaction: medallion runs the same lance stack,
# so a LanceNamespaceError (or any unhandled error) must surface as the same RFC 9457 body, not
# Starlette's plain 500 text. Installed BEFORE the routers so no route can outrun the taxonomy.
install_problem_handlers(app, log)
app.include_router(health_router)
# The DaprApp wrapper serves GET /dapr/subscribe (read by the sidecar at startup) and routes deliveries
# of `sub_topic` to /medallion-event. Each mover has its own app-id + sub_topic, so no consumer clash.
register_stage_route(app)
