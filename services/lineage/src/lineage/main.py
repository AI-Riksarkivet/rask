"""Lineage service — FastAPI app: ingest OpenLineage events, query the graph.

A sibling microservice to the catalog (it owns the AGE graph; nobody else touches
it). Run: ``uvicorn lineage.main:app``. See ``docs/LINEAGE.md``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles

from lineage.api.dapr import register_dapr
from lineage.api.v1.endpoints import demo
from lineage.api.v1.router import api_router
from lineage.core.age import make_pool, run_cypher
from lineage.core.config import apply_lineage_secrets, get_settings
from lineage.services.repository import LineageRepository
from service_kit.draining import arm_drain_on_sigterm
from service_kit.governed.audit import configure_audit
from service_kit.governed.auth_lifespan import attach_auth
from service_kit.governed.dapr_auth import assert_app_token_configured
from service_kit.lakehouse.lance_metrics import instrument_lance_if_available
from service_kit.lance_app import build_lance_service_app
from service_kit.obs import configure_app_logging
from service_kit.schemas.health import Readiness, ReadinessStatus


log = logging.getLogger(__name__)
configure_app_logging()  # INFO audit/lifecycle logs reach OTLP (obs audit 2026-07-13)


def _no_disarm() -> None:
    """The drain-disarm before the SIGTERM handler is armed — a bootstrap failure still reaches the
    lifespan `finally` that calls it (see the note at the pre-binding site)."""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    # Readiness flags (mirrors the catalog): /readyz returns 503 until startup_complete and again once
    # shutting_down, so k8s pulls the pod from rotation during boot and graceful drain.
    app.state.startup_complete = False
    app.state.shutting_down = False
    configure_audit(enabled=settings.audit_enabled)  # #41 gate the compliance audit stream (dlq_replay)
    instrument_lance_if_available()  # Lance-native IO metrics onto the global MeterProvider
    # Fail closed if ANY sidecar-delivered route mounts — the pub/sub ingest (dapr_enabled) OR the cron
    # reconcile binding — but the app-api-token is unset: either route would otherwise be an
    # unauthenticated forgery/graph-mutation path (the audit's 'blanked token' residual + its
    # reconcile-route follow-up: the two flags can diverge, so the assert must cover both mounts).
    # No-op in dev (both off).
    assert_app_token_configured(dapr_enabled=settings.dapr_enabled or bool(settings.reconcile_binding_name))
    # Consume the S3 secret + AGE DB password from the Dapr secret store (OpenBao) before opening the pool,
    # so neither lives in plaintext pod env — the audit's secret-consumption fix, symmetric with the
    # catalog. No-op (and no Dapr dependency) when secrets_from_dapr is off; fails closed on the S3 secret.
    # In a thread: the fetch is sync (blocking httpx + sleep between retries, ~80s worst case) — event-loop
    # hygiene; nothing is served until the lifespan completes, but the loop must stay free regardless.
    await run_in_threadpool(apply_lineage_secrets, settings)
    pool = make_pool(settings.database_url, statement_timeout_seconds=settings.age_statement_timeout_seconds)

    # The `finally` owns `pool.close()`, so the pool must be opened INSIDE the guarded scope: a bootstrap
    # await that raises (ensure_events_table on a wedged Postgres, attach_auth on an unreachable store)
    # would otherwise unwind past an open pool that nothing closes — one leaked pool per crash-looping
    # boot. `_disarm_drain` is pre-bound to a no-op because a failure before it is armed still hits the
    # `finally` that calls it.
    _disarm_drain: Callable[[], None] = _no_disarm
    try:
        await pool.open()
        app.state.pool = pool
        repository = LineageRepository(
            pool,
            settings.graph,
            events_retention=settings.events_retention,
            # The same value the pool sets session-wide — the repository re-asserts it transaction-scoped
            # (SET LOCAL) around the first-boot DDL so a wedged Postgres fails boot fast. (P6)
            statement_timeout_seconds=settings.age_statement_timeout_seconds,
        )
        app.state.repository = repository
        # Durable events feed: a Postgres table created on first boot. /runs folds onto the AGE (:Run)
        # node; both now survive restart + are replica-shared — no in-memory state. (#22)
        await repository.ensure_events_table()
        await repository.ensure_reads_table()  # the read-audit log (#6); off unless LINEAGE_READ_AUDIT_ENABLED
        # Create the AGE graph if absent — self-healing + the ONLY graph bootstrap on the external managed-PG
        # path (the in-cluster age-postgres init has none). Fatal on a real failure (the graph is our storage),
        # so it runs BEFORE ensure_graph_constraints, which needs the graph to exist. (prod-readiness P2)
        await repository.ensure_graph()
        # UNIQUE index per AGE vertex label so a concurrent MERGE (reconcile racing ingest) can't create a
        # duplicate vertex (item 6). Best-effort: a per-label failure is logged, not fatal, so ingest still boots.
        await repository.ensure_graph_constraints()
        # Auth is opt-in; when enabled, converge on the CATALOG's store — provision is idempotent by store
        # NAME ("lance-catalog"), so both services resolve the same store + model without the id being
        # pinned ahead of boot. (The catalog writes the grants on create; lineage reads them — one shared
        # Zanzibar store.)
        #
        # `fatal=True` KEEPS THIS SERVICE'S POSTURE: neither construction was wrapped in a `try`, so a
        # failed build has always crashed the pod. Lineage answers "who did what to which dataset"; a
        # replica that came up unable to authorize would serve 503s that read as a slow graph, not as a
        # broken authorization plane.
        await attach_auth(app, settings, service="lineage", fatal=True)
        # Durable ingest (#25) is the Dapr subscription wired below (declarative — the sidecar drives it);
        # there is no consumer task to manage here. The HTTP /api/v1/lineage path stays for external producers.
        #
        # The RELAY's publisher, and only when the outbox is on. The drain re-publishes a recovered event so a
        # subscriber that never saw it still acts on it — without this the relay repairs the GRAPH while the
        # cascade it was meant to restart stays halted. Built here rather than per drain (one channel per
        # process, the same rule as the AGE pool), and skipped entirely when `outbox_uri` is empty so a
        # deployment with the outbox off opens no sidecar channel it will never use.
        app.state.dapr = None
        if settings.outbox_uri:
            from dapr.aio.clients import DaprClient

            app.state.dapr = DaprClient()
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
        # Isolate each close so a failure in one still runs the others (parity with the other services) —
        # e.g. a raising fga_client.close() must not leak the AGE pool.
        fga_client = getattr(app.state, "fga", None)
        if fga_client is not None:
            with suppress(Exception):
                await fga_client.close()
        dapr_client = getattr(app.state, "dapr", None)
        if dapr_client is not None:
            with suppress(Exception):
                await dapr_client.close()
        with suppress(Exception):
            await pool.close()


async def _graph_ready(request: Request) -> Readiness:
    """Gate readiness on the AGE pool AND the graph — lineage's sole hard dependency — so a pod with an
    unhealthy pool is pulled from rotation instead of serving 500s.

    Graph health, not just POOL health: `ensure_graph_constraints` is best-effort non-fatal, so a pod whose
    AGE graph is absent/broken (an external-PG that was never bootstrapped, a failed create_graph, a bad
    restore) would otherwise report Ready, get rotated in, and then silently DISCARD every delivered event —
    provenance loss as a green rollout. A trivial Cypher proves the graph is actually queryable; if it
    isn't, fail the pod loudly. (prod-readiness P1)
    """
    try:
        async with asyncio.timeout(2):
            async with request.app.state.pool.connection() as conn:
                await conn.execute("SELECT 1")
                await run_cypher(conn, get_settings().graph, "RETURN 1")
    except Exception:
        log.warning("readyz_db_unavailable")
        return Readiness(status=ReadinessStatus.degraded, components={"database": "unavailable"})
    return Readiness(status=ReadinessStatus.ready, components={"database": "healthy"})


# THE SHARED LANCE-PLANE ASSEMBLY (open_python-audit DUP-12). Logging before the app exists, the docs
# gate, the handler pair in the order that makes it work, one request id, and the probes — see
# `service_kit.lance_app` for what each of those five is for and what a copy of it got wrong.
#
# `_graph_ready` is the readiness this service adds: lineage's sole hard dependency is the AGE pool
# and the graph itself, so a pod whose graph is absent must be pulled from rotation rather than
# rotated in to silently DISCARD every delivered event.
app = build_lance_service_app(
    title="Lance Lineage Service",
    docs_enabled=get_settings().docs_enabled,
    lifespan=lifespan,
    log=log,
    routers=[api_router],
    ready_check=_graph_ready,
)

# Dapr pub/sub subscription (#25): wired after the app exists, because `DaprApp(app)` needs it.
# DaprApp also serves GET /dapr/subscribe (the sidecar's startup registration).
register_dapr(app)


# Demo data peek (reads the real Lance datasets on S3) — mounted only when explicitly enabled.
if get_settings().demo_data_enabled:
    app.include_router(demo.router)


# Periodic storage->graph reconciliation cron route (B4) — mounted only when a Dapr cron binding names it.
def mount_reconcile_cron(target: FastAPI, binding_name: str | None) -> bool:
    """Mount the reconcile cron route iff a Dapr cron binding names it; return whether it mounted.

    A named function (not inline module-level wiring) so the unit tier can drive the PRODUCTION
    mount decision both ways — the audit's gap was the route being tested only on a synthetic app,
    leaving this gate itself unpinned.
    """
    if not binding_name:
        return False
    from lineage.api.reconcile_cron import build_reconcile_cron_router

    target.include_router(build_reconcile_cron_router(binding_name))
    return True


mount_reconcile_cron(app, get_settings().reconcile_binding_name)

# Thin demo UI — a single self-contained page that polls the query endpoints to render the live
# medallion DAG (see scripts/medallion_demo.py). Mounted last so it never shadows an API route.
_STATIC = Path(__file__).resolve().parent / "static"
if _STATIC.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_STATIC), html=True), name="ui")
