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

import httpx
from dapr.aio.clients import DaprClient
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

from medallion.api.events import register_stage_route
from medallion.api.stage_ops import router as stage_ops_router
from medallion.core.config import apply_dapr_secrets, get_settings
from medallion.services.ray_submit import close_ray_client
from service_kit import setup_logging
from service_kit.draining import arm_drain_on_sigterm
from service_kit.exceptions import register_handlers
from service_kit.governed.actor_state_store import probe_actor_state_store
from service_kit.governed.auth_lifespan import build_fga_client
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
    # ONE catalog client for the process, not one per stage transition. Every mover event makes at
    # least one catalog call (register/publish) and the held path makes two, each of which was opening
    # and tearing down its own connection — `fastapi` -> production-patterns.md § Lifespan: build once,
    # dispose once. Closed below, beside the sidecar client.
    app.state.catalog_http = httpx.Client(base_url=_settings.catalog_url.rstrip("/"), timeout=_settings.publish_timeout_seconds)
    # THE FGA HALF ONLY, deliberately. A mover is bus-only — no gateway row, no Ingress, no human
    # caller — so it has never had an OIDC door and must not grow one as a side effect of sharing the
    # bootstrap: constructing a verifier here would fetch discovery at boot for a token nothing
    # presents. It checks authorization as its OWN service identity before every transition.
    #
    # Pre-set to None because the transition guard reads the attribute directly; unset would be an
    # AttributeError on the hot path rather than a fail-closed refusal. Pinned ids when set (the
    # production posture), else provision by store NAME so the mover converges on the catalog's
    # Zanzibar store (idempotent).
    #
    # `fatal=True` KEEPS THIS APP'S POSTURE: no `try` wrapped the build, so a failed one has always
    # crashed the pod. A mover that cannot authorize must not sit in the subscription quietly
    # refusing every stage — nothing downstream would report it.
    settings = get_settings()
    app.state.fga = await build_fga_client(settings, service="medallion-mover", fatal=True)
    # THE WORKFLOW WORKER (S1). Without this the mover can SCHEDULE `stage_run` and nothing will ever
    # execute it: `DaprWorkflowClient` only enqueues, and the runtime is what registers the definitions
    # and pulls work. Ingest's first in-cluster deploy had the engine running in the sidecar and still
    # could not run a workflow because the APP side was absent — an asymmetry that looks healthy from
    # every angle except an actual run. Only started when the Ray lane is on, because that is the only
    # lane with a job to wait for.
    app.state.workflow_runtime = None
    app.state.workflow_client = None
    if settings.ray_enabled:
        try:
            import dapr.ext.workflow as wf

            from medallion.workflow import register

            runtime = wf.WorkflowRuntime()
            register(runtime)
            runtime.start()  # spawns the worker's own threads; does not block the event loop
            app.state.workflow_runtime = runtime
            # ONE client for the app, read by the operator routes — never one per request, which
            # re-opens a gRPC channel to the sidecar on every call.
            app.state.workflow_client = wf.DaprWorkflowClient()
            log.info("dapr workflow runtime started")
            # The line above is TRUE and INSUFFICIENT: the runtime starts whether or not this
            # app-id can reach an actor state store, and without one the first call fails (and,
            # on dapr 1.18.1, panics the sidecar). Ask the sidecar what it can actually see.
            await probe_actor_state_store(capability="this mover cannot run a stage")
        except Exception:
            # Non-fatal, same reasoning as ingest: a service that refuses to start because its sidecar
            # is not up yet turns an ordering blip into a CrashLoopBackOff. A stage that cannot
            # schedule fails loudly at dispatch, where an operator can see it.
            log.warning("dapr workflow runtime unavailable — ray stages cannot wait for their jobs", exc_info=True)
    else:
        # ANNOUNCE THE NEGATIVE CASE. `flows` states its inline fallback and `ingest` states its own;
        # this branch said nothing, so a mover hosting ZERO workflow workers looked identical in the log
        # to one hosting them. The lane is coherently off — `transform.py` gates dispatch on the same
        # flag — but "off" and "broken" have to be distinguishable without reading the chart.
        log.info("dapr workflow runtime NOT started — the ray lane is off (MEDALLION_RAY_ENABLED unset)")
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
        if app.state.workflow_runtime is not None:
            with suppress(Exception):
                app.state.workflow_runtime.shutdown()
        with suppress(Exception):
            await app.state.dapr.close()
        # Dispose the catalog client beside the sidecar's: built once in this lifespan, so it is this
        # lifespan's to close. `suppress` for the same reason the others use it — a shutdown that
        # raises on a already-broken connection must not stop the rest of the teardown.
        with suppress(Exception):
            app.state.catalog_http.close()
        # And the RAY client, which the workflow activities pool at module level. It cannot live on
        # `app.state` — an activity has no `Request` and no way to reach it — so it takes the worker's
        # lifetime instead, and this is where that lifetime ends. Without this the pooling would trade a
        # per-call teardown for a permanent leak plus an "Unclosed client session" on every stop.
        with suppress(Exception):
            await close_ray_client()
        if app.state.fga is not None:
            with suppress(Exception):
                await app.state.fga.close()


# Application logging, before the app exists — every module here uses getLogger(__name__), and
# without this they propagate to a root logger with no handlers and are DISCARDED. That is not
# hypothetical: it hid a two-day lineage feed outage (see service_kit.setup_logging).
setup_logging()

app = FastAPI(
    title=f"medallion mover ({_settings.from_namespace}->{_settings.to_namespace})",
    lifespan=lifespan,
    docs_url="/docs" if _settings.docs_enabled else None,
    redoc_url="/redoc" if _settings.docs_enabled else None,  # gate /docs (off in prod), like the catalog
    openapi_url="/openapi.json" if _settings.docs_enabled else None,
)
# Problem+json handlers — parity with catalog/lineage/compaction: medallion runs the same lance stack,
# so a LanceNamespaceError (or any unhandled error) must surface as the same RFC 9457 body, not
# Starlette's plain 500 text. Installed BEFORE the routers so no route can outrun the taxonomy.
# AND the fleet handlers, before the lance translator. `service_kit.exceptions.DomainError`
# subclasses `HTTPException`, so without `register_handlers` starlette's built-in handler renders
# it — status and headers intact, `{"detail": ...}` body — which is how the draining 503 came to
# declare problem+json over a body that was not one. Registered FIRST so the lance translator
# still wins for `RequestValidationError`, exactly the order `make_service_app` uses.
register_handlers(app)
install_problem_handlers(app, log)
app.include_router(health_router)
# The DaprApp wrapper serves GET /dapr/subscribe (read by the sidecar at startup) and routes deliveries
# of `sub_topic` to /medallion-event. Each mover has its own app-id + sub_topic, so no consumer clash.
register_stage_route(app)
# The cascade's operator surface (DWF-MGT-002/003). Mounted HERE and not on the producer because both
# `get_workflow_state` and `terminate_workflow` resolve the instance through the calling app's app-id:
# the producer would not find it and would accept the call anyway. The producer proxies to this.
app.include_router(stage_ops_router)
