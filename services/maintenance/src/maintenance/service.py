"""Table maintenance service — a FastAPI app driven by Dapr cron **input bindings**.

``bindings.cron`` components POST to ``/<binding-name>`` every interval (no app code drives the
schedule — it is component config). Two bindings, two jobs:

* the **sweep** discovers every Lance dataset across the configured buckets and runs one ORDERED pass
  per dataset — ``compact_files()``, then ``optimize_indices()``, then ``cleanup_old_versions()``.
  The order is the reason these live in one service: compaction leaves its new fragments unindexed,
  so index optimization follows it, and cleanup runs last because it reclaims the superseded
  versions both earlier steps produced, in one pass.
* the **reconcile** pass reads OpenFGA, the control-root registries and object storage and REPORTS where
  they disagree — then, ONLY if that report ran clean, purges expired trash records (#79): revoke the
  object's grants, delete the bytes the catalog recorded at drop time, clear the record. Off by default.

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
from service_kit import setup_logging
from service_kit.control_emit import make_control_emitter
from service_kit.governed import fga
from service_kit.governed.dapr_auth import assert_app_token_configured
from service_kit.lakehouse.lance_metrics import instrument_lance_if_available
from service_kit.lakehouse.ns_errors import install_problem_handlers
from service_kit.obs import configure_app_logging
from service_kit.probes import router as probes_router
from storage import s3_client


configure_app_logging()  # INFO audit/lifecycle logs reach OTLP (obs audit 2026-07-13)

log = logging.getLogger(__name__)


async def _make_fga_client(settings: MaintenanceSettings) -> Any | None:  # noqa: ANN401 — OpenFgaClient, no protocol
    """The OpenFGA client this service reads with — and, since #79, REVOKES with. Or ``None``.

    Two consumers with different rights, and the difference is the whole point:

    * the **drift reconciler** only READS tuples (`fga.read_tuples`);
    * the **expired-trash purge** REVOKES an object's tuples (`fga.revoke_object_tuples`, origin
      ``lifecycle_delete``) as the FIRST step of destroying it, because a grant that outlives the bytes
      silently re-grants the old subjects if the id is ever reused. A revoke it cannot perform refuses
      the record, so the client is a precondition for deleting, never a means of granting.

    It holds **no grant-writing path at all** — no `write_tuples`, no `seed_ownership` — which is why it
    needs no capability beyond what pins the store.

    This service must NOT PROVISION a store when the ids are absent: provisioning from a maintenance
    job would create an empty store and then cheerfully report every real tenant as a ghost, which is
    worse than reporting the category unavailable. That rule stands and `fga.provision` is referenced
    nowhere here.

    IT MAY, HOWEVER, LOOK ONE UP — and refusing to was a distinct bug wearing the same justification.
    Unpinned used to return ``None`` outright, so on the chart's DEFAULT posture (`auth.fgaStoreId: ""`,
    because a store id is a per-cluster ULID that cannot be a committed default) this service never had
    a client at all. Measured live 2026-08-16: `ghost_projects`, `ghost_warehouses` and
    `unreferenced_projects` reported UNAVAILABLE on every tick, and since `report_is_clean` blocks on any
    unavailable category, the #79 expired-trash purge could never certify in any default deployment.

    `service_kit.governed.fga.resolve` exists for exactly this, and its docstring records the identical
    mistake made by ingest — which applied the no-authoring principle to BOTH halves and therefore 503'd
    out of the box on every dev and e2e cluster: *"reading is not authoring"*. Resolve is read-only, can
    never create a store or write a model, and returns ``None`` when the estate is not bootstrapped —
    so this still fails closed, it just no longer fails closed against an estate that is right there.

    Never raises. A misconfigured or unreachable authz endpoint degrades the authz categories; it must
    not stop the sweep, which is this service's primary job and needs no FGA at all.
    """
    if not settings.fga_enabled:
        return None

    store_id, model_id = settings.fga_store_id, settings.fga_model_id
    if not (store_id and model_id):
        try:
            resolved = await fga.resolve(settings.fga_api_url)
        except Exception:
            log.warning("reconcile_fga_resolve_failed", exc_info=True)
            return None
        if resolved is None:
            log.warning(
                "reconcile_fga_unpinned",
                extra={"reason": "MAINTENANCE_FGA_STORE_ID/MODEL_ID unset and no store to resolve — authz categories will report unavailable"},
            )
            return None
        store_id, model_id = resolved
        log.info("reconcile_fga_resolved_by_name", extra={"store_id": store_id, "hint": "pin MAINTENANCE_FGA_STORE_ID/MODEL_ID for production"})

    try:
        return fga.make_client(settings.fga_api_url, store_id, model_id, timeout_seconds=settings.fga_timeout_seconds)
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
    # ONE sidecar client for both emitters — lineage (data) and control (governance). Built when either
    # is on; the sidecar is local, so construction is cheap and needs no broker reachability at boot.
    dapr_client = DaprClient() if (settings.lineage_emit_enabled or settings.control_emit_enabled) else None
    app.state.lineage_emitter = make_emitter(
        enabled=settings.lineage_emit_enabled,
        dapr=dapr_client,
        pubsub=settings.lineage_pubsub,
        topic=settings.lineage_topic,
        job_namespace=settings.lineage_job_namespace,
        timeout_seconds=settings.publish_timeout_seconds,
    )
    # #79: the expired-trash purge announces each reclamation on the catalog's control topic. A no-op
    # when off — never a half-configured transport that looks like it publishes.
    app.state.control_emitter = make_control_emitter(
        enabled=settings.control_emit_enabled,
        dapr=dapr_client,
        pubsub=settings.control_pubsub,
        timeout_seconds=settings.publish_timeout_seconds,
        service="maintenance",
    )
    # The reconciler's two read-only clients. Both are OPTIONAL by design: a missing one degrades its
    # categories to UNAVAILABLE-with-a-reason, and the other five still report. Boot must NOT fail on
    # them — the sweep is this service's primary job and does not need either.
    app.state.fga_client = await _make_fga_client(settings)
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
# Application logging, before the app exists — every module here uses getLogger(__name__), and
# without this they propagate to a root logger with no handlers and are DISCARDED
# (see service_kit.setup_logging).
setup_logging()

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
