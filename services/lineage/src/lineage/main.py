"""Lineage service — FastAPI app: the OpenLineage intake, the commit feed and the edge list (ADR 0158).

Run: ``uvicorn lineage.main:app``. See ``docs/LINEAGE.md``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, suppress
from typing import Final

import httpx
from fastapi import FastAPI, Request

from lineage.api.dapr import register_dapr
from lineage.api.v1.router import api_router
from lineage.core.config import get_settings
from lineage.services.feed_watch import FeedWatch
from lineage.services.names import CatalogNames
from lineage.services.provenance import ProvenanceStore
from service_kit.bus_watchdog import start_bus_watchdog
from service_kit.draining import arm_drain_on_sigterm
from service_kit.governed.auth_lifespan import attach_auth
from service_kit.governed.dapr_auth import assert_app_token_configured
from service_kit.governed.fga import dispose as fga_dispose
from service_kit.governed.settings import assert_authentication_configured
from service_kit.governed.store_credential import make_store_credential
from service_kit.lance_app import build_lance_service_app
from service_kit.obs import configure_app_logging
from service_kit.schemas.health import Readiness, ReadinessStatus


log = logging.getLogger(__name__)
configure_app_logging()  # INFO audit/lifecycle logs reach OTLP (obs audit 2026-07-13)

#: How long boot waits for the catalog to create this service's tables. The catalog keeps the provenance
#: namespace itself and may still be starting when this pod is; a pod that gives up sooner crash-loops on a
#: rollout that rolls both, and one that waits for ever never reports what it is waiting for.
_ENSURE_DEADLINE_SECONDS: Final = 120.0
_ENSURE_RETRY_SECONDS: Final = 2.0
#: How often readiness's feed watch reads the feed through the catalog, and how long one read may take (LIN-8).
FEED_WATCH_INTERVAL_SECONDS: Final = 5.0
FEED_WATCH_BUDGET_SECONDS: Final = 5.0


def _no_disarm() -> None:
    """The drain-disarm before the SIGTERM handler is armed — a bootstrap failure still reaches the
    lifespan `finally` that calls it (see the note at the pre-binding site)."""


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
    # The outbox's S3 key pair from the Dapr secret store (OpenBao), never plaintext pod env: read now, failing
    # the boot closed if the store never answers, then again while lineage runs, so a pair rotated in the store
    # is the one the relay signs with within its TTL ([[XC-082]], ADR 0166). None when secrets_from_dapr is off.
    store_credential = make_store_credential(settings)
    if store_credential is not None:
        await store_credential.start()
    app.state.store_credential = store_credential
    # Restarts this pod's sidecar once one of its bus connections has closed for good, as daprd's do after a long bus
    # outage (`service_kit.bus_watchdog`); None where the chart names this pod no bus.
    bus_watchdog = await start_bus_watchdog()
    _disarm_drain: Callable[[], None] = _no_disarm
    feed_watch: FeedWatch | None = None
    try:
        store = ProvenanceStore.from_settings(settings)
        await _ensure_tables(store)
        app.state.provenance = store
        app.state.names = CatalogNames.from_settings(settings)
        # Readiness reads this watch's verdict, never the feed itself: the read costs what the catalog takes to open the
        # feed, which a one-second probe cannot wait for (`lineage.services.feed_watch`).
        feed_watch = FeedWatch(store.ready, interval=FEED_WATCH_INTERVAL_SECONDS, budget=FEED_WATCH_BUDGET_SECONDS)
        feed_watch.start()
        app.state.feed_watch = feed_watch
        # Auth is opt-in; when enabled, converge on the CATALOG's store — provision is idempotent by store
        # NAME ("lance-catalog"), so both services resolve the same store + model without the id being
        # pinned ahead of boot. (The catalog writes the grants on create; lineage reads them — one shared
        # Zanzibar store.)
        #
        # `fatal=True` KEEPS THIS SERVICE'S POSTURE: a replica that came up unable to authorize would serve
        # 503s that read as a slow feed, not as a broken authorization plane.
        await attach_auth(app, settings, service="lineage", fatal=True)
        # The RELAY's publisher, and only when the outbox is on. The drain re-publishes a recovered event so a
        # subscriber that never saw it still acts on it. Built here rather than per drain (one channel per
        # process), and skipped entirely when `outbox_uri` is empty so a deployment with the outbox off opens no
        # sidecar channel it will never use.
        app.state.dapr = None
        if settings.outbox_uri:
            from dapr.aio.clients import DaprClient

            app.state.dapr = DaprClient()
        # The forwarder's client, and only when a graph backend is configured: one connection pool per process.
        app.state.forward_client = httpx.AsyncClient(timeout=settings.forward_timeout_seconds) if settings.forward_url else None
        app.state.startup_complete = True
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
        if feed_watch is not None:
            await feed_watch.stop()
        if bus_watchdog is not None:
            await bus_watchdog.stop()
        # Isolate each close so a failure in one still runs the others (parity with the other services).
        await fga_dispose(app)
        dapr_client = getattr(app.state, "dapr", None)
        if dapr_client is not None:
            with suppress(Exception):
                await dapr_client.close()
        forward_client = getattr(app.state, "forward_client", None)
        if forward_client is not None:
            with suppress(Exception):
                await forward_client.aclose()
        if store_credential is not None:
            await store_credential.stop()


async def _feed_ready(request: Request) -> Readiness:
    """Gate readiness on the feed table answering through the catalog — lineage's sole hard dependency.

    A pod whose catalog cannot be reached would otherwise report Ready, get rotated in, and answer every
    delivery with a retry: the bus redelivers, so nothing is lost, but the pod should be out of rotation
    while it cannot record anything. The answer is the feed watch's latest verdict, so the probe never waits on the read.
    """
    watch: FeedWatch | None = getattr(request.app.state, "feed_watch", None)
    if watch is None or not watch.ready:
        return Readiness(status=ReadinessStatus.degraded, components={"catalog": "unavailable"})
    return Readiness(status=ReadinessStatus.ready, components={"catalog": "healthy"})


# THE SHARED LANCE-PLANE ASSEMBLY (docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit" DUP-12). Logging before the app exists, the docs
# gate, the handler pair in the order that makes it work, one request id, and the probes — see
# `service_kit.lance_app` for what each of those five is for and what a copy of it got wrong.
app = build_lance_service_app(
    title="Lance Lineage Service",
    docs_enabled=get_settings().docs_enabled,
    audit_enabled=get_settings().audit_enabled,
    lifespan=lifespan,
    log=log,
    routers=[api_router],
    ready_check=_feed_ready,
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
