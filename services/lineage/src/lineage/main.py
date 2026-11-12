"""Lineage service — FastAPI app: the OpenLineage intake, the commit feed and the edge list (ADR 0158).

Run: ``uvicorn lineage.main:app``. See ``docs/LINEAGE.md``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from typing import Final

import httpx
from fastapi import FastAPI, Request

from lakehouse.admission import judge_before_the_body
from lakehouse.bus_watchdog import BusWatchdog, start_bus_watchdog
from lakehouse.draining import arm_drain_on_sigterm
from lakehouse.governed.auth_lifespan import attach_auth
from lakehouse.governed.dapr_auth import assert_app_token_configured
from lakehouse.governed.fga import dispose as fga_dispose
from lakehouse.governed.settings import assert_authentication_configured
from lakehouse.governed.signing_key import secret_store_dependency
from lakehouse.health import Dependency, HealthWatch, bus_dependency, openfga_dependency, store_dependency
from lakehouse.lance_app import build_lance_service_app
from lakehouse.lifecycle import closed_quietly
from lakehouse.obs import configure_app_logging
from lakehouse.schemas.health import Readiness
from lakehouse.store_session import require_env_pair, start_store_session
from lakehouse.supervision import Supervisor
from lineage.api.dapr import register_dapr
from lineage.api.security import authenticate, judge
from lineage.api.v1.router import api_router
from lineage.core.config import LineageSettings, get_settings, storage_options
from lineage.services.names import CatalogNames
from lineage.services.provenance import ProvenanceStore


log = logging.getLogger(__name__)
configure_app_logging()  # INFO audit/lifecycle logs reach OTLP (obs audit 2026-07-13)

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
    # Readiness flags (mirrors the catalog): /readyz returns 503 until startup_complete and again once
    # shutting_down, so k8s pulls the pod from rotation during boot and graceful drain.
    app.state.startup_complete = False
    app.state.shutting_down = False
    # Fail closed if ANY sidecar-delivered route mounts — the pub/sub ingest (dapr_enabled) OR the relay
    # cron binding — but the app-api-token is unset: either route would otherwise be an unauthenticated
    # forgery path (the audit's 'blanked token' residual + its cron-route follow-up: the two flags can
    # diverge, so the assert must cover both mounts). No-op in dev (both off).
    assert_app_token_configured(dapr_enabled=settings.dapr_enabled or bool(settings.relay_binding_name))
    # Q17-6 / §F2-2: the CODE default is anonymous while the chart flips auth ON, so a service
    # started any other way serves every route to whoever reaches the port. Refused on the
    # AMBIGUITY — an operator declares which they meant. See `assert_authentication_configured`.
    assert_authentication_configured(
        oidc_enabled=settings.oidc_enabled,
        insecure_allow_unauthenticated=settings.insecure_allow_unauthenticated,
    )
    # EVERYTHING ACQUIRED BELOW IS RELEASED BY THIS STACK, in reverse, whether the start completes or fails part-way
    # (ADR 0203), so a start that fails after the store session or a client is built leaves none of them open.
    async with AsyncExitStack() as stack:
        # Every background loop this process runs is supervised here: one that ends unasked ends the process.
        loops = await stack.enter_async_context(Supervisor())
        await _build(app, settings, stack=stack, loops=loops)
        # Registered after everything it stops, so run before any of it is released.
        stack.push_async_callback(loops.stop)
        app.state.startup_complete = True
        # ARMED AT SIGTERM, not at lifespan shutdown. The flag below flips in the `finally`, which uvicorn reaches only
        # AFTER it has stopped accepting connections and drained, so an admission guard reading that flag alone would
        # refuse nothing. Kubernetes sends SIGTERM at the START of termination, and that window is exactly when the
        # sidecar is still delivering. Owner ruling 2026-08-25.
        stack.callback(arm_drain_on_sigterm(app))
        try:
            yield
        finally:
            app.state.shutting_down = True


async def _build(app: FastAPI, settings: LineageSettings, *, stack: AsyncExitStack, loops: Supervisor) -> None:
    """Build what lineage serves with onto ``app.state``, each piece's release pushed onto ``stack`` as it is built, and
    its background loops started on ``loops``."""
    # LINEAGE HOLDS NO STORE KEY ([[XC-084]], ADR 0170). Its outbox drain signs with a session its ServiceAccount token is
    # exchanged for, here at boot, failing the boot closed if none is issued, then again once half the session's life is
    # used. With no token (the offline and Dagger lanes) an outbox on the store is drained under the environment's pair,
    # refused here when either half is missing: the drain's boto3 would otherwise sign with whatever its default chain finds.
    store_session = await start_store_session(settings.web_identity, session_name="lineage", duration_seconds=settings.s3_session_seconds, loops=loops)
    if store_session is None and settings.outbox_on_the_store:
        require_env_pair(settings, prefix="LINEAGE")
    if store_session is not None:
        stack.push_async_callback(store_session.stop)
    app.state.store_session = store_session
    # Restarts this pod's sidecar once one of its bus connections has closed for good, as daprd's do after a long bus
    # outage (`lakehouse.bus_watchdog`); None where the chart names this pod no bus.
    bus_watchdog = await start_bus_watchdog(loops)
    if bus_watchdog is not None:
        stack.push_async_callback(bus_watchdog.stop)
    store = ProvenanceStore.from_settings(settings)
    await _ensure_tables(store)
    app.state.provenance = store
    app.state.names = CatalogNames.from_settings(settings)
    # Auth is opt-in; when enabled, converge on the CATALOG's store — provision is idempotent by store
    # NAME ("lance-catalog"), so both services resolve the same store + model without the id being
    # pinned ahead of boot. (The catalog writes the grants on create; lineage reads them — one shared
    # Zanzibar store.)
    #
    # `fatal=True` KEEPS THIS SERVICE'S POSTURE: a replica that came up unable to authorize would serve
    # 503s that read as a slow feed, not as a broken authorization plane.
    stack.push_async_callback(fga_dispose, app)
    await attach_auth(app, settings, service="lineage", loops=loops, fatal=True)
    # The RELAY's publisher, and only when the outbox is on. The drain re-publishes a recovered event so a
    # subscriber that never saw it still acts on it. Built here rather than per drain (one channel per
    # process), and skipped entirely when `outbox_uri` is empty so a deployment with the outbox off opens no
    # sidecar channel it will never use.
    app.state.dapr = None
    if settings.outbox_uri:
        from dapr.aio.clients import DaprClient

        app.state.dapr = DaprClient()
        stack.push_async_callback(closed_quietly, app.state.dapr.close)
    # The forwarder's client, and only when a graph backend is configured: one connection pool per process.
    app.state.forward_client = httpx.AsyncClient(timeout=settings.forward_timeout_seconds) if settings.forward_url else None
    if app.state.forward_client is not None:
        stack.push_async_callback(closed_quietly, app.state.forward_client.aclose)
    # Readiness reads these verdicts, never a dependency itself (`lakehouse.health`): the feed's read costs what the
    # catalog takes to open the feed, which a one-second probe cannot wait for.
    app.state.health = HealthWatch(_dependencies(app, settings, store, bus_watchdog))
    app.state.health.watch(loops)


def _dependencies(app: FastAPI, settings: LineageSettings, store: ProvenanceStore, bus_watchdog: BusWatchdog | None) -> list[Dependency]:
    """What lineage reads to know it can serve. The feed table answering through the catalog gates (owner, 2026-11-12): a
    pod whose catalog cannot be reached would otherwise report Ready, get rotated in, and answer every delivery with a
    retry. The rest is reported and never gates: OpenFGA where lineage asks it directly, whose outage its doors answer
    503 and its deliveries retry; the bus, which delivers again what lineage could not take; the object store where the
    outbox the relay drains is kept there, whose drain waits for it; and the secret store its signers' published keys are
    read from, since the keys it read carry it through."""
    dependencies = [
        Dependency(name="catalog", read=store.ready, required=True),
        *openfga_dependency(app, required=False),
        *bus_dependency(bus_watchdog),
    ]
    if settings.outbox_on_the_store:
        dependencies.append(store_dependency(settings.outbox_uri, partial(storage_options, settings), required=False))
    if settings.secrets_from_dapr and settings.signing.signers:
        dependencies.append(secret_store_dependency(settings.dapr_secret_store, min(settings.signing.signers)))
    return dependencies


async def _lineage_ready(request: Request) -> Readiness:
    """The verdicts the health watch keeps; never a read of its own."""
    return request.app.state.health.readiness()


# THE SHARED LANCE-PLANE ASSEMBLY (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit" DUP-12). Logging before the app exists, the docs
# gate, the handler pair in the order that makes it work, one request id, and the probes — see
# `lakehouse.lance_app` for what each of those five is for and what a copy of it got wrong.
app = build_lance_service_app(
    title="Lance Lineage Service",
    docs_enabled=get_settings().docs_enabled,
    audit_enabled=get_settings().audit_enabled,
    lifespan=lifespan,
    log=log,
    routers=[api_router],
    ready_check=_lineage_ready,
    max_body_bytes=get_settings().max_body_bytes,
    request_deadline_seconds=get_settings().request_deadline_seconds,
)

# Dapr pub/sub subscription (#25): wired after the app exists, because `DaprApp(app)` needs it.
# DaprApp also serves GET /dapr/subscribe (the sidecar's startup registration).
register_dapr(app)


def mount_relay_cron(target: FastAPI, binding_name: str | None) -> bool:
    """Mount the relay tick's route iff a Dapr cron binding names it; return whether it mounted.

    A named function (not inline module-level wiring) so the unit tier can drive the PRODUCTION
    mount decision both ways.
    """
    if not binding_name:
        return False
    from lineage.api.relay_cron import build_relay_cron_router

    target.include_router(build_relay_cron_router(binding_name))
    return True


mount_relay_cron(app, get_settings().relay_binding_name)
# Every door that authenticates its caller does so from the request's headers before its body is read
# (`lakehouse.admission`): the intake's JSON body is never read, let alone parsed, for a caller with no token.
judge_before_the_body(app, gate=authenticate, judge=judge)
