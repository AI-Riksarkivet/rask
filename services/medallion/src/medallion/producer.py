"""medallion-producer — the dummy Ray ingest job that is the HEAD of the medallion pipeline (FastAPI entry).

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
from medallion.api.cascade_lag_cron import mount_lag_cron
from medallion.api.ingest_media import router as ingest_media_router
from medallion.api.mover_ops import router as mover_ops_router
from medallion.api.produce import router as produce_router
from medallion.api.promotions import register_promotion_route
from medallion.api.promotions import router as promotions_router
from medallion.api.train import register_train_trigger_route
from medallion.api.train import router as train_router
from medallion.core.config import get_settings
from medallion.services.task_register import register_ray_tasks
from service_kit.draining import arm_drain_on_sigterm
from service_kit.governed.actor_state_store import probe_actor_state_store
from service_kit.governed.audit import configure_audit
from service_kit.governed.auth_lifespan import attach_auth
from service_kit.governed.dapr_auth import assert_app_token_configured
from service_kit.governed.secrets import apply_dapr_secrets
from service_kit.lakehouse.lance_metrics import instrument_lance_if_available
from service_kit.lance_app import build_lance_service_app
from service_kit.obs import configure_app_logging


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
    # Consume the S3 secret from the Dapr secret store when configured (strict sole source, fails closed;
    # no-op in dev). Threadpool: the fetch blocks + retries while the store seeds. The splice mutates the
    # `@lru_cache`d settings IN PLACE — deliberately, so every later `get_settings()` read sees the key;
    # `apply_dapr_secrets` carries why a copy would break this silently.
    await run_in_threadpool(apply_dapr_secrets, get_settings())
    instrument_lance_if_available()  # Lance-native IO metrics onto the global MeterProvider
    # WHAT THIS ESTATE'S RAY PLANE CAN RUN, asserted by a plane that can actually run it. The catalog
    # consults `_tasks/` when a transform is declared, so a task nobody registered is refused at the
    # door rather than discovered at submit. AFTER `apply_dapr_secrets`, because the write needs the
    # S3 credential that call splices in. Non-fatal by design — see `register_ray_tasks`.
    await run_in_threadpool(register_ray_tasks, get_settings())
    # The Dapr client targets the local sidecar (localhost) — cheap to build, no broker reachability
    # needed at boot. The sidecar persists publishes to NATS JetStream; a delivery that exhausts its
    # retries dead-letter-parks on the subscriber's dlq.* topic (Dapr-native DLQ, default-on via the
    # dapr.resiliency.enabled chart resiliency; the /dlq-event route ERROR-logs + acks — park-and-alert,
    # not replay — docs/RESILIENCE.md gap #2, fixed 2026-07-12).
    app.state.dapr = DaprClient()
    # BOTH DEFAULT TO None BEFORE THE BUILD, because this app's dependencies read the attributes
    # directly rather than through `getattr`: the trainer consumer (#115a) gates as its own identity
    # and the client MUST exist here or the gate is silently off with RASK_FGA_ENABLED=true
    # (review 2026-07-10 caught exactly that bypass), and #64's OIDC verifier is the /produce human
    # door — an admin can trigger the cascade without the service token.
    #
    # `fatal=True` KEEPS THIS APP'S POSTURE: it wrapped neither construction in a `try`, so a failed
    # build has always crashed the pod. That is the loud failure, and the cascade head is not a place
    # to serve 503s quietly — a producer that cannot authorize is a cascade that does not run.
    app.state.fga = None
    app.state.oidc = None
    await attach_auth(app, get_settings(), service="medallion-producer", fatal=True)
    # THE WORKFLOW WORKER for `promotion_review` — and the reason this app hosts it at all.
    # `raise_workflow_event` resolves the instance through the CALLING app's app-id, so the approve
    # route and the instance must share a process. The gate that holds a promotion runs in a mover,
    # but a mover is bus-only: no gateway row, no Ingress, nothing a person can POST to. Hosting the
    # workflow here (beside the door, behind the same dual-auth as /produce) is what makes the ask
    # answerable; the mover reaches it by publishing, like every other cascade hop.
    #
    # Without this the door 404s honestly — which is the correct failure, not a working one.
    app.state.workflow_runtime = None
    app.state.workflow_client = None
    # `qualityReview OR ray`, because this app hosts TWO workflows and they answer to different
    # features: `promotion_review` (quality review) and `train_run` (the Ray training watcher, started
    # by `schedule_train_watch`). Gating on quality review alone meant the DEFAULT chart -- ray on,
    # review off -- started no runtime here, so every training job was submitted and never watched.
    # That lane fails silently ON PURPOSE (a lost watcher must not fail a trigger whose job is already
    # running), so nobody was ever told: no terminal event, no outcome report, no notification to the
    # originator. Owner ruling 2026-08-25. NOT "always": with neither feature on, this app hosts no
    # workflow and should run no engine.
    settings = get_settings()
    if settings.quality_review_enabled or settings.ray_enabled:
        try:
            import dapr.ext.workflow as wf

            from medallion.workflow import register

            runtime = wf.WorkflowRuntime()
            register(runtime)
            runtime.start()  # its own threads; does not block the event loop
            app.state.workflow_runtime = runtime
            # ONE client for the app, not one per request — the decision door reads it from here.
            app.state.workflow_client = wf.DaprWorkflowClient()
            log.info("dapr workflow runtime started", extra={"promotion_review": settings.quality_review_enabled, "train_watch": settings.ray_enabled})
            # The line above is TRUE and INSUFFICIENT: the runtime starts whether or not this
            # app-id can reach an actor state store, and without one the first call fails (and,
            # on dapr 1.18.1, panics the sidecar). Ask the sidecar what it can actually see.
            await probe_actor_state_store(capability="held promotions cannot be reviewed and training jobs cannot be watched")
        except Exception:
            # Non-fatal, the mover's reasoning: refusing to start because the sidecar is not up yet
            # turns an ordering blip into a CrashLoopBackOff. A hold that cannot be scheduled RETRYs
            # at the subscription, where it is visible.
            log.warning("dapr workflow runtime unavailable — held promotions cannot be reviewed", exc_info=True)
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
        fga_client = getattr(app.state, "fga", None)
        if fga_client is not None:
            with suppress(Exception):
                await fga_client.close()


# THE SHARED LANCE-PLANE ASSEMBLY (open_python-audit DUP-12). Logging before the app exists, the docs
# gate, the handler pair in the order that makes it work, one request id, and the probes — see
# `service_kit.lance_app` for what each of those five is for and what a copy of it got wrong. The
# handlers are installed BEFORE any router, so no route can outrun the taxonomy.
app = build_lance_service_app(
    title="medallion-producer (medallion producer)",
    docs_enabled=get_settings().docs_enabled,
    lifespan=lifespan,
    log=log,
)
app.include_router(produce_router)
# The multimodal head (§9): POST /ingest-media lands external media as bronze blobs + triggers the
# media chain (bronze→silver derive) — the deployed twin of the manual media pipeline scripts.
app.include_router(ingest_media_router)
# The event-driven cascade head: subscribe to the lineage topic; a bronze write fires medallion.bronze.
_dapr_app = register_bronze_arrival_route(app)
# The Ray TRAIN head (#115a): POST /train + the training-trigger subscription (own topic; submit-and-ack).
app.include_router(train_router)
register_train_trigger_route(app, _dapr_app)
# The quality gate's third answer (S3/S4): a mover that HOLDS a promotion publishes it here, the
# review workflow runs in this process, and a `can_promote` holder answers it on /promotions/*.
app.include_router(promotions_router)
# The cascade's operator door (DWF-MGT-002/003). The ROUTES that touch the workflow live on the mover
# — `terminate_workflow` resolves the instance through the calling app's app-id — so this end does the
# human auth and forwards. See `api/mover_ops.py` for why the split is forced rather than chosen.
app.include_router(mover_ops_router)
register_promotion_route(app, _dapr_app)
# The cascade-lag cron door. Opt-in on a configured binding name, like the control relay: an unnamed
# binding means no Component, and mounting an always-live door would add a catalog+lineage scan surface
# with nothing behind it. See `api/cascade_lag_cron.py` for the one-string rule.
mount_lag_cron(app, get_settings().cascade_lag_binding_name)
