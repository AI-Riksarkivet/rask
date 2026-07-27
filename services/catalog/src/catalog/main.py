"""Lance Namespace REST Catalog — FastAPI application entry.

A Python/FastAPI adapter exposing the full Lance Namespace REST API (spec.yaml)
over a native ``lance.namespace`` backend (``DirectoryNamespace`` on MinIO/S3 by
default), with the pylance data plane filling operations the backend stubs.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx
from common import fga
from common.audit import configure_audit
from common.dapr_auth import assert_app_token_configured
from common.exceptions import install_problem_handlers
from common.lance_metrics import instrument_lance_if_available
from common.obs import configure_app_logging
from common.oidc import OIDCVerifier
from common.secrets import fetch_required_secrets
from common.user_state import UserStateStore
from dapr.aio.clients import DaprClient
from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from lance_namespace import LanceNamespaceError
from pydantic import SecretStr

from catalog.api.body_limit import BodySizeLimitMiddleware
from catalog.api.dapr import register_control_dapr
from catalog.api.load_shed import WriteConcurrencyLimitMiddleware
from catalog.api.maintenance import maintenance_middleware
from catalog.api.v1.router import api_router
from catalog.core.config import get_settings
from catalog.core.control_buffer import ControlEventBuffer
from catalog.core.control_emit import make_control_emitter
from catalog.core.lineage_emit import make_emitter
from catalog.core.namespace import build_namespace
from catalog.core.vending import make_vendor


log = logging.getLogger(__name__)
configure_app_logging()  # INFO audit/lifecycle logs reach OTLP (obs audit 2026-07-13)

PROBLEM_JSON = "application/problem+json"


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
    instrument_lance_if_available()  # Lance-native IO metrics — no-op until the pylance 9 bump
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
    if settings.oidc_enabled and settings.oidc_issuer and settings.oidc_audience:
        app.state.oidc = OIDCVerifier(
            settings.oidc_issuer,
            settings.oidc_audience,
            settings.oidc_cache_ttl,
            leeway=settings.oidc_leeway,
            allow_insecure=settings.oidc_allow_insecure,
            # Split-horizon (reverse-proxy IdP): fetch discovery/JWKS in-cluster while tokens
            # keep the public issuer string. Unset = derive the fetch URL from the issuer.
            discovery_overrides=({settings.oidc_issuer: settings.oidc_discovery_url} if settings.oidc_discovery_url else None),
        )
    if settings.fga_enabled:
        store_id, model_id = settings.fga_store_id, settings.fga_model_id
        if not (store_id and model_id):
            store_id, model_id = await fga.provision(settings.fga_api_url)
            log.info("openfga_provisioned", extra={"store_id": store_id, "model_id": model_id})
        app.state.fga = fga.make_client(settings.fga_api_url, store_id, model_id, timeout_seconds=settings.fga_timeout_seconds)
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
    try:
        yield
    finally:
        app.state.shutting_down = True
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
app = FastAPI(
    title="Lance Namespace REST Catalog",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if _settings.docs_enabled else None,
    openapi_url="/openapi.json" if _settings.docs_enabled else None,
)
app.include_router(api_router)
# Wire the broadcast control-plane-event subscription that fills each replica's ring buffer. DaprApp always
# adds GET /dapr/subscribe; the subscription registers only when control_emit_enabled (see api/dapr.py).
register_control_dapr(app)
# Read-only maintenance gate (no-op unless LANCE_MAINTENANCE_READ_ONLY=true).
app.middleware("http")(maintenance_middleware)
# Reject oversized request bodies with 413 before they are buffered (Arrow-IPC OOM guard). See body_limit.py.
app.add_middleware(BodySizeLimitMiddleware, max_bytes=_settings.max_body_bytes)
# Outermost (added LAST → wraps everything, runs FIRST): shed a bulk-write burst with 429 once the
# concurrent-write cap is reached, BEFORE the body is buffered — so shedding relieves memory pressure rather
# than adding to it. Sits above body_limit so an over-cap write is rejected before even the size check. (P5)
app.add_middleware(WriteConcurrencyLimitMiddleware, max_concurrent=_settings.max_concurrent_writes)


install_problem_handlers(app, log)


@app.get("/livez", tags=["health"])
async def livez() -> dict[str, str]:
    # async (not sync def) so the probe runs ON the event loop, not the blocking data-plane threadpool —
    # else liveness queues behind heavy Arrow-IPC work and fails exactly when the pod is busiest. No I/O here.
    return {"status": "ok"}


@app.get("/readyz", tags=["health"])
async def readyz(request: Request) -> JSONResponse:
    state = request.app.state
    if getattr(state, "shutting_down", False):
        return JSONResponse(status_code=503, content={"status": "shutting_down"})
    if not getattr(state, "startup_complete", False):
        return JSONResponse(status_code=503, content={"status": "starting"})

    body: dict[str, object] = {"status": "ready"}
    ns = getattr(state, "namespace", None)
    if ns is not None:
        with suppress(LanceNamespaceError):
            body["namespace"] = ns.namespace_id()
    return JSONResponse(status_code=200, content=body)
