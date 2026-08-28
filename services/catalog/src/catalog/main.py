"""Lance Namespace REST Catalog — FastAPI application entry.

A Python/FastAPI adapter exposing the full Lance Namespace REST API (spec.yaml)
over a native ``lance.namespace`` backend (``DirectoryNamespace`` on MinIO/S3 by
default), with the pylance data plane filling operations the backend stubs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx
from dapr.aio.clients import DaprClient
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import LanceNamespaceError
from pydantic import SecretStr

from catalog.api.dapr import register_control_dapr
from catalog.api.load_shed import WriteConcurrencyLimitMiddleware
from catalog.api.maintenance_mode import maintenance_middleware
from catalog.api.v1.router import api_router
from catalog.core.config import get_settings
from catalog.core.control_buffer import ControlEventBuffer
from catalog.core.lineage_emit import make_emitter
from catalog.core.namespace import build_namespace
from catalog.core.vending import make_vendor
from catalog.services import warehouses
from service_kit import setup_logging
from service_kit.body_limit import BodySizeLimitMiddleware
from service_kit.control_emit import make_control_emitter
from service_kit.exceptions import register_handlers
from service_kit.governed.audit import configure_audit
from service_kit.governed.auth_lifespan import attach_auth
from service_kit.governed.dapr_auth import assert_app_token_configured
from service_kit.governed.secrets import fetch_required_secrets
from service_kit.governed.user_state import UserStateStore
from service_kit.lakehouse.lance_metrics import instrument_lance_if_available
from service_kit.lakehouse.ns_errors import install_problem_handlers
from service_kit.middleware import RequestIDMiddleware
from service_kit.obs import configure_app_logging
from service_kit.probes import make_probes_router
from service_kit.schemas.health import Readiness, ReadinessStatus


log = logging.getLogger(__name__)
configure_app_logging()  # INFO audit/lifecycle logs reach OTLP (obs audit 2026-07-13)

PROBLEM_JSON = "application/problem+json"


async def _backfill_cascade_grants(app: FastAPI) -> None:
    """Run the cascade-grant backfill, reporting rather than raising. See its call site for why."""
    from catalog.services.cascade_backfill import backfill

    try:
        seen, written, failures = await backfill(app.state.settings)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("cascade_backfill_error", extra={"error": str(exc)})
        return
    if seen:
        log.info("cascade_backfill_done", extra={"warehouses": seen, "tuples": written, "failures": len(failures)})


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    app.state.shutting_down = False
    app.state.startup_complete = False
    configure_audit(enabled=settings.audit_enabled)  # #41 gate the compliance audit stream
    # Fail closed if control eventing is on but the ingest route (api/dapr.py /control-events) would be
    # unauthenticated: require_dapr_token silently no-ops on a blank APP_API_TOKEN, so an unset token with
    # the subscription live is a misconfiguration a forged in-cluster POST could exploit — refuse to boot.
    assert_app_token_configured(dapr_enabled=settings.control_emit_enabled)
    instrument_lance_if_available()  # Lance-native IO metrics onto the global MeterProvider
    # Consume the sensitive S3 secret from the Dapr secret store (OpenBao) — the store is the SOLE source
    # of truth, NOT a fallback (the audit's 'wired but never read' / 'plaintext still ships' fix). With
    # secrets_from_dapr on, the chart does not put the secret in pod env, so reading the env would yield
    # nothing — we fetch from the store (retrying while it seeds) and FAIL CLOSED if it never arrives,
    # rather than booting with an empty/plaintext key.
    if settings.secrets_from_dapr:
        # Strict sole source: a store miss FAILS CLOSED (the shared helper raises) — never fall back to a
        # plaintext env value. fetch_required_secrets retries while the store/sidecar/seed come up; it is
        # sync (blocking httpx + sleep between retries, ~80s worst case), so it runs in a thread — event-loop
        # hygiene: nothing served during the lifespan anyway, but the loop must stay free for other startup
        # tasks and must never normalize blocking calls in async context.
        bundle = await run_in_threadpool(
            fetch_required_secrets,
            settings.dapr_secret_store,
            settings.dapr_secret_key,
            require=settings.dapr_secret_s3_field,
        )
        settings.s3_secret_access_key = SecretStr(bundle[settings.dapr_secret_s3_field])
        log.info("secret_from_dapr_store", extra={"field": settings.dapr_secret_s3_field})
    elif not settings.s3_secret_access_key.get_secret_value():
        raise RuntimeError("LANCE_S3_SECRET_ACCESS_KEY is required when secrets_from_dapr is off")
    app.state.namespace = build_namespace(settings)  # fail fast if storage misconfigured
    # #3-A warehouse routing caches (only used when warehouses_enabled): top-level-namespace → its physical
    # root_uri (bindings are immutable, so cache-forever is safe) and root_uri → its namespace connection.
    app.state.warehouse_binding_cache = {}
    app.state.warehouse_namespaces = {}
    # `fatal=True` KEEPS THIS SERVICE'S POSTURE: neither construction was wrapped in a `try`, so a
    # failed build has always crashed the pod. The catalog is the estate's authorization SOURCE — it
    # writes the grants every other service reads — so a boot that cannot reach OpenFGA must be
    # visible as a CrashLoopBackOff, not as a fleet of ready pods answering 503 to everyone.
    await attach_auth(app, settings, service="catalog", fatal=True)
    # Credential vending (data plane): turn an authorized (table location, tier) into the scoped
    # storage_options a client uses to reach object storage DIRECTLY. mode_b (default) vends nothing —
    # clients use the server-mediated Arrow-IPC endpoints; sts (AssumeRole + per-table session policy) /
    # static delegate short-TTL or per-bucket creds. Built once from config (see core/vending.py).
    app.state.vendor = make_vendor(
        settings.vending_mode,
        region=settings.s3_region,
        sts_endpoint=settings.s3_sts_endpoint,
        assume_role_arn=settings.s3_assume_role_arn,
        ttl_seconds=settings.vending_ttl_seconds,
    )
    # Lineage emission (opt-in, best-effort). Build the chosen transport: a Dapr pub/sub publisher (the
    # sidecar persists to NATS) or a direct-HTTP client. The Dapr client targets the local sidecar, so
    # it's cheap to construct and needs no broker reachability at boot.
    lineage_http = None
    dapr_client = None
    if settings.lineage_emit_enabled and settings.lineage_transport == "dapr":
        dapr_client = DaprClient()
    elif settings.lineage_emit_enabled and settings.lineage_url:
        lineage_http = httpx.AsyncClient(timeout=settings.lineage_emit_timeout_seconds)

    # WHICH TENANT a write belongs to, for the emitted `lance.project` facet — WATCH targeting's only
    # key. Without it `notifications` skips the watcher loop entirely and every catalog write in the
    # estate reaches its author and nobody else.
    #
    # Resolved through the warehouse registry (binding → warehouse → project) because no string rule
    # can do it: `PROJECT_PATTERN` allows `-` inside a project id, so `acme-bronze` is ambiguous.
    # Reads run in the threadpool (pyarrow's filesystem is blocking) and share the SAME positives-only
    # binding cache the routing resolver uses, so a hot namespace costs one warehouse read, not two
    # per write. Best-effort by construction — `project_for` swallows and degrades to `None`.
    async def _resolve_project(top_ns: str) -> str | None:
        cache: dict[str, dict[str, str]] = app.state.warehouse_binding_cache
        cached = cache.get(top_ns, {}).get("project")
        if cached:
            return cached
        project = await run_in_threadpool(warehouses.project_for_namespace, settings.registry_root, settings.storage_options(), top_ns)
        if project:
            cache.setdefault(top_ns, {})["project"] = project
        return project

    app.state.lineage_emitter = make_emitter(
        enabled=settings.lineage_emit_enabled,
        transport=settings.lineage_transport,
        url=settings.lineage_url,
        client=lineage_http,
        dapr=dapr_client,
        pubsub=settings.dapr_pubsub,
        topic=settings.dapr_topic,
        job_namespace=settings.lineage_job_namespace,
        timeout_seconds=settings.lineage_emit_timeout_seconds,
        project_resolver=_resolve_project,
        outbox_uri=settings.lineage_outbox_uri,
        storage_options=settings.storage_options(),
    )
    # Control-plane change-events (opt-in, best-effort — the governance/metadata stream). Publishes through
    # the same local sidecar (reuse/lazily build the Dapr client). The per-replica ring buffer is ALWAYS
    # built (plain memory, fed by the broadcast subscription in api/dapr.py); the emitter is a no-op when off.
    if settings.control_emit_enabled and dapr_client is None:
        dapr_client = DaprClient()
    app.state.control_buffer = ControlEventBuffer(settings.control_buffer_size)
    app.state.control_emitter = make_control_emitter(
        enabled=settings.control_emit_enabled,
        dapr=dapr_client,
        pubsub=settings.control_pubsub,
        timeout_seconds=settings.control_emit_timeout_seconds,
        service="catalog",
        # Staged when configured, plain publish when not — opt-in, exactly like the lineage outbox.
        outbox_uri=settings.control_outbox_uri,
        storage_options=settings.storage_options(),
    )
    # Per-subject user state on the Dapr state store (endpoints/user_state.py). Built unconditionally: it
    # is a client over the local sidecar, so construction is pure and does no I/O — a deployment without a
    # reachable state store fails at the first request with a fail-closed 503, never at boot, and never by
    # looking like an empty document set.
    app.state.user_state = UserStateStore.build(
        store_name=settings.user_state_store,
        dapr_http_port=settings.dapr_http_port,
        timeout_seconds=settings.user_state_timeout_seconds,
    )
    app.state.startup_complete = True
    # Re-assert the cascade's grants over warehouses that already exist. BACKGROUND and non-fatal, and
    # both of those are the design rather than caution:
    #
    #   * The grants are written ONCE, at warehouse-create, from `LANCE_FGA_CASCADE_WRITERS` — a value
    #     that CHANGES. Adding a mover extends the list and every older warehouse is missing the new
    #     subject, so that mover cannot promote into any existing tenant. It fails 403 at promotion,
    #     in a log nobody watches. Measured on the k3s estate: `user:service-silver-to-gold` held
    #     `owner` on NEITHER warehouse:acme-bucket NOR warehouse:research-bucket.
    #   * A hook Job was the first choice and cannot work here. With `secrets_from_dapr` the registry
    #     read needs the S3 secret from the secret store, so the Job needs a Dapr sidecar — and a
    #     sidecar keeps a Job from ever completing. Putting the secret in the Job's env instead is the
    #     one thing the secret rule forbids. This process already holds both the secret and an FGA
    #     client, so the work belongs here.
    #   * NOT awaited, so a slow or unreachable OpenFGA cannot delay readiness, and NOT fatal, because
    #     a grant that could not be re-asserted must not take the catalog down — it leaves the estate
    #     exactly as it was, which is the state this repairs, not a worse one.
    #   * Per-replica repetition is harmless: every write is idempotent (`write_tuples` retries
    #     one-by-one past a duplicate), so replicas racing land the same tuples.
    backfill_task = asyncio.create_task(_backfill_cascade_grants(app))
    try:
        yield
    finally:
        app.state.shutting_down = True
        backfill_task.cancel()
        fga_client = getattr(app.state, "fga", None)
        # Each close is isolated so one failing teardown can't strand the other resource.
        if fga_client is not None:
            with suppress(Exception):
                await fga_client.close()
        if lineage_http is not None:
            with suppress(Exception):
                await lineage_http.aclose()
        if dapr_client is not None:
            with suppress(Exception):
                await dapr_client.close()
        with suppress(Exception):
            await app.state.user_state.aclose()


_settings = get_settings()
# Application logging, before the app exists — every module here uses getLogger(__name__), and
# without this they propagate to a root logger with no handlers and are DISCARDED. That is not
# hypothetical: it hid a two-day lineage feed outage (see service_kit.setup_logging).
setup_logging()

app = FastAPI(
    title="Lance Namespace REST Catalog",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _settings.docs_enabled else None,
    redoc_url="/redoc" if _settings.docs_enabled else None,
    openapi_url="/openapi.json" if _settings.docs_enabled else None,
)
app.include_router(api_router)
# Wire the broadcast control-plane-event subscription that fills each replica's ring buffer. DaprApp always
# adds GET /dapr/subscribe; the subscription registers only when control_emit_enabled (see api/dapr.py).
register_control_dapr(app)
# Read-only maintenance gate (no-op unless LANCE_MAINTENANCE_READ_ONLY=true).
app.middleware("http")(maintenance_middleware)
# ONE ID PER REQUEST, reaching the logs. `RequestIDMiddleware` mints or echoes `X-Request-ID`, puts
# it on `request.state` and publishes it to the context var `setup_logging`'s filter reads — so a
# caller can quote an id from a failed request and an operator can grep for it. Pure ASGI, so it
# passes streaming bodies through untouched (this plane serves Arrow IPC).
app.add_middleware(RequestIDMiddleware)
# Reject oversized request bodies with 413 before they are buffered (Arrow-IPC OOM guard). See body_limit.py.
app.add_middleware(BodySizeLimitMiddleware, max_bytes=_settings.max_body_bytes)
# Outermost (added LAST → wraps everything, runs FIRST): shed a bulk-write burst with 429 once the
# concurrent-write cap is reached, BEFORE the body is buffered — so shedding relieves memory pressure rather
# than adding to it. Sits above body_limit so an over-cap write is rejected before even the size check. (P5)
app.add_middleware(WriteConcurrencyLimitMiddleware, max_concurrent=_settings.max_concurrent_writes)


# AND the fleet handlers, before the lance translator. `service_kit.exceptions.DomainError`
# subclasses `HTTPException`, so without `register_handlers` starlette's built-in handler renders
# it — status and headers intact, `{"detail": ...}` body — which is how the draining 503 came to
# declare problem+json over a body that was not one. Registered FIRST so the lance translator
# still wins for `RequestValidationError`, exactly the order `make_service_app` uses.
register_handlers(app)
install_problem_handlers(app, log)


async def _namespace_ready(request: Request) -> Readiness:
    """Report the resolved namespace id alongside ``ready`` — a boot-config fact worth surfacing on
    the probe. Best-effort: a backend that cannot answer ``namespace_id()`` is not a readiness fault
    (per-request resolution surfaces that as a domain error), so the pod stays Ready without the fact."""
    body = Readiness(status=ReadinessStatus.ready)
    ns = getattr(request.app.state, "namespace", None)
    if ns is not None:
        with suppress(LanceNamespaceError):
            body.components["namespace"] = str(ns.namespace_id())
    return body


# /livez + /readyz — the shared router (service_kit.probes), not a hand-rolled copy.
app.include_router(make_probes_router(_namespace_ready))
