"""Lance Namespace REST Catalog — FastAPI application entry.

A Python/FastAPI adapter exposing the full Lance Namespace REST API (spec.yaml)
over a native ``lance.namespace`` backend (``DirectoryNamespace`` on the object store by
default), with the pylance data plane filling operations the backend stubs.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from functools import partial
from typing import Final, Literal, get_args

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import LanceNamespace, LanceNamespaceError, ServiceUnavailableError
from openfga_sdk import OpenFgaClient
from opentelemetry import metrics
from starlette.middleware import Middleware

from catalog.api.authz import decisions
from catalog.api.control_lane import STREAM as CONTROL_STREAM
from catalog.api.control_lane import TOPIC as CONTROL_TOPIC
from catalog.api.control_lane import ControlLane, create_signature_refused_series
from catalog.api.control_relay import attach_published_keys
from catalog.api.dependencies import app_wire, declared_base_judge
from catalog.api.load_shed import WriteConcurrencyLimitMiddleware
from catalog.api.maintenance_mode import maintenance_middleware
from catalog.api.path_reading import PathReadingMiddleware
from catalog.api.security import authenticate, judge
from catalog.api.signing_gate import commit_ids_ready_check, signing_gate_middleware
from catalog.api.timers import start_timers
from catalog.api.v1.router import api_router
from catalog.core import base_judge
from catalog.core.commit_ids import STATE as COMMIT_IDS_STATE
from catalog.core.commit_ids import CommitIdKeyHolder, CommitIds, make_commit_ids
from catalog.core.config import CatalogReadBounds, CatalogStore, Settings, get_settings
from catalog.core.control_signing import control_sign
from catalog.core.lineage_emit import make_emitter
from catalog.core.namespace import NamespaceConnections
from catalog.core.vending import EncryptionAtRest, make_vendor
from catalog.services import provenance_namespace, warehouses
from lakehouse.events.bus import Bus, bus_dependency, bus_session
from lakehouse.events.control_emit import make_control_emitter
from lakehouse.events.signing import SigningKey, parse_published_keys
from lakehouse.governed.admission import judge_before_the_body
from lakehouse.governed.auth_lifespan import BootFailure, ModelRole, attach_auth
from lakehouse.governed.fga_provision import dispose as fga_dispose
from lakehouse.governed.secrets import secret_store_configured, secret_store_session
from lakehouse.governed.settings import assert_authentication_configured
from lakehouse.governed.signing_key import SigningKeyHolder, attach_signing, make_signing_holder, secret_store_dependency, signing_ready_check
from lakehouse.lance_metrics import register_lance_metrics
from lakehouse.problems.outage import reads_as_store_outage
from lakehouse.process.health import Dependency, HealthWatch, openfga_dependency
from lakehouse.process.supervision import Supervisor
from lakehouse.schemas.health import Readiness
from lakehouse.service_app.factory import build_service_app
from lakehouse.storage.conditional_records import RecordChangedError
from lakehouse.storage.objectfs import store_dependency
from lakehouse.storage.store_session import require_env_pair, start_store_session


log = logging.getLogger(__name__)

#: The backoff of the provenance namespace's boot task: doubling from the first delay, capped at the second.
_PROVENANCE_RETRY_FIRST_SECONDS = 1.0
_PROVENANCE_RETRY_MAX_SECONDS = 60.0
#: What the boot task reads as an OUTAGE by type alone, a dependency that did not answer: an unanswered OpenFGA, a store
#: credential not yet confirmed and a store that did not take the connection are ``ServiceUnavailableError``
#: (`fga_resilience.guarded`, `StoreCredentialUnavailableError`, `namespace._connected`); a record another replica changed
#: meanwhile is ``RecordChangedError``; a socket refused or timed out is Python's ``ConnectionError`` or ``TimeoutError``.
#: Any other failure is an outage only where it SAYS the store did not answer (`reads_as_store_outage`: no response, a
#: 5xx or a 429, in the native backend's, pylance's, pyarrow's or botocore's words, read through ``__cause__`` where a
#: seam re-raised the client's error), and otherwise a FAULT: a store that answered 403 or 404 (a refused or expired
#: credential, a missing bucket), a record no reader can parse, a defect. The kind is told only on the log line and the
#: counter, never in what the task does, so an operator pages on a fault sooner than on a dependency still starting.
_OUTAGE_TYPES: Final = (ServiceUnavailableError, RecordChangedError, ConnectionError, TimeoutError)
#: How much of a failure's text a log line carries, as the catalog's other failure lines do.
_ERROR_TEXT_CHARS: Final = 300

_failed_tries = metrics.get_meter("lance.catalog").create_counter(
    "catalog.estate_namespaces.failed",
    unit="{try}",
    description="Failed tries of the catalog's estate-namespaces boot task (the estate warehouse's row and the provenance "
    "namespace), each retried, by kind: `outage` (the store or OpenFGA did not answer) or `fault` (anything else: an "
    "answer refusing the call, a record no reader can parse, a defect).",
)
#: The kind of a failed try, each a series of `catalog.estate_namespaces.failed`.
type _TryFailure = Literal["outage", "fault"]


def _failure_kind(exc: Exception) -> _TryFailure:
    """Whether a failed try of the boot task met a dependency that did not answer, or a fault no wait clears."""
    if isinstance(exc, _OUTAGE_TYPES) or reads_as_store_outage(exc):
        return "outage"
    return "fault"


async def _ensure_estate_namespaces(app: FastAPI) -> None:
    """Keep the estate warehouse's row and the provenance namespace, retrying every failed try until both hold (ADR 0203).

    Raises:
        InternalError: The lifespan wired no settings, no connections or no authorization client, not even its ``None``.
    """
    from catalog.services.provenance_namespace import ensure_provenance_namespace

    # A wire the lifespan did not set is no failure a retry outlasts: read once, it ends the task, and the supervisor the
    # process (`app_wire`). A missing authorization client where authorization is on is retried as the outage it may be
    # (`decisions.wired`), never read as off: a namespace made with no grant would leave lineage unable to write its feed.
    settings = app_wire(app, "settings", Settings)
    connections = app_wire(app, "connections", NamespaceConnections)
    client = app_wire(app, "fga", OpenFgaClient, none_is_off=True)
    for kind in get_args(_TryFailure.__value__):
        # Each kind's series exists at 0 from the task's start, so the first failed try is an increase a rule reads.
        _failed_tries.add(0, {"lance.catalog.kind": kind})
    delay = _PROVENANCE_RETRY_FIRST_SECONDS
    previous: tuple[type[Exception], _TryFailure] | None = None
    while True:
        try:
            # The row first ([[LH-593]]): every name served from the estate's own root, the provenance namespace's
            # among them, begins with the estate warehouse's id. Every step is idempotent.
            namespace = await run_in_threadpool(connections.connection, settings)
            await run_in_threadpool(warehouses.ensure_catalog_row, namespace, settings.estate_warehouse)
            await ensure_provenance_namespace(namespace, settings, decisions.wired(settings, client))
            return
        # THE TASK HOLDS on every failure of converging and never ends the replica, which is Ready and serving and whose
        # readiness does not wait on it: ending it would take away a serving replica, and for a fault every replica
        # meets, every replica and the policy door that repairs it. What is no ``Exception`` (a request to stop, a
        # native panic) is not caught here.
        except Exception as exc:  # noqa: BLE001 — logged and counted, then retried
            kind = _failure_kind(exc)
            _failed_tries.add(1, {"lance.catalog.kind": kind})
            # The traceback on the first failure and on each change of type or kind, so a task held for hours repeats one
            # line a minute, not one traceback a minute.
            changed = (type(exc), kind) != previous
            previous = (type(exc), kind)
            log.warning(
                "provenance_namespace_error",
                extra={"error": str(exc)[:_ERROR_TEXT_CHARS], "error_type": type(exc).__name__, "kind": kind, "retry_in_seconds": delay},
                exc_info=exc if changed else None,
            )
        await asyncio.sleep(delay)
        delay = min(delay * 2, _PROVENANCE_RETRY_MAX_SECONDS)


async def _backfill_warehouse_grants(app: FastAPI) -> None:
    """Run the warehouse-grant backfill, reporting rather than raising. See its call site for why."""
    from catalog.api.warehouse_grant_backfill import backfill

    try:
        seen, written, failures = await backfill(app.state.settings)
    except Exception as exc:
        log.warning("warehouse_grant_backfill_error", extra={"error": str(exc)}, exc_info=exc)
        return
    if seen:
        log.info("warehouse_grant_backfill_done", extra={"warehouses": seen, "tuples": written, "failures": len(failures)})


def bound_lance_scans(settings: CatalogReadBounds) -> None:
    """Give every scan this process builds the catalog's bounds, unless the operator set Lance's own variable ([[LH-247]]).

    `/query` and `analyze_plan` are served by the native backend, which builds its scanner in Rust from
    the spec request, so no scanner option reaches it from Python; Lance's process defaults are the one
    lever that bounds those scans and the change feed's alike, and they are read when a scan is built,
    so setting them here, before the app serves, bounds every one. ``LANCE_DEFAULT_IO_BUFFER_SIZE`` is
    documented (pylance 12.0.0 `ScannerBuilder.io_buffer_size`). ``LANCE_DEFAULT_BATCH_SIZE`` and
    ``LANCE_DEFAULT_FRAGMENT_READAHEAD`` are NOT in `lance_docs/` or pylance's docstrings: they are
    named in the 12.0.0 binary and their effect was measured, so a pylance upgrade re-measures them.

    MEASURED on pylance 12.0.0 from outside a one-CPU catalog process over a moto store, tables of
    1024-dim float32 vectors, pod limit 524,288 kB, Lance's defaults against these three:

        change feed, 164 MB table                       597,224 kB  ->  361,572 kB
        unindexed vector /query k<=2048, 164 MB table   627,404 kB  ->  440,648 kB
        unindexed k=10 vector /query, 492 MB table      824,824 kB  ->  466,296 kB (IO buffer alone)

    The last row is why the bound cannot be a cap on `k` alone: a flat vector search reads the whole
    vector column whatever `k` is.
    """
    os.environ.setdefault("LANCE_DEFAULT_IO_BUFFER_SIZE", str(settings.scan_io_buffer_mb << 20))
    os.environ.setdefault("LANCE_DEFAULT_BATCH_SIZE", str(settings.scan_batch_rows))
    os.environ.setdefault("LANCE_DEFAULT_FRAGMENT_READAHEAD", "1")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    app.state.settings = settings
    app.state.shutting_down = False
    app.state.startup_complete = False
    # Q17-6 / §F2-2: the CODE default is anonymous and the chart flips it on, so a catalog started
    # any other way serves every route to anyone who can reach the port. Refused on the AMBIGUITY —
    # an operator declares which they meant. See `assert_authentication_configured`.
    assert_authentication_configured(
        oidc_enabled=settings.oidc_enabled,
        insecure_allow_unauthenticated=settings.insecure_allow_unauthenticated,
    )
    register_lance_metrics()  # Lance-native IO metrics onto the global MeterProvider
    bound_lance_scans(settings)
    # EVERYTHING ACQUIRED BELOW IS RELEASED BY THIS STACK, in reverse, whether the start completes or fails part-way
    # (ADR 0203), so a start that fails after the store session or the bus session is opened leaves neither open.
    async with AsyncExitStack() as stack:
        # Every background loop and task this process runs is supervised here: one that ends unasked ends the process.
        loops = await stack.enter_async_context(Supervisor())
        await _build(app, settings, stack=stack, loops=loops)
        # Registered last, so run FIRST: every background loop and task is stopped while the clients it uses are open.
        stack.push_async_callback(loops.stop)
        app.state.startup_complete = True
        try:
            yield
        finally:
            app.state.shutting_down = True


async def _build(app: FastAPI, settings: Settings, *, stack: AsyncExitStack, loops: Supervisor) -> None:
    """Build what the catalog serves with onto ``app.state``, each piece's release pushed onto ``stack`` as it is built,
    and its background loops and tasks started on ``loops``."""
    namespace = await _open_the_store(app, settings, stack=stack, loops=loops)
    await _attach_authorization_and_vending(app, settings, stack=stack, loops=loops)
    # THIS PROCESS'S ONE CONNECTION TO THE BUS ([[XC-199]]), as the catalog's own bus user, which both emitters and the
    # control relay publish on (`lakehouse.events.bus`). A loop of `loops` connects it behind the start and keeps it, so the
    # catalog serves, and stages what it announces, while the bus is away. Entered before everything that publishes, so
    # it is drained after them. None for a stack the chart names no bus.
    bus = await stack.enter_async_context(bus_session(loops, settings.bus))

    signing = make_signing_holder(identity=settings.signing_identity, load_key=SigningKey.from_seed, parse_published=parse_published_keys)
    attach_signing(app, signing)
    # The reader the relay and the control lane verify with, built per start and before either can ask for it.
    attach_published_keys(app)
    # THE KEY EVERY COMMIT'S RUN ID IS DERIVED UNDER ([[LH-497]]): read from the store like the signing seed, or the empty
    # key for a stack with no store. Published for the gate, the readiness check and the reconciler.
    commit_ids = make_commit_ids()
    setattr(app.state, COMMIT_IDS_STATE, commit_ids)
    _attach_emitters(app, settings, bus=bus, signing=signing, commit_ids=commit_ids)
    # RESOLVED LAST, so the probes and the app are up whether or not the key resolved: a catalog that is waiting for
    # its key reports itself not ready and heals in place when the key is published, and a write it has already
    # committed is never failed by an emit that cannot be signed; that emit is withheld, lineage and control alike.
    if signing is not None:
        await signing.start(loops)
        stack.push_async_callback(signing.stop)
    if isinstance(commit_ids, CommitIdKeyHolder):
        await commit_ids.start(loops)
        stack.push_async_callback(commit_ids.stop)
    # Re-assert the warehouse grants over warehouses that already exist. BACKGROUND and non-fatal, and
    # both of those are the design rather than caution:
    #
    #   * The grants are written ONCE, at warehouse-create, from `LANCE_FGA_MAINTAINERS` — a value that
    #     CHANGES. Adding a subject extends the list and every older warehouse is missing it, so that
    #     subject is refused on every existing tenant, in a log nobody watches.
    #   * A hook Job would need a store identity and an OpenFGA writer's token of its own, for one
    #     registry read and one write this process already holds both for, so the work belongs here.
    #   * NOT awaited, so a slow or unreachable OpenFGA cannot delay readiness, and NOT fatal, because
    #     a grant that could not be re-asserted must not take the catalog down — it leaves the estate
    #     exactly as it was, which is the state this repairs, not a worse one.
    #   * Per-replica repetition is harmless: every write is idempotent (`write_tuples` retries
    #     one-by-one past a duplicate), so replicas racing land the same tuples.
    loops.start("warehouse-grant-backfill", partial(_backfill_warehouse_grants, app))
    # The estate's own warehouse row and the namespace lineage keeps its tables in, in the background for the
    # backfill's reasons: lineage retries its tables until the namespace exists, so a slow OpenFGA or store must not
    # delay this catalog's readiness. A name under the estate's warehouse is absent until the row is there.
    loops.start("estate-namespaces", partial(_ensure_estate_namespaces, app))
    _follow_control_events(app, settings, bus, loops=loops)
    # What `/readyz` answers, read in the background so the probe never waits on a dependency or a request thread
    # (`lakehouse.process.health`, ADR 0203). The store and OpenFGA gate: no door serves without them. The bus is reported only,
    # since every announcement it carries is staged in an outbox first. The namespace this catalog serves is a fact
    # read once here: a backend that cannot answer `namespace_id()` is no readiness fault, and per-request resolution
    # surfaces that as a domain error.
    with suppress(LanceNamespaceError):
        app.state.readiness_facts = {"namespace": str(namespace.namespace_id())}
    app.state.health = HealthWatch(_dependencies(app, settings, bus))
    app.state.health.watch(loops)
    # THE CATALOG'S FIXED TIMERS ([[XC-199]]), started last, once everything a tick works with is on `app.state`: the
    # control relay, the erasure release, the commit reconcile, the manifest reclaim and the
    # declaration converge, each a loop of `loops` on the period git gives it, each window claimed on the control root
    # so a tick runs once whatever the replica count (`catalog.api.timers`).
    start_timers(app, settings, loops)


async def _open_the_store(app: FastAPI, settings: Settings, *, stack: AsyncExitStack, loops: Supervisor) -> LanceNamespace:
    """Log in to the secret store, start the store session and open the namespace this catalog serves."""
    # THE CATALOG LOGS IN TO ITS SECRET STORE ITSELF ([[XC-199]]), before anything below reads a secret, and refuses to
    # start when the store never admits it, whichever secrets it goes on to read. A loop of `loops` then logs in again
    # once half the store token's lease is used, or as soon as the store no longer honours it. Nothing for a stack with
    # no secret store.
    await stack.enter_async_context(secret_store_session(loops))
    # THE CATALOG HOLDS NO STORE KEY ([[XC-083]], ADR 0170). Its identity is the ServiceAccount token its pod projects
    # for the store's audience, exchanged here for a session (`AssumeRoleWithWebIdentity`), waiting out a store or a
    # provisioning hook still starting, and the boot FAILS CLOSED if none is issued; a background loop exchanges again
    # once half the session's life is used, so every request signs with an unexpired session and no restart is ever
    # needed. Every `storage_options()` read peeks at what that loop holds, and nothing built here keeps a credential of
    # its own: the connections are keyed on it, the judge reads it at each use, and the emitters take
    # `storage_options` as a callable. With no token (the offline and Dagger lanes) the environment's pair signs.
    store_session = await start_store_session(settings.web_identity, session_name="catalog", duration_seconds=settings.s3_session_seconds, loops=loops)
    if store_session is None:
        # A ValueError when the environment's key pair may not sign.
        require_env_pair(settings, prefix="LANCE")
    else:
        stack.push_async_callback(store_session.stop)
    app.state.store_session = store_session
    app.state.connections = NamespaceConnections()
    namespace = app.state.connections.connection(settings)  # fail fast if storage misconfigured
    # The estate's own warehouse row before the first request is served: every name on the estate's root begins with
    # it, and the estate-namespaces loop below writes it only once its task first runs. A store that is still starting
    # is that loop's to retry, and must not fail this boot.
    try:
        await run_in_threadpool(warehouses.ensure_catalog_row, namespace, settings.estate_warehouse)
    except Exception as exc:  # noqa: BLE001 — retried by the loop below
        log.warning("estate_warehouse_row_deferred", extra={"error": str(exc)})
    return namespace


async def _attach_authorization_and_vending(app: FastAPI, settings: Settings, *, stack: AsyncExitStack, loops: Supervisor) -> None:
    """Attach the authorization plane, the credential vendor and the base judge, each release pushed onto ``stack``."""
    # `BootFailure.CRASH` KEEPS THIS SERVICE'S POSTURE: neither construction was wrapped in a `try`, so a
    # failed build has always crashed the pod. The catalog is the estate's authorization SOURCE — it
    # writes the grants every other service reads — so a boot that cannot reach OpenFGA must be
    # visible as a CrashLoopBackOff, not as a fleet of ready pods answering 503 to everyone.
    stack.push_async_callback(fga_dispose, app)
    await attach_auth(
        app,
        settings,
        service="catalog",
        loops=loops,
        on_failure=BootFailure.CRASH,
        # THE ESTATE'S ONE PUBLISHER OF THE AUTHORIZATION MODEL. Every other service resolves what
        # this one wrote; `build_fga_client` defaults to the read-only half for that reason.
        # The catalog is the publisher because it owns the governed plane the model describes, it
        # is `BootFailure.CRASH` so a failure to build is a refusal to serve rather than an open door,
        # and its declaration converge writes the estate's first tuples against its objects.
        model_role=ModelRole.PUBLISHER,
    )
    # Credential vending (data plane): turn an authorized (table location, tier) into the scoped
    # storage_options a client uses to reach object storage DIRECTLY. mode_b (default) vends nothing —
    # clients use the server-mediated Arrow-IPC endpoints; web_identity exchanges the catalog's own token for a
    # session narrowed to the table and tier by an inline session policy. Built once from config (see core/vending.py).
    app.state.vendor = make_vendor(
        settings.vending_mode,
        identity=settings.web_identity,
        store_endpoint=settings.vended_store_endpoint,
        ttl_seconds=settings.vending_ttl_seconds,
        encryption=EncryptionAtRest(
            algorithm=settings.s3_server_side_encryption,
            kms_key_id=settings.s3_sse_kms_key_id,
            bucket_key_enabled=settings.s3_sse_bucket_key_enabled,
        ),
        # THE OPERATOR'S allowlist of legitimate foreign DATA bases. A manifest-declared base outside the
        # table's own vended scope is granted READ only if it is on it; without that, a writer on one table
        # could declare a base naming another tenant's prefix and read it with their next credential. The
        # external blob bases are not on it ([[LH-209]]): they are pointer bases the blob door serves, and
        # granting one would hand every vend every object under it. Empty by default.
        sanctioned_bases=settings.multibase_data_base_list,
    )
    # [[LH-279]] WHAT EVERY TABLE READ JUDGES A DECLARED BASE AGAINST: the control root the create and
    # register doors write each table's base record to. Installed
    # as the settings it is built from, so each read signs its record reads with the pair in force, and
    # cleared at shutdown so nothing in this process outlives the app that configured it.
    base_judge.install(partial(declared_base_judge, settings))
    stack.callback(base_judge.install, None)


def _attach_emitters(app: FastAPI, settings: Settings, *, bus: Bus | None, signing: SigningKeyHolder[SigningKey] | None, commit_ids: CommitIds) -> None:
    """Attach the lineage emitter, the bus and the control emitter, each publishing on ``bus`` and signing with ``signing``."""

    # WHICH TENANT a write belongs to, for the emitted `rask_lance.project` facet — WATCH targeting's only
    # key. Without it a notifier skips the watcher loop entirely and every catalog write in the
    # estate reaches its author and nobody else.
    #
    # A name's project is its warehouse's, one registry read in the threadpool (pyarrow's filesystem is
    # blocking). Best-effort by construction: `project_for` swallows a failure and degrades to `None`.
    async def _resolve_project(warehouse_id: str) -> str | None:
        return await run_in_threadpool(warehouses.project_of, settings.registry_root, settings.storage_options(), warehouse_id)

    app.state.lineage_emitter = make_emitter(
        enabled=settings.lineage_emit_enabled,
        bus=bus,
        topic=settings.lineage_topic,
        job_namespace=settings.lineage_job_namespace,
        dataset_namespace=settings.lineage_dataset_namespace,
        timeout_seconds=settings.publish_timeout_seconds,
        project_resolver=_resolve_project,
        # The provenance namespace's own rows are never announced (`services.provenance_namespace`).
        unannounced=lambda namespace, operation: provenance_namespace.is_unannounced(namespace, operation, settings),
        outbox_uri=settings.lineage_outbox_uri,
        storage_options=settings.storage_options,
        # The catalog's self-description for the standard `catalog` facet ([[LIN-002]]): the Lance
        # Namespace implementation it is reached through, and where the data it governs physically
        # lives. Both are read from the SAME settings the catalog serves from, so the facet cannot
        # describe a catalog other than the one answering.
        catalog_impl=settings.impl,
        warehouse_uri=settings.root,
        # THIS CATALOG'S OWN SIGNING KEY ([[LH-064]]), or None for an estate with no store or no identity. The holder
        # is built here and started with the app below: an emitter never reaches for the store itself.
        signing=signing,
        commit_ids=commit_ids,
    )
    # Control-plane change-events (opt-in, best-effort — the governance/metadata stream). Publishes on
    # the same connection to the bus, from whose stream the poll endpoint reads; the emitter is a no-op when off.
    # The connection itself, published for callers that are not the emitter. The control relay
    # (api/control_relay.py) re-publishes the STAGED event, never a re-serialization of the model, so it
    # cannot go through `control_emitter`, which would rebuild the event from the model and stage it
    # again. `None` when the chart names this process no bus.
    app.state.bus = bus
    app.state.control_emitter = make_control_emitter(
        enabled=settings.control_emit_enabled,
        bus=bus,
        timeout_seconds=settings.publish_timeout_seconds,
        service="catalog",
        # SIGNED AS THIS CATALOG ([[XC-078]]), with the holder the lineage emitter signs with, for the person each
        # event names, BEFORE it is staged; None for a catalog with no store or no identity. An event it cannot sign is
        # withheld, never staged. The relay signs nothing: it delivers only staged bytes that verify as signed by the
        # identity of the holder `attach_signing` above published, against the keys that identity publishes.
        sign=control_sign(signing) if signing is not None else None,
        # Staged when configured, plain publish when not — opt-in, exactly like the lineage outbox.
        outbox_uri=settings.control_outbox_uri,
        storage_options=settings.storage_options,
    )


def _follow_control_events(app: FastAPI, settings: Settings, bus: Bus | None, *, loops: Supervisor) -> None:
    """Follow the control topic on a loop of ``loops``, judging and counting each event, where control eventing is on
    (`catalog.api.control_lane`): the lane runs under the emitter's own gate, so a control-off deployment follows
    nothing. Stopped with the other loops."""
    if bus is None or not settings.control_emit_enabled:
        return
    create_signature_refused_series()
    loops.keep("catalog-control-lane", partial(bus.follow, CONTROL_STREAM, CONTROL_TOPIC, ControlLane(app, settings).handle))


def _dependencies(app: FastAPI, settings: CatalogStore, bus: Bus | None) -> list[Dependency]:
    """What the catalog reads to know it can serve: its store under its own session (the control root's listing), OpenFGA
    under its own credential, the bus where the chart names one, and the secret store its keys are read from."""
    dependencies = [
        store_dependency(settings.registry_root, settings.storage_options, required=True),
        *openfga_dependency(app, required=True),
        *bus_dependency(bus),
    ]
    if secret_store_configured():
        dependencies.append(secret_store_dependency())
    return dependencies


async def _catalog_ready(request: Request) -> Readiness:
    """The verdicts the health watch keeps, with the namespace this catalog serves; never a read of its own."""
    state = request.app.state
    return state.health.readiness(getattr(state, "readiness_facts", None))


_settings = get_settings()
# THE SHARED LANCE-PLANE ASSEMBLY (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit" DUP-12). Logging before the app exists, the docs
# gate, the handler pair in the order that makes it work, one trace per request, every layer a request passes, and the
# probes — see `lakehouse.service_app.factory` for what each is for and what a copy of it got wrong.
#
# `maintenance_middleware` (the read-only gate, a no-op unless LANCE_MAINTENANCE_READ_ONLY=true) and
# `signing_gate_middleware` (the writes a catalog without its signing key refuses) go in as `inner_middleware`, inside
# the request's trace, so their 503 names the trace (the problem body's `instance`) an operator correlates the refusal with.
#
# The OUTER layers, in order: the write shed answers 429 once the concurrent-write cap is reached, when a write begins
# to read its body and before a byte of it is buffered, so shedding relieves memory pressure rather than adding to it;
# the path reading, outermost of the two, reads the request's path as the client spelled its names, its public prefix
# stripped and a bare `+` read as a space ([[LH-526]]), so every layer beneath reads one path.
app = build_service_app(
    title="Lance Namespace REST Catalog",
    version="1.0.0",
    docs_enabled=_settings.docs_enabled,
    audit_enabled=_settings.audit_enabled,
    lifespan=lifespan,
    log=log,
    routers=[api_router],
    ready_check=signing_ready_check(commit_ids_ready_check(_catalog_ready)),
    probes_port=_settings.probes_port,
    # A body over the cap is refused 413 before it is buffered (Arrow-IPC OOM guard, `lakehouse.service_app.body_limit`).
    max_body_bytes=_settings.max_body_bytes,
    request_deadline_seconds=_settings.request_deadline_seconds,
    inner_middleware=[maintenance_middleware, signing_gate_middleware],
    outer_middleware=[Middleware(WriteConcurrencyLimitMiddleware, max_concurrent=_settings.max_concurrent_writes), Middleware(PathReadingMiddleware)],
)
# Every door that authenticates its caller does so from the request's headers before its body is read
# (`lakehouse.governed.admission`): a caller with no token is refused before a byte is buffered, and never takes a write seat.
judge_before_the_body(app, gate=authenticate, judge=judge)
