"""Table maintenance service — a FastAPI app driven by Dapr cron **input bindings**.

``bindings.cron`` components POST to ``/<binding-name>`` every interval (no app code drives the
schedule — it is component config). Two bindings, two jobs:

* the **sweep** discovers every Lance dataset across the configured buckets and runs one ORDERED pass
  per dataset — ``compact_files()``, then ``cleanup_old_versions()``, then ``optimize_indices()``.
  The order is the reason these live in one service: compaction obsoletes files, so cleanup must
  follow it, and index optimization must follow that.
* the **reconcile** pass reads OpenFGA, the control-root registries and object storage, and REPORTS
  where they disagree. It mutates nothing.

Blocking Lance/S3 IO runs in the threadpool so the event loop stays free.
Run: ``uvicorn maintenance.service:app``.

Thin entrypoint: lifespan + ``FastAPI()`` + health, with both cron routes registered via
``maintenance.api.routes``. The operations live in ``maintenance.services``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any

from dapr.aio.clients import DaprClient
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

from maintenance.api.routes import router
from maintenance.core.config import MaintenanceSettings, apply_dapr_secrets, get_settings
from maintenance.core.lineage_emit import make_emitter
from service_kit.governed import fga
from service_kit.governed.dapr_auth import assert_app_token_configured
from service_kit.lakehouse.lance_metrics import instrument_lance_if_available
from service_kit.lakehouse.ns_errors import install_problem_handlers
from service_kit.obs import configure_app_logging
from service_kit.probes import router as probes_router
from storage import s3_client


configure_app_logging()  # INFO audit/lifecycle logs reach OTLP (obs audit 2026-07-13)

log = logging.getLogger(__name__)


def _make_fga_client(settings: MaintenanceSettings) -> Any | None:  # noqa: ANN401 — OpenFgaClient, no protocol
    """A READ-ONLY OpenFGA client for the drift reconciler, or ``None``.

    ``None`` on every "not configured" path — FGA off, or the store/model ids unpinned. This service
    only reads tuples, so unlike the catalog it must NOT provision a store when the ids are absent:
    provisioning from a maintenance job would create an empty store and then cheerfully report every
    real tenant as a ghost, which is worse than reporting the category unavailable.

    Never raises. A misconfigured authz endpoint degrades four categories; it must not stop the sweep.
    """
    if not settings.fga_enabled:
        return None
    if not (settings.fga_store_id and settings.fga_model_id):
        log.warning("reconcile_fga_unpinned", extra={"reason": "MAINTENANCE_FGA_STORE_ID/MODEL_ID unset — authz categories will report unavailable"})
        return None
    try:
        return fga.make_client(
            settings.fga_api_url,
            settings.fga_store_id,
            settings.fga_model_id,
            timeout_seconds=settings.fga_timeout_seconds,
        )
    except Exception:
        log.warning("reconcile_fga_client_failed", exc_info=True)
        return None


def _make_s3_client(settings: MaintenanceSettings) -> Any | None:  # noqa: ANN401 — boto3 client has no stub
    """The bucket-listing client for ``orphan_buckets``, or ``None`` (→ that category is unavailable).

    Built from the sweep's own credentials via the canonical wrapper in ``packages/storage`` — never
    boto3 directly, so one process cannot end up addressing two different endpoints.
    """
    try:
        return s3_client(
            settings.s3_endpoint,
            access_key=settings.s3_access_key_id,
            secret_key=settings.s3_secret_access_key.get_secret_value(),
        )
    except Exception:
        log.warning("reconcile_s3_client_failed", exc_info=True)
        return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.startup_complete = False
    app.state.shutting_down = False
    settings = get_settings()
    instrument_lance_if_available()  # Lance-native IO metrics onto the global MeterProvider
    # Fail closed if behind a Dapr sidecar but the app-token is unset — the cron route would otherwise be an
    # open forged-sweep path (symmetric with the lineage service). No-op in dev (dapr_enabled off).
    assert_app_token_configured(dapr_enabled=settings.dapr_enabled)
    # Consume the S3 secret from the Dapr secret store (OpenBao) before the first cron sweep, so the
    # sweep's S3 access uses a store-sourced key and the plaintext secret never ships in pod env — the
    # audit's secret-consumption fix. Mutates the cached settings in place; fails closed if unavailable.
    # In a thread: the fetch is sync (blocking httpx + retry sleeps) and must not stall the event loop.
    await run_in_threadpool(apply_dapr_secrets, settings)
    # Fail fast at boot if the S3 secret is still empty (neither the store nor plaintext env provided it) —
    # the sweep is a real S3 consumer, so a silent empty key would otherwise fail every later compaction
    # with a cryptic S3 SignatureDoesNotMatch instead of a clear startup error (parity with the catalog's
    # 'credentials required, no silent fallback'). apply_dapr_secrets already fails closed when the store IS
    # the source; this covers the plaintext-env path (secrets_from_dapr off) the comment used to over-claim.
    if not settings.s3_secret_access_key.get_secret_value():
        raise RuntimeError("MAINTENANCE_S3_SECRET_ACCESS_KEY is required (set it, or enable MAINTENANCE_SECRETS_FROM_DAPR)")
    # Lineage emission (opt-in, best-effort): build the Dapr pub/sub emitter so each materially-compacted
    # dataset records a maintenance run on the lineage graph (#7b). The Dapr client targets the local
    # sidecar, so it's cheap to construct and needs no broker reachability at boot; a no-op emitter when off.
    dapr_client = DaprClient() if settings.lineage_emit_enabled else None
    app.state.lineage_emitter = make_emitter(
        enabled=settings.lineage_emit_enabled,
        dapr=dapr_client,
        pubsub=settings.lineage_pubsub,
        topic=settings.lineage_topic,
        job_namespace=settings.lineage_job_namespace,
        timeout_seconds=settings.publish_timeout_seconds,
    )
    # The reconciler's two read-only clients. Both are OPTIONAL by design: a missing one degrades its
    # categories to UNAVAILABLE-with-a-reason, and the other five still report. Boot must NOT fail on
    # them — the sweep is this service's primary job and does not need either.
    app.state.fga_client = _make_fga_client(settings)
    app.state.s3_client = _make_s3_client(settings)
    app.state.startup_complete = True
    try:
        yield
    finally:
        app.state.shutting_down = True
        if dapr_client is not None:
            with suppress(Exception):
                await dapr_client.close()


_docs = get_settings().docs_enabled  # gate /docs + /openapi.json (off in prod), like the catalog
app = FastAPI(
    title="Lance table maintenance",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs else None,
    openapi_url="/openapi.json" if _docs else None,
)


# Problem+json handlers (parity with catalog/lineage — maintenance runs the same lance stack, so an
# unhandled LanceNamespaceError must surface as the same RFC 9457 body, not Starlette's plain 500 text).
install_problem_handlers(app, log)

# /livez + /readyz — the shared router, not a hand-rolled copy (service_kit.probes).
app.include_router(probes_router)

# The Dapr cron route (POST /<binding-name>) + its OPTIONS discovery ack, with the require_dapr_token gate.
app.include_router(router)
