"""A medallion stage runner — one DAG edge, event-driven (FastAPI application entry).

All stage runners run THIS module (``medallion.stage_runner:app``) and differ only by ``MEDALLION_*`` env: each
subscribes to its upstream stage's trigger topic, emits a standard OpenLineage transform event
(``inputs=[from_dataset]`` → ``outputs=[to_dataset]`` — the ``DERIVED_FROM`` edge), and publishes the next
stage's trigger. So a single producer event cascades bronze→silver→gold (R23: bronze is the first
governed tier — the producer ingests external raw straight into it), and because every hop is a Dapr
publish over the instrumented gRPC client, the W3C trace context propagates → one distributed trace.

Idempotent + best-effort: with ``MEDALLION_COMPUTE_ENABLED`` each stage does a REAL in-process Lance write
(the fake-Ray compute) so the cascade produces data, not just provenance; off, it's a pure lineage emit.
The graph MERGEs on run_id, and a compute/publish outage returns ``RETRY`` so the Dapr sidecar redelivers.
Run: ``uvicorn medallion.stage_runner:app``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx
from dapr.aio.clients import DaprClient
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

from medallion.api.events import register_stage_route
from medallion.api.stage_ops import router as stage_ops_router
from medallion.api.stage_outcomes import mount_stage_outcomes
from medallion.core.config import get_settings
from medallion.core.lineage_publish import start_signing, stop_signing
from medallion.services.engine_registry import close_executors
from service_kit.draining import arm_drain_on_sigterm
from service_kit.governed.auth_lifespan import attach_auth
from service_kit.governed.dapr_auth import assert_app_token_configured
from service_kit.governed.secrets import apply_dapr_secrets
from service_kit.governed.signing_key import signing_ready_check
from service_kit.lakehouse.lance_metrics import instrument_lance_if_available
from service_kit.lance_app import build_lance_service_app
from service_kit.obs import configure_app_logging


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
    # Consume the S3 secret from the Dapr secret store when configured (strict sole source, fails closed;
    # no-op in dev). Threadpool: the fetch blocks + retries while the store seeds. The splice mutates the
    # `@lru_cache`d settings IN PLACE — deliberately, so every later `get_settings()` read sees the key;
    # `apply_dapr_secrets` carries why a copy would break this silently.
    await run_in_threadpool(apply_dapr_secrets, _settings)
    instrument_lance_if_available()  # Lance-native IO metrics onto the global MeterProvider
    app.state.dapr = DaprClient()  # local sidecar; persists publishes to NATS JetStream
    # ONE catalog client for the process, not one per stage transition. Every stage runner event makes at
    # least one catalog call (register/publish) and the held path makes two, each of which was opening
    # and tearing down its own connection — `fastapi` -> production-patterns.md § Lifespan: build once,
    # dispose once. Closed below, beside the sidecar client.
    app.state.catalog_http = httpx.Client(base_url=_settings.catalog_url.rstrip("/"), timeout=_settings.publish_timeout_seconds)
    # NO PERSON'S DOOR, deliberately. A stage runner is bus-only — no gateway row, no Ingress, no human
    # caller — and the chart renders it with OIDC off, so `attach_auth` builds no Dex verifier here. What
    # it does build, when `RASK_SA_ISSUER` is set, is the service-account verifier its operator routes
    # admit the producer through (`service_door.require_producer`, [[LH-220]]), and the FGA client it
    # checks its own service identity against before every transition.
    #
    # Pre-set to None because the transition guard reads the attribute directly; unset would be an
    # AttributeError on the hot path rather than a fail-closed refusal. Pinned ids when set (the
    # production posture), else the store is resolved by NAME, read-only (`fga.resolve`).
    #
    # `fatal=True` KEEPS THIS APP'S POSTURE: no `try` wrapped the build, so a failed one has always
    # crashed the pod. A stage runner that cannot authorize must not sit in the subscription quietly
    # refusing every stage — nothing downstream would report it.
    settings = get_settings()
    app.state.fga = None
    await attach_auth(app, settings, service="medallion-stage-runner", fatal=True)
    # THIS STAGE RUNNER'S OWN SIGNING KEY, resolved through its own sidecar. `start_signing` makes one bounded attempt
    # to resolve the key and does not wait for it to be published, so the probes and the retry answers are served from
    # the moment the app boots whether or not the key resolved: a stage runner that is waiting for its key takes no
    # delivery (`retry_until_signed` on its route), reports itself not ready, and heals in place when the key is
    # published. NO WORKFLOW RUNTIME: a Ray stage is planned and resolved through its outcome door and the plan sweep
    # (`services/stage_plans.py`, CP-029), so this process hosts no Dapr workflow and needs no actor state store.
    signing = await start_signing(app, settings)
    app.state.startup_complete = True
    try:
        # ARMED AT SIGTERM, not at lifespan shutdown. The flag below flips in the `finally`,
        # which uvicorn only reaches AFTER it has stopped accepting connections and drained —
        # so the admission guards that read it refused nothing, ever. Kubernetes sends SIGTERM
        # at the START of termination, and that window is exactly when the sidecar is still
        # delivering. Owner ruling 2026-08-25.
        _disarm_drain = arm_drain_on_sigterm(app)
        yield
    finally:
        _disarm_drain()
        app.state.shutting_down = True
        await stop_signing(signing)
        with suppress(Exception):
            await app.state.dapr.close()
        # Dispose the catalog client beside the sidecar's: built once in this lifespan, so it is this
        # lifespan's to close. `suppress` for the same reason the others use it — a shutdown that
        # raises on a already-broken connection must not stop the rest of the teardown.
        with suppress(Exception):
            app.state.catalog_http.close()
        # And what the executors hold for the process (the Ray adapter's pooled client). Dispatch, the sweep and
        # the operator routes all use it on THIS loop, so it is closed here, on the loop that owns its pool.
        with suppress(Exception):
            await close_executors()
        if app.state.fga is not None:
            with suppress(Exception):
                await app.state.fga.close()


# THE SHARED LANCE-PLANE ASSEMBLY (docs/DECISIONS.md "The Python estate audit" DUP-12). Logging before the app exists, the docs
# gate, the handler pair in the order that makes it work, one request id, and the probes — see
# `service_kit.lance_app` for what each of those five is for and what a copy of it got wrong.
#
# THE STAGE RUNNER IS THE COPY THAT LOST ONE. Its four siblings each added `RequestIDMiddleware` under the
# same copied comment, and this file did not — so the service that consumes the cascade's bus
# deliveries was the one whose responses carried no id to quote. Coming through the factory it gets
# the same layer as everything else.
app = build_lance_service_app(
    title=f"medallion stage runner ({_settings.from_namespace}->{_settings.to_namespace})",
    docs_enabled=_settings.docs_enabled,
    audit_enabled=_settings.audit_enabled,
    lifespan=lifespan,
    log=log,
    ready_check=signing_ready_check(),
)
# The DaprApp wrapper serves GET /dapr/subscribe (read by the sidecar at startup) and routes deliveries
# of `sub_topic` to /medallion-event. Each stage runner has its own app-id + sub_topic, so no consumer clash.
register_stage_route(app)
# The cascade's operator surface (DWF-MGT-002/003) over this stage runner's plans, which live under its own identity;
# the producer authorizes a person and proxies to it.
app.include_router(stage_ops_router)
# The planned runs' outcome door (the compute head's jobs report here) and the plan sweep's cron door.
mount_stage_outcomes(app, sweep_binding_name=_settings.plan_sweep_binding_name)
