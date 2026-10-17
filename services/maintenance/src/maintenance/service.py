"""Table maintenance service — a FastAPI app driven by Dapr cron **input bindings** and one write-event subscription.

Maintenance decides and does not rewrite (ADR 0155, ADR 0156 C4 and C6, ADR 0162). ``bindings.cron`` components POST
to ``/<binding-name>`` every interval. Two bindings, two jobs:

* the **sweep** discovers every Lance dataset across the swept buckets, expires each one's superseded versions
  (``cleanup_old_versions`` behind the refusal ladder that guards reclamation), and writes a due record for each table
  a bring-your-own job should compact or reindex;
* the **reconcile** pass reads OpenFGA, the control-root registries and object storage and REPORTS where they
  disagree — then, ONLY if that report ran clean, purges expired trash records (#79). Off by default.

With ``MAINTENANCE_ARRIVAL_ENABLED``, ``/maintenance-arrival`` refreshes a written table's due record between ticks.
Blocking Lance/S3 IO runs in the threadpool so the event loop stays free. Run: ``uvicorn maintenance.service:app``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from typing import Any

from dapr.aio.clients import DaprClient
from fastapi import FastAPI

from lineage_kit import SigningKey, attach_control_signature, parse_published_keys
from maintenance.api.arrival import register_arrival_route
from maintenance.api.routes import build_router
from maintenance.core.config import MaintenanceSettings, get_settings
from maintenance.core.lineage_emit import make_emitter
from service_kit.bus_watchdog import start_bus_watchdog
from service_kit.control_emit import ControlSign, make_control_emitter
from service_kit.draining import arm_drain_on_sigterm
from service_kit.governed.auth_lifespan import build_fga_client
from service_kit.governed.dapr_auth import assert_app_token_configured
from service_kit.governed.fga import OpenFgaCredentialRefusedError
from service_kit.governed.fga import dispose as fga_dispose
from service_kit.governed.signing_key import SigningKeyHolder, attach_signing, make_signing_holder, signing_ready_check
from service_kit.governed.store_credential import make_store_credential
from service_kit.lakehouse.lance_metrics import instrument_lance_if_available
from service_kit.lance_app import build_lance_service_app
from service_kit.obs import configure_app_logging


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

    Neither may AUTHOR the estate's model, so this passes ``provision=False`` — the estate-wide
    default since 2026-09-09, stated here because the sweep runs outside a lifespan: the shared bootstrap
    then takes `fga.resolve`, which is read-only, can never create a store or write a model, and
    returns ``None`` when there is no store, or no model this image carries, by its deadline. That principle once covered the LOOKUP too,
    and the cost is recorded in `fga.resolve`'s own docstring — on the chart's DEFAULT posture
    (`auth.fgaStoreId: ""`, a per-cluster ULID that cannot be a committed default) refusing to look up
    meant reporting every authz category unavailable against an estate that was right there. Reading
    which store exists is not authoring one.

    NEVER RAISES, which is why ``fatal`` stays at its default. A misconfigured or unreachable authz
    endpoint degrades the authz categories; it must not stop the sweep, which is this service's
    primary job and needs no FGA at all. It also returns the client rather than assigning it: the
    sweep runs from a cron route and holds it as ``app.state.fga_client``. That holds for a refused
    credential too, which `build_fga_client` raises for every other service ([[XC-077]]): here it is logged
    as an error and the authz categories report unavailable while the sweep runs on.
    """
    try:
        return await build_fga_client(settings, service="maintenance", provision=False)
    except OpenFgaCredentialRefusedError:
        log.exception("openfga_credential_refused", extra={"service": "maintenance"})
        return None


def require_env_pair(settings: MaintenanceSettings) -> None:
    """With the secret store off, refuse to boot without both halves of the environment's key pair.

    The sweep is a real S3 consumer, so an empty secret would fail every pass with a cryptic
    `SignatureDoesNotMatch` instead of a clear startup error. The key half is refused because any default for
    it would be the store's root account, whose credential reaches every tenant's bytes and the
    `_projects/`/`_protection/` records that govern maintenance itself. A credential is a pair; refusing only the
    secret half leaves the dangerous half unguarded.
    """
    if not settings.s3_secret_access_key.get_secret_value():
        raise RuntimeError("MAINTENANCE_S3_SECRET_ACCESS_KEY is required (set it, or enable MAINTENANCE_SECRETS_FROM_DAPR)")
    if not settings.s3_access_key_id:
        raise RuntimeError("MAINTENANCE_S3_ACCESS_KEY_ID is required (set it, or enable MAINTENANCE_SECRETS_FROM_DAPR): no key id defaults to the store's root")


def _control_sign(holder: SigningKeyHolder[SigningKey]) -> ControlSign:
    """The purge's control events, signed as this service with no delegation: maintenance authenticates nobody, so the
    actor it stamps is itself (`purge.ACTOR`) and it never signs for a person."""
    return lambda envelope: attach_control_signature(envelope, key=holder.key(), identity=holder.identity)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    app.state.startup_complete = False
    app.state.shutting_down = False
    # ARMED ON THE SIGNAL, not on lifespan unwind: the `finally` below runs after uvicorn has stopped serving, so
    # `retry_when_draining` on the arrival route could never refuse a delivery.
    _disarm_drain = arm_drain_on_sigterm(app)
    settings = get_settings()
    instrument_lance_if_available()  # Lance-native IO metrics onto the global MeterProvider
    # Fail closed if behind a Dapr sidecar but the app-token is unset — the cron route would otherwise be an
    # open forged-sweep path (symmetric with the lineage service). No-op in dev (dapr_enabled off).
    assert_app_token_configured(dapr_enabled=settings.dapr_enabled)
    # The S3 key pair from the Dapr secret store (OpenBao) before the first cron sweep, so the sweep signs with a
    # store-held pair and the plaintext secret never ships in pod env: read now, failing the boot closed if the
    # store never answers with both halves, then again while the service runs, so a pair rotated in the store is
    # the one the sweep signs with within its TTL ([[XC-082]], ADR 0166). With the store off, the environment's
    # pair is refused here when either half is missing.
    store_credential = make_store_credential(settings)
    if store_credential is None:
        require_env_pair(settings)
    else:
        await store_credential.start()
    app.state.store_credential = store_credential
    # ONE sidecar client for both emitters — lineage (data) and control (governance). Built when either is on; the
    # sidecar is local, so construction is cheap and needs no broker reachability at boot.
    dapr_client = DaprClient() if (settings.lineage_emit_enabled or settings.control_emit_enabled) else None
    # THIS SERVICE'S OWN SIGNING KEY ([[LH-064]]), or None for a stack with no store or no identity. It is also the author every event stamps, which the emitter checks
    # at construction: a verifier refuses a signer that is not its event's author.
    signing = make_signing_holder(
        secrets_from_dapr=settings.secrets_from_dapr,
        identity=settings.signing_identity,
        store=settings.dapr_secret_store,
        load_key=SigningKey.from_seed,
        parse_published=parse_published_keys,
    )
    attach_signing(app, signing)
    app.state.lineage_emitter = make_emitter(
        enabled=settings.lineage_emit_enabled,
        dapr=dapr_client,
        pubsub=settings.lineage_pubsub,
        topic=settings.lineage_topic,
        job_namespace=settings.lineage_job_namespace,
        timeout_seconds=settings.publish_timeout_seconds,
        outbox_uri=settings.lineage_outbox_uri,
        storage_options=settings.storage_options,
        # The SAME identity this service presents at the catalog's service door, so the graph and the
        # catalog cannot disagree about who maintained a dataset. A signer must sign as it: the emitter refuses to
        # be built with a key for any other identity.
        author=settings.catalog_service_identity,
        signing=signing,
    )
    # #79: the expired-trash purge announces each reclamation on the catalog's control topic. A no-op
    # when off — never a half-configured transport that looks like it publishes.
    app.state.control_emitter = make_control_emitter(
        enabled=settings.control_emit_enabled,
        dapr=dapr_client,
        pubsub=settings.control_pubsub,
        timeout_seconds=settings.publish_timeout_seconds,
        service="maintenance",
        # SIGNED AS THIS SERVICE ([[XC-078]]) with the holder its lineage signs with; None for a stack with no store or
        # no identity. Nothing stages a control event here, so one the purge emits while the key is unresolved is
        # withheld and counted, never published unsigned.
        sign=_control_sign(signing) if signing is not None else None,
    )
    # The reconciler's read-only FGA client. OPTIONAL by design: a missing one degrades its categories to
    # UNAVAILABLE-with-a-reason, and the others still report. Boot must NOT fail on it — the sweep is this
    # service's primary job and does not need it. Its bucket-listing client is built per tick, with the pair
    # in force (`services.reconcile`).
    app.state.dapr_client = dapr_client
    app.state.fga_client = await _make_fga_client(settings)
    # RESOLVED LAST, so the probes and the retry answers are served from the moment the app boots whether or not the
    # key resolved: a service that is waiting for its key takes no delivery (`retry_until_signed` on its routes),
    # reports itself not ready, and heals in place when the key is published.
    if signing is not None:
        await signing.start()
    # Restarts this pod's sidecar once one of its bus connections has closed for good, as daprd's do after a long bus
    # outage (`service_kit.bus_watchdog`); None where the chart names this pod no bus.
    bus_watchdog = await start_bus_watchdog()
    app.state.startup_complete = True
    try:
        yield
    finally:
        _disarm_drain()
        app.state.shutting_down = True
        if bus_watchdog is not None:
            await bus_watchdog.stop()
        if signing is not None:
            await signing.stop()
        if store_credential is not None:
            await store_credential.stop()
        # Same disposal as every sibling. Maintenance stores it as `app.state.fga_client` rather than
        # `fga`; the shared disposer covers both names so the difference stops mattering.
        await fga_dispose(app)
        if dapr_client is not None:
            with suppress(Exception):
                await dapr_client.close()


# THE SHARED LANCE-PLANE ASSEMBLY (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit" DUP-12). Logging before the app exists, the docs
# gate, the handler pair in the order that makes it work, one request id, and the probes — see
# `service_kit.lance_app` for what each of those five is for and what a copy of it got wrong.
app = build_lance_service_app(
    title="Lance table maintenance",
    docs_enabled=get_settings().docs_enabled,
    audit_enabled=get_settings().audit_enabled,
    lifespan=lifespan,
    log=log,
    ready_check=signing_ready_check(),
)

# The Dapr cron route (POST /<binding-name>) + its OPTIONS discovery ack, with the require_dapr_token gate.
# BUILT from the settings this app was assembled with, not stamped into the routes module at import.
app.include_router(build_router(get_settings()))

# The write-event subscription (POST /maintenance-arrival), registered only when the lane is enabled.
register_arrival_route(app, get_settings())
