"""lance-ray — the dummy Ray ingest job that is the HEAD of the medallion pipeline (FastAPI entry).

Event-driven head (GOAL 4 B2, reshaped by R23 — bronze is the FIRST governed tier; raw is the external
world): ``POST /produce`` (with ``compute_enabled``) seeds a real ``bronze$events`` Lance dataset and
emits ONE OpenLineage event for it. It does NOT itself publish ``medallion.bronze`` — this app also
*subscribes* to the shared lineage topic (``/bronze-arrival``), reacts to a bronze-dataset write event,
and publishes the trigger the ``bronze→silver`` mover consumes. So the cascade is driven by the
*arrival of external raw INTO bronze*, not the call: every stage, the head included, reacts to an event
on the bus. What drives it is specifically a COMPLETE write whose output matches
``bronze_namespace``/``bronze_dataset`` (``bronze`` / ``bronze$events``, or a page lane's
``bronze$pages``) — this dummy today, or a real Ray ingest job that writes that same dataset. (An
ordinary catalog table write does NOT: its output namespace/name won't match the bronze filter — the
head reacts to the *bronze* dataset, not to any write.) In production the head is a real Ray Data job
emitting the same event; here it is a dummy emitter, which is all the event-driven demo needs.

Run: ``uvicorn medallion.producer:app``. Publishes/subscribes through the local Dapr sidecar (best-effort).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from dapr.aio.clients import DaprClient
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

from medallion.api.bronze_arrival import register_bronze_arrival_route
from medallion.api.ingest_media import router as ingest_media_router
from medallion.api.produce import router as produce_router
from medallion.api.train import register_train_trigger_route
from medallion.api.train import router as train_router
from medallion.core.config import apply_dapr_secrets, get_settings
from service_kit.governed import fga
from service_kit.governed.audit import configure_audit
from service_kit.governed.dapr_auth import assert_app_token_configured
from service_kit.governed.oidc import OIDCVerifier
from service_kit.lakehouse.lance_metrics import instrument_lance_if_available
from service_kit.lakehouse.ns_errors import install_problem_handlers
from service_kit.obs import configure_app_logging
from service_kit.probes import router as health_router


configure_app_logging()  # INFO audit/lifecycle logs reach OTLP (obs audit 2026-07-13)

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Fail closed if behind a Dapr sidecar but the app-token is unset — /bronze-arrival would otherwise be
    # an open forged-trigger path (symmetric with the movers + lineage). No-op in dev (dapr_enabled off).
    app.state.startup_complete = False
    app.state.shutting_down = False
    configure_audit(enabled=get_settings().audit_enabled)  # #41 gate the compliance audit stream
    assert_app_token_configured(dapr_enabled=get_settings().dapr_enabled)
    # Consume the S3 secret from the Dapr secret store when configured (Batch 7 — strict sole source,
    # fails closed; no-op in dev). Threadpool: the fetch blocks + retries while the store seeds.
    await run_in_threadpool(apply_dapr_secrets, get_settings())
    instrument_lance_if_available()  # Lance-native IO metrics onto the global MeterProvider
    # The Dapr client targets the local sidecar (localhost) — cheap to build, no broker reachability
    # needed at boot. The sidecar persists publishes to NATS JetStream; a delivery that exhausts its
    # retries dead-letter-parks on the subscriber's dlq.* topic (Dapr-native DLQ, default-on via the
    # dapr.resiliency.enabled chart resiliency; the /dlq-event route ERROR-logs + acks — park-and-alert,
    # not replay — docs/RESILIENCE.md gap #2, fixed 2026-07-12).
    app.state.dapr = DaprClient()
    # The trainer consumer (#115a) gates as its own identity — the client MUST exist here or the gate
    # is silently off with MEDALLION_FGA_ENABLED=true (review 2026-07-10 caught exactly that bypass).
    app.state.fga = None
    if get_settings().fga_enabled:
        settings = get_settings()
        # Pinned-else-provision + explicit timeout — the same client-construction shape as
        # catalog/lineage, so all four FGA consumers behave alike (audit 2026-07-15: the medallion
        # alone re-provisioned on every boot, minting a model version per pod restart).
        store_id, model_id = settings.fga_store_id, settings.fga_model_id
        if not (store_id and model_id):
            store_id, model_id = await fga.provision(settings.fga_api_url)
        app.state.fga = fga.make_client(settings.fga_api_url, store_id, model_id, timeout_seconds=settings.fga_timeout_seconds)
    # #64 OIDC verifier for the /produce human door (admin can trigger the cascade without the service
    # token). Same construction shape as catalog/lineage; None when OIDC is off (dev / service-only).
    app.state.oidc = None
    _oidc = get_settings()
    if _oidc.oidc_enabled and _oidc.oidc_issuer and _oidc.oidc_audience:
        app.state.oidc = OIDCVerifier(
            _oidc.oidc_issuer,
            _oidc.oidc_audience,
            _oidc.oidc_cache_ttl,
            leeway=_oidc.oidc_leeway,
            allow_insecure=_oidc.oidc_allow_insecure,
            # Split-horizon (see the catalog twin): in-cluster fetch, public issuer string.
            discovery_overrides=({_oidc.oidc_issuer: _oidc.oidc_discovery_url} if _oidc.oidc_discovery_url else None),
        )
    app.state.startup_complete = True
    try:
        yield
    finally:
        app.state.shutting_down = True
        with suppress(Exception):
            await app.state.dapr.close()
        fga_client = getattr(app.state, "fga", None)
        if fga_client is not None:
            with suppress(Exception):
                await fga_client.close()


_docs = get_settings().docs_enabled  # gate /docs + /openapi.json (off in prod), like the catalog
app = FastAPI(
    title="lance-ray (medallion producer)",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs else None,
    openapi_url="/openapi.json" if _docs else None,
)
# Problem+json handlers — parity with catalog/lineage/compaction: medallion runs the same lance stack,
# so a LanceNamespaceError (or any unhandled error) must surface as the same RFC 9457 body, not
# Starlette's plain 500 text. Installed BEFORE the routers so no route can outrun the taxonomy.
install_problem_handlers(app, log)
app.include_router(health_router)
app.include_router(produce_router)
# The multimodal head (§9): POST /ingest-media lands external media as bronze blobs + triggers the
# media chain (bronze→silver derive) — the deployed twin of the manual media pipeline scripts.
app.include_router(ingest_media_router)
# The event-driven cascade head: subscribe to the lineage topic; a bronze write fires medallion.bronze.
_dapr_app = register_bronze_arrival_route(app)
# The Ray TRAIN head (#115a): POST /train + the training-trigger subscription (own topic; submit-and-ack).
app.include_router(train_router)
register_train_trigger_route(app, _dapr_app)
