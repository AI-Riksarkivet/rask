"""Lineage service — FastAPI app: the OpenLineage intake, the commit feed and the edge list (ADR 0158).

Run: ``uvicorn lineage.main:app``. See ``docs/LINEAGE.md``.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from typing import Final

import httpx
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool

from lakehouse.events.bus import Bus, bus_dependency, bus_session
from lakehouse.governed.admission import judge_before_the_body
from lakehouse.governed.auth_lifespan import BootFailure, attach_auth, openfga_dependency
from lakehouse.governed.fga_provision import dispose as fga_dispose
from lakehouse.governed.secrets import SecretStore, secret_store_configured, secret_store_session
from lakehouse.governed.settings import assert_authentication_configured
from lakehouse.governed.signing_key import secret_store_dependency
from lakehouse.process.health import Dependency, HealthWatch
from lakehouse.process.lifecycle import closed_quietly, mark_draining, mark_started, mark_starting
from lakehouse.process.own_threads import ThreadSet
from lakehouse.process.supervision import Supervisor
from lakehouse.schemas.health import Readiness
from lakehouse.service_app.factory import build_service_app
from lakehouse.storage.objectfs import store_dependency
from lakehouse.storage.store_session import require_env_pair, start_store_session
from lineage.api.ingest_lane import IngestLane, lane
from lineage.api.relay import start_relay
from lineage.api.security import authenticate, judge
from lineage.api.v1.router import api_router
from lineage.core.config import LineageSettings, get_settings, storage_options
from lineage.core.metrics import create_outcome_series, create_signature_refused_series
from lineage.services.catalog_client import CatalogClients
from lineage.services.names import CatalogNames
from lineage.services.provenance import ProvenanceStore
from lineage.services.signature import store_published_keys


log = logging.getLogger(__name__)

#: How long boot waits for the catalog to create this service's tables. The catalog keeps the provenance
#: namespace itself and may still be starting when this pod is; a pod that gives up sooner crash-loops on a
#: rollout that rolls both, and one that waits for ever never reports what it is waiting for.
_ENSURE_DEADLINE_SECONDS: Final = 120.0
_ENSURE_RETRY_SECONDS: Final = 2.0


async def _ensure_tables(store: ProvenanceStore) -> None:
    """Create this service's tables through the catalog, retrying until the catalog answers or the deadline passes."""
    deadline = asyncio.get_running_loop().time() + _ENSURE_DEADLINE_SECONDS
    while True:
        try:
            await store.ensure()
            return
        except Exception as exc:
            if asyncio.get_running_loop().time() > deadline:
                raise
            log.warning("lineage_provenance_tables_not_ready", extra={"error": str(exc)})
            await asyncio.sleep(_ENSURE_RETRY_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    # /readyz answers 503 until the start completes and again once the drain begins, so the pod is out of rotation then.
    mark_starting(app)
    # Q17-6 / §F2-2: the CODE default is anonymous while the chart flips auth ON, so a service
    # started any other way serves every route to whoever reaches the port. Refused on the
    # AMBIGUITY — an operator declares which they meant. See `assert_authentication_configured`.
    assert_authentication_configured(
        oidc_enabled=settings.oidc_enabled,
        insecure_allow_unauthenticated=settings.insecure_allow_unauthenticated,
    )
    settings.signing.require_signers_where_they_can_be_verified(authenticated=settings.oidc_enabled, secret_store=secret_store_configured())
    # EVERYTHING ACQUIRED BELOW IS RELEASED BY THIS STACK, in reverse, whether the start completes or fails part-way
    # (ADR 0203), so a start that fails after the store session or a client is built leaves none of them open.
    async with AsyncExitStack() as stack:
        # Every background loop this process runs is supervised here: one that ends unasked ends the process.
        loops = await stack.enter_async_context(Supervisor())
        await _build(app, settings, stack=stack, loops=loops)
        # Registered after everything it stops, so run before any of it is released.
        stack.push_async_callback(loops.stop)
        mark_started(app)
        try:
            yield
        finally:
            mark_draining(app)


async def _build(app: FastAPI, settings: LineageSettings, *, stack: AsyncExitStack, loops: Supervisor) -> None:
    """Build what lineage serves with onto ``app.state``, each piece's release pushed onto ``stack`` as it is built, and
    its background loops started on ``loops``."""
    # LINEAGE LOGS IN TO ITS SECRET STORE ITSELF ([[XC-199]]), before it serves, and refuses to start when the store
    # never admits it: with no key of its own to read it would otherwise ask the store nothing until the first event it
    # has to verify. A loop of `loops` then logs in again once half the store token's lease is used, or as soon as the
    # store no longer honours it. Nothing for a stack with no secret store.
    secrets = await stack.enter_async_context(secret_store_session(loops))
    # The session the forwarder reads the graph backend's key under, or None for a stack with no secret store.
    app.state.secret_store = secrets
    # LINEAGE HOLDS NO STORE KEY ([[XC-084]], ADR 0170). Its outbox drain signs with a session its ServiceAccount token is
    # exchanged for, here at boot, failing the boot closed if none is issued, then again once half the session's life is
    # used; its relay's timer claims each window with the same session. With no token (the offline and Dagger lanes) an
    # outbox or a timer on the store is reached under the environment's pair, refused here when either half is missing:
    # boto3 would otherwise sign with whatever its default chain finds.
    store_session = await start_store_session(settings.web_identity, session_name="lineage", duration_seconds=settings.s3_session_seconds, loops=loops)
    if store_session is None and settings.signs_store_requests:
        require_env_pair(settings, prefix="LINEAGE")
    if store_session is not None:
        stack.push_async_callback(store_session.stop)
    app.state.store_session = store_session
    # What every call through the catalog shares: its two connection pools, the threads the calls run on and the feed's
    # writer thread (`CatalogClients`). Closed in a worker thread, since it waits for a burst still landing.
    clients = CatalogClients(settings.catalog_url)
    stack.push_async_callback(run_in_threadpool, clients.close)
    store = ProvenanceStore.from_settings(settings, clients)
    await _ensure_tables(store)
    app.state.provenance = store
    app.state.names = CatalogNames.from_settings(settings, clients)
    # Auth is opt-in; when enabled, converge on the CATALOG's store — provision is idempotent by store
    # NAME ("lance-catalog"), so both services resolve the same store + model without the id being
    # pinned ahead of boot. (The catalog writes the grants on create; lineage reads them — one shared
    # Zanzibar store.)
    #
    # `BootFailure.CRASH` KEEPS THIS SERVICE'S POSTURE: a replica that came up unable to authorize would serve
    # 503s that read as a slow feed, not as a broken authorization plane.
    stack.push_async_callback(fga_dispose, app)
    await attach_auth(app, settings, service="lineage", loops=loops, on_failure=BootFailure.CRASH)
    # THIS PROCESS'S OWN CONNECTION TO THE BUS ([[XC-199]]), as lineage's bus user (`lakehouse.events.bus`): lineage takes its
    # deliveries on it, and the relay re-publishes a recovered event on it, so a consumer that never saw it still acts on
    # it. A loop of `loops` connects it behind the start and keeps it, so lineage serves while the bus is away, and the
    # bus keeps what it could not deliver. None for a stack the chart names no bus.
    bus = await stack.enter_async_context(bus_session(loops))
    app.state.bus = bus
    # The signers' published keys, read from the secret store under this process's login when an event is verified
    # (`lineage.services.signature`). Built per start, before the first delivery or request can ask for it.
    app.state.published_keys = store_published_keys(secrets)
    _take_deliveries(app, settings, bus, loops=loops)
    # The forwarder's client, and only when a graph backend is configured: one connection pool per process.
    app.state.forward_client = httpx.AsyncClient(timeout=settings.forward_timeout_seconds) if settings.forward_url else None
    if app.state.forward_client is not None:
        stack.push_async_callback(closed_quietly, app.state.forward_client.aclose)
    # Readiness reads these verdicts, never a dependency itself (`lakehouse.process.health`): the feed's read costs what the
    # catalog takes to open the feed, which a one-second probe cannot wait for.
    app.state.health = HealthWatch(_dependencies(app, settings, store, secrets=secrets, threads=loops.threads))
    app.state.health.watch(loops)
    # THE RELAY'S TIMER ([[XC-199]]), started last, once everything a tick works with is on `app.state`.
    start_relay(app, settings, loops)


def _take_deliveries(app: FastAPI, settings: LineageSettings, bus: Bus | None, *, loops: Supervisor) -> None:
    """Pull lineage's consumer on a loop of ``loops`` for as long as the process runs, where a bus is named. Started once
    the feed and the gate a delivery is handled with are on ``app.state``, and stopped with the other loops, before
    either is released."""
    # Every reason series of the signature refusal counter at 0, which the doors that verify a bus or staged event share,
    # and every outcome's series of the processed-events counter, which every door counts on.
    create_signature_refused_series()
    create_outcome_series()
    if bus is None:
        return
    ingest = IngestLane(app, settings)
    loops.keep("lineage-ingest", partial(bus.consume, lane(settings), ingest.handle, park=ingest.park))


def _dependencies(app: FastAPI, settings: LineageSettings, store: ProvenanceStore, *, secrets: SecretStore | None, threads: ThreadSet) -> list[Dependency]:
    """What lineage reads to know it can serve. The feed table answering through the catalog gates (owner, 2026-11-12): a
    pod whose catalog cannot be reached would otherwise report Ready, get rotated in, and answer every delivery with a
    retry. The rest is reported and never gates: OpenFGA where lineage asks it directly, whose outage its doors answer
    503 and its deliveries retry; the bus, which delivers again what lineage could not take; the object store where the
    outbox the relay drains is kept there, whose drain waits for it; and the secret store its signers' published keys are
    read from, since the keys it read carry it through. The blocking reads run on ``threads``, the process's own."""
    dependencies = [
        Dependency(name="catalog", read=partial(store.ready, threads), required=True),
        *openfga_dependency(app, required=False),
        *bus_dependency(app.state.bus),
    ]
    if settings.outbox_on_the_store:
        dependencies.append(store_dependency(settings.outbox_uri, partial(storage_options, settings), required=False, threads=threads))
    if secrets is not None:
        dependencies.append(secret_store_dependency(secrets, threads))
    return dependencies


async def _lineage_ready(request: Request) -> Readiness:
    """The verdicts the health watch keeps; never a read of its own."""
    return request.app.state.health.readiness()


# THE SHARED LANCE-PLANE ASSEMBLY (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit" DUP-12). Logging before the app exists, the docs
# gate, the handler pair in the order that makes it work, one trace per request, and the probes — see
# `lakehouse.service_app.factory` for what each of those five is for and what a copy of it got wrong.
app = build_service_app(
    title="Lance Lineage Service",
    docs_enabled=get_settings().docs_enabled,
    audit_enabled=get_settings().audit_enabled,
    lifespan=lifespan,
    log=log,
    routers=[api_router],
    ready_check=_lineage_ready,
    probes_port=get_settings().probes_port,
    max_body_bytes=get_settings().max_body_bytes,
    request_deadline_seconds=get_settings().request_deadline_seconds,
)


# Every door that authenticates its caller does so from the request's headers before its body is read
# (`lakehouse.governed.admission`): the intake's JSON body is never read, let alone parsed, for a caller with no token.
judge_before_the_body(app, gate=authenticate, judge=judge)
