"""HCP Unified API — S3 data-plane + MAPI admin."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from app.api.dependencies import get_cache_service
from app.api.v1.router import api_router
from app.core.config import AuthSettings, StorageSettings
from app.core.middleware import RequestIDMiddleware
from app.core.telemetry import setup_telemetry
from app.services.kv import KVCache, create_kv_cache
from app.services.mapi_errors import MapiError

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.core.config import CacheSettings, MapiSettings, StorageSettings
    from app.services.mapi_service import MapiService
    from app.services.query_service import QueryService

    # ── Startup ──────────────────────────────────────────────────────
    cache_settings = CacheSettings()
    cache = create_kv_cache(cache_settings)
    await cache.connect()
    app.state.cache = cache

    mapi_settings = MapiSettings()
    mapi = MapiService(mapi_settings)
    if cache.enabled:
        from app.services.cached_mapi import CachedMapiService

        logger.info("Creating CachedMapiService singleton")
        app.state.mapi = CachedMapiService(mapi, cache, cache_settings)
    else:
        logger.info("Creating MapiService singleton")
        app.state.mapi = mapi

    app.state.s3_cache = {}
    app.state.lance_cache = {}

    # ── IIIF service (singleton, shared httpx client) ────────────────
    from app.services.iiif_service import IiifService

    iiif_inner = IiifService()
    if cache.enabled:
        from app.services.cached_iiif import CachedIiifService

        logger.info("Creating CachedIiifService singleton")
        app.state.iiif = CachedIiifService(iiif_inner, cache, cache_settings)
    else:
        from app.services.cached_iiif import CachedIiifService
        from key_value.aio.stores.null import NullStore

        app.state.iiif = CachedIiifService(
            iiif_inner,
            KVCache(NullStore(), enabled=False),
            cache_settings,
        )

    query = QueryService(mapi_settings)
    if cache.enabled:
        from app.services.cached_query import CachedQueryService

        logger.info("Creating CachedQueryService singleton")
        app.state.query = CachedQueryService(query, cache, cache_settings)
    else:
        logger.info("Creating QueryService singleton")
        app.state.query = query

    # S3 storage probe for readiness checks on non-HCP backends
    storage_settings = StorageSettings()
    app.state.storage_probe = None
    if storage_settings.storage_backend != "hcp":
        try:
            from app.services.storage.factory import create_storage

            probe = await create_storage(
                storage_settings,
                storage_settings.s3_access_key,
                storage_settings.s3_secret_key.get_secret_value(),
            )
            app.state.storage_probe = probe
            logger.info(
                "S3 storage probe created (backend=%s)",
                storage_settings.storage_backend,
            )
        except Exception:
            logger.warning("Failed to create S3 storage probe", exc_info=True)

    yield

    # ── Shutdown ─────────────────────────────────────────────────────
    if app.state.storage_probe is not None:
        try:
            await app.state.storage_probe.close()
        except Exception:
            logger.warning("Failed to close S3 storage probe", exc_info=True)

    for storage in app.state.s3_cache.values():
        try:
            await storage.close()
        except Exception:
            logger.warning("Failed to close S3 storage adapter", exc_info=True)

    await app.state.iiif.close()
    await app.state.query.close()
    await app.state.mapi.close()
    await cache.close()


ROOT_PATH = os.getenv("ROOT_PATH", "")

OPENAPI_TAGS = [
    {
        "name": "Authentication",
        "description": (
            "Login to obtain a JWT token.\n\n"
            "Use the **Authorize** lock icon (top-right) to log in. "
            "For tenant-scoped access, enter `tenant/username` in the username field "
            "(e.g. `dev-ai/admin`)."
        ),
    },
    {
        "name": "Health",
        "description": "Application health check.",
    },
    # ── S3 data-plane ──
    {
        "name": "S3 Buckets",
        "description": "Create, list, and manage S3 buckets (versioning, ACLs).",
    },
    {
        "name": "S3 Objects",
        "description": "Upload, download, copy, and delete objects within buckets.",
    },
    {
        "name": "S3 Versions",
        "description": "List object versions and delete markers for versioning-enabled buckets.",
    },
    {
        "name": "S3 Multipart Upload",
        "description": "Multipart upload: initiate, upload parts, complete, abort, and list.",
    },
    {
        "name": "S3 Credentials",
        "description": "Generate presigned URLs and retrieve S3 access credentials.",
    },
    # ── System admin ──
    {
        "name": "System Admin: Tenants",
        "description": "List and create tenants. "
        "**Requires: system-level user account.**",
    },
    {
        "name": "System Admin: Identity",
        "description": "Manage system-level user and group accounts. "
        "**Requires: system-level user account.**",
    },
    {
        "name": "System Admin: Infrastructure",
        "description": "View and manage network settings, licenses, and system statistics. "
        "**Requires: system-level user account.**",
    },
    {
        "name": "System Admin: Operations",
        "description": "Run health checks, prepare and download logs, and manage support access. "
        "**Requires: system-level user account.**",
    },
    {
        "name": "System Admin: Replication",
        "description": "Manage replication links, certificates, and schedules. "
        "**Requires: system-level user account.**",
    },
    {
        "name": "System Admin: Erasure Coding",
        "description": "Manage erasure-coding topologies. "
        "**Requires: system-level user account.**",
    },
    # ── Tenant admin ──
    {
        "name": "Tenant Admin: Settings",
        "description": "View and modify tenant configuration: console security, contact info, "
        "email notifications, namespace defaults, search security, permissions, "
        "service plans, CORS. **Requires: tenant-level admin role.**",
    },
    {
        "name": "Tenant Admin: Statistics",
        "description": "View tenant statistics and chargeback reports. "
        "**Requires: tenant-level monitor role.**",
    },
    {
        "name": "Tenant Admin: Identity",
        "description": "Manage tenant-level user and group accounts: create, modify, delete, "
        "reset passwords, and assign per-bucket data access permissions. "
        "**Requires: tenant-level security role.**",
    },
    {
        "name": "Tenant Admin: Content Classes",
        "description": "Manage content classes for your tenant. "
        "**Requires: tenant-level admin role.**",
    },
    # ── Namespace ──
    {
        "name": "Namespace: Management",
        "description": "Create, list, and manage namespaces and versioning settings. "
        "**Requires: tenant-level admin role.**",
    },
    {
        "name": "Namespace: Compliance",
        "description": "Manage compliance settings and retention classes per namespace. "
        "**Requires: tenant-level compliance role.**",
    },
    {
        "name": "Namespace: Access",
        "description": "Manage namespace permissions, protocol configuration (HTTP/REST, NFS, "
        "CIFS, SMTP), CORS, and replication collision settings. "
        "**Requires: tenant-level admin role.**",
    },
    {
        "name": "Namespace: Indexing",
        "description": "Manage custom metadata indexing settings. "
        "**Requires: tenant-level admin role.**",
    },
    {
        "name": "Namespace: Statistics",
        "description": "View namespace statistics and chargeback reports. "
        "**Requires: tenant-level monitor role.**",
    },
    {
        "name": "Namespace: Templates",
        "description": "Export and import namespace configuration templates. "
        "**Requires: tenant-level admin role.**",
    },
    # ── Query & exploration ──
    {
        "name": "Metadata Query",
        "description": "Search objects by metadata and audit create/delete/purge events "
        "via the HCP Metadata Query API.",
    },
    {
        "name": "IIIF",
        "description": "Fetch and cache IIIF manifests, resolve image URLs.",
    },
    {
        "name": "Lance Explorer",
        "description": "Browse and query Lance vector datasets stored in S3.",
    },
]

app = FastAPI(
    title="HCP Unified API",
    description=(
        "S3 data-plane + MAPI admin for Hitachi Content Platform.\n\n"
        "## Access levels\n\n"
        "- **S3 Buckets / S3 Objects** — S3 data operations\n"
        "- **System Admin: \u2026** — system-level MAPI (requires HCP system admin)\n"
        "- **Tenant Admin: \u2026** — tenant-level MAPI (requires tenant admin role)\n"
        "- **Namespace: \u2026** — namespace-level MAPI "
        "(requires admin/compliance role within tenant)\n"
    ),
    version="1.0.0",
    lifespan=lifespan,
    root_path=ROOT_PATH,
    openapi_tags=OPENAPI_TAGS,
)

setup_telemetry(app)

# ── Middleware (outermost → innermost) ────────────────────────────────
# Order matters: RequestID is outermost so every response gets an ID,
# even if GZip or CORS rejects the request.
app.add_middleware(RequestIDMiddleware)  # type: ignore[arg-type]
app.add_middleware(GZipMiddleware, minimum_size=500)  # type: ignore[arg-type]

_auth_settings = AuthSettings()
_cors_origins = [o.strip() for o in _auth_settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,  # type: ignore[arg-type]
    allow_origins=_cors_origins or ["*"],  # empty CORS_ORIGINS = allow all (dev mode)
    allow_credentials=bool(_cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Exception handlers ───────────────────────────────────────────────


@app.exception_handler(MapiError)
async def mapi_error_handler(request: Request, exc: MapiError):
    """Translate MAPI domain errors into JSON HTTP responses."""
    return JSONResponse(
        status_code=exc.http_status,
        content={"detail": exc.message},
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch-all for unhandled exceptions — log and return 500."""
    request_id = getattr(request.state, "request_id", "unknown")
    logger.exception(
        "Unhandled exception on %s %s [request_id=%s]",
        request.method,
        request.url.path,
        request_id,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "request_id": request_id},
    )


app.include_router(api_router, prefix="/api/v1")


# ── Health & readiness probes ─────────────────────────────────────────


@app.get("/liveness", tags=["Health"])
async def liveness():
    """Liveness probe — returns 200 if the process is running."""
    return {"status": "ok"}


@app.get("/readiness", tags=["Health"])
async def readiness(request: Request):
    """Readiness probe — checks backend services are reachable."""
    checks: dict[str, str] = {}
    ready = True

    storage_settings = StorageSettings()

    if storage_settings.storage_backend == "hcp":
        mapi = getattr(request.app.state, "mapi", None)
        if mapi is not None and await mapi.ping():
            checks["hcp"] = "reachable"
        else:
            checks["hcp"] = "unreachable"
            ready = False
    else:
        probe = getattr(request.app.state, "storage_probe", None)
        if probe is not None:
            try:
                await probe.list_buckets()
                checks["storage"] = "reachable"
            except Exception:
                checks["storage"] = "unreachable"
                ready = False
        else:
            checks["storage"] = "unconfigured"
            ready = False

    cache: KVCache | None = getattr(request.app.state, "cache", None)
    if cache is not None and cache.has_url:
        if await cache.ping():
            checks["cache"] = "connected"
        else:
            checks["cache"] = "disconnected"
            ready = False
    else:
        checks["cache"] = "disabled"

    return JSONResponse(
        status_code=200 if ready else 503,
        content={"status": "ready" if ready else "not ready", "checks": checks},
    )


@app.get("/health", tags=["Health"])
async def health(cache: KVCache | None = Depends(get_cache_service)):
    """Legacy health endpoint (backwards compatible)."""
    return {
        "status": "ok",
        "cache": "connected" if cache and cache.enabled else "disabled",
    }
