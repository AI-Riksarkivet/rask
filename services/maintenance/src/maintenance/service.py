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

from maintenance.api.arrival import register_arrival_route
from maintenance.api.index_work import register_index_route
from maintenance.api.routes import build_router
from maintenance.api.work import register_work_route
from maintenance.core.config import MaintenanceSettings, get_settings
from maintenance.core.lineage_emit import make_emitter
from service_kit.control_emit import make_control_emitter
from service_kit.governed.auth_lifespan import build_fga_client
from service_kit.governed.dapr_auth import assert_app_token_configured
from service_kit.governed.fga import dispose as fga_dispose
from service_kit.governed.secrets import apply_dapr_secrets
from service_kit.lakehouse.lance_metrics import instrument_lance_if_available
from service_kit.lance_app import build_lance_service_app
from service_kit.obs import configure_app_logging
from storage import s3_client


configure_app_logging()  # INFO audit/lifecycle logs reach OTLP (obs audit 2026-07-13)

log = logging.getLogger(__name__)


async def _make_fga_client(settings: MaintenanceSettings) -> Any | None:  # noqa: ANN401 — OpenFgaClient, no protocol
    """The OpenFGA client this service reads with — and, since #79, REVOKES with. Or ``None``.

    Two consumers with different rights, and the difference is the whole point:

    * the **drift reconciler** only READS tuples (`fga.read_tuples`);
    * the **expired-trash purge** REVOKES an object's tuples (`fga.revoke_object_tuples`, origin
      ``lifecycle_delete``) as the FIRST step of destroying it — a grant that outlives the bytes would
      silently re-grant the old subjects if the id is ever reused. (`lifecycle_delete` is the real
      origin, on the governed allowlist at `service_kit.governed.fga`; `purge.py` passes it. An
      earlier prose pass invented `trash-purge`, which is on no allowlist and matches nothing.)

    Neither may AUTHOR the estate's model, so this passes ``provision=False``: the shared bootstrap
    then takes `fga.resolve`, which is read-only, can never create a store or write a model, and
    returns ``None`` when the estate is not bootstrapped. That principle once covered the LOOKUP too,
    and the cost is recorded in `fga.resolve`'s own docstring — on the chart's DEFAULT posture
    (`auth.fgaStoreId: ""`, a per-cluster ULID that cannot be a committed default) refusing to look up
    meant reporting every authz category unavailable against an estate that was right there. Reading
    which store exists is not authoring one.

    NEVER RAISES, which is why ``fatal`` stays at its default. A misconfigured or unreachable authz
    endpoint degrades the authz categories; it must not stop the sweep, which is this service's
    primary job and needs no FGA at all. It also returns the client rather than assigning it: the
    sweep runs from a cron route and holds it as ``app.state.fga_client``.
    """
    return await build_fga_client(settings, service="maintenance", provision=False)


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
    # sweep's S3 access uses a store-sourced key and the plaintext secret never ships in pod env. Fails
    # closed if unavailable. The splice mutates the `@lru_cache`d settings IN PLACE — deliberately, so
    # every later `get_settings()` read sees the key; `apply_dapr_secrets` carries why a copy would break
    # this silently. In a thread: the fetch is sync (blocking httpx + retry sleeps) and must not stall
    # the event loop.
    await run_in_threadpool(apply_dapr_secrets, settings)
    # Fail fast at boot if the S3 secret is still empty (neither the store nor plaintext env provided it) —
    # the sweep is a real S3 consumer, so a silent empty key would otherwise fail every later compaction
    # with a cryptic S3 SignatureDoesNotMatch instead of a clear startup error (parity with the catalog's
    # 'credentials required, no silent fallback'). The shared apply_dapr_secrets already fails closed when the store IS
    # the source; this covers the plaintext-env path (secrets_from_dapr off) the comment used to over-claim.
    if not settings.s3_secret_access_key.get_secret_value():
        raise RuntimeError("MAINTENANCE_S3_SECRET_ACCESS_KEY is required (set it, or enable MAINTENANCE_SECRETS_FROM_DAPR)")
    # THE KEY HALF, refused here for the same reason and in the same place. It used to default to the
    # RustFS tenant root, so an unconfigured deployment did not fail — it ran the entire sweep with a
    # credential reaching every tenant's bytes and the `_projects/`/`_protection/` records that govern
    # maintenance itself. A credential is a pair; refusing only the secret half left the dangerous half
    # unguarded.
    if not settings.s3_access_key_id:
        raise RuntimeError("MAINTENANCE_S3_ACCESS_KEY_ID is required — it no longer defaults to the RustFS tenant root")
    # Lineage emission (opt-in, best-effort): build the Dapr pub/sub emitter so each materially-compacted
    # dataset records a maintenance run on the lineage graph (#7b). The Dapr client targets the local
    # sidecar, so it's cheap to construct and needs no broker reachability at boot; a no-op emitter when off.
    # ONE sidecar client for both emitters — lineage (data) and control (governance). Built when either
    # is on; the sidecar is local, so construction is cheap and needs no broker reachability at boot.
    # The work queue publishes through this same client, so a configured work topic keeps it alive
    # even with both emitters off — otherwise the tick would plan units it has no way to send.
    dapr_client = DaprClient() if (settings.lineage_emit_enabled or settings.control_emit_enabled or settings.work_topic) else None
    app.state.lineage_emitter = make_emitter(
        enabled=settings.lineage_emit_enabled,
        dapr=dapr_client,
        pubsub=settings.lineage_pubsub,
        topic=settings.lineage_topic,
        job_namespace=settings.lineage_job_namespace,
        timeout_seconds=settings.publish_timeout_seconds,
        outbox_uri=settings.lineage_outbox_uri,
        storage_options=settings.storage_options(),
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
    app.state.dapr_client = dapr_client
    app.state.fga_client = await _make_fga_client(settings)
    app.state.s3_client = _make_s3_client(settings)
    app.state.startup_complete = True
    try:
        yield
    finally:
        app.state.shutting_down = True
        # Same disposal as every sibling. Maintenance stores it as `app.state.fga_client` rather than
        # `fga`; the shared disposer covers both names so the difference stops mattering.
        await fga_dispose(app)
        if dapr_client is not None:
            with suppress(Exception):
                await dapr_client.close()


# THE SHARED LANCE-PLANE ASSEMBLY (open_python-audit DUP-12). Logging before the app exists, the docs
# gate, the handler pair in the order that makes it work, one request id, and the probes — see
# `service_kit.lance_app` for what each of those five is for and what a copy of it got wrong.
app = build_lance_service_app(
    title="Lance table maintenance",
    docs_enabled=get_settings().docs_enabled,
    lifespan=lifespan,
    log=log,
)

# The Dapr cron route (POST /<binding-name>) + its OPTIONS discovery ack, with the require_dapr_token gate.
# BUILT from the settings this app was assembled with, not stamped into the routes module at import.
app.include_router(build_router(get_settings()))

# The work subscription (POST /maintenance-work), registered only when a work topic is configured — the
# same condition the cron route uses to choose its lane, so a deployment cannot advertise a subscription
# for a queue it never publishes to.
_work_app = register_work_route(app, get_settings())

# The write-event subscription (POST /maintenance-arrival) — the PRIMARY trigger. Shares the DaprApp
# the work route built: a second DaprApp(app) re-registers /dapr/subscribe and the sidecar reads only
# one of them, so one subscription would silently never be delivered to.
_work_app = register_arrival_route(app, get_settings(), _work_app) or _work_app

# The index-build subscription (POST /maintenance-index). Its own topic beside the work queue —
# one ackWait cannot serve both a minutes-long compaction and a vector index over a large table —
# but the SAME DaprApp, for the reason the line above states.
register_index_route(app, get_settings(), _work_app)
