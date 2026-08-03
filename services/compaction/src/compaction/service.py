"""Compaction service — a FastAPI app triggered by a Dapr cron **input binding**.

A ``bindings.cron`` component POSTs to ``/<binding-name>`` every interval (no app code drives the
schedule — it's component config). Each tick runs one maintenance sweep: discover every Lance dataset in
the lakehouse bucket, then ``compact_files()`` + ``cleanup_old_versions()`` each. Blocking Lance/S3 IO
runs in the threadpool so the event loop stays free. Run: ``uvicorn compaction.service:app``.

Thin entrypoint: lifespan + ``FastAPI()`` + health, with the cron route + OPTIONS ack registered via
``compaction.api.routes``. The sweep logic lives in ``compaction.services``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from dapr.aio.clients import DaprClient
from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

from compaction.api.routes import router
from compaction.core.config import apply_dapr_secrets, get_settings
from compaction.core.lineage_emit import make_emitter
from service_kit.governed.dapr_auth import assert_app_token_configured
from service_kit.lakehouse.lance_metrics import instrument_lance_if_available
from service_kit.lakehouse.ns_errors import install_problem_handlers
from service_kit.obs import configure_app_logging
from service_kit.probes import router as probes_router


configure_app_logging()  # INFO audit/lifecycle logs reach OTLP (obs audit 2026-07-13)

log = logging.getLogger(__name__)


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
        raise RuntimeError("COMPACTION_S3_SECRET_ACCESS_KEY is required (set it, or enable COMPACTION_SECRETS_FROM_DAPR)")
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
    title="Lance compaction/GC",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs else None,
    openapi_url="/openapi.json" if _docs else None,
)


# Problem+json handlers (parity with catalog/lineage — compaction runs the same lance stack, so an
# unhandled LanceNamespaceError must surface as the same RFC 9457 body, not Starlette's plain 500 text).
install_problem_handlers(app, log)

# /livez + /readyz — the shared router, not a hand-rolled copy (service_kit.probes).
app.include_router(probes_router)

# The Dapr cron route (POST /<binding-name>) + its OPTIONS discovery ack, with the require_dapr_token gate.
app.include_router(router)
