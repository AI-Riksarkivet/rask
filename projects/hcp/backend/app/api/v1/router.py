"""Aggregated API v1 router — S3 + MAPI endpoints."""

from fastapi import APIRouter, Depends

from app.core.security import get_current_user
from app.api.v1.endpoints import auth
from app.api.v1.endpoints.s3 import buckets, credentials, multipart, objects, versions
from app.api.v1.endpoints.mapi.system import (
    tenants as sys_tenants,
    user_accounts as sys_user_accounts,
    group_accounts as sys_group_accounts,
    infrastructure,
    operations,
    replication,
    erasure_coding,
)
from app.api.v1.endpoints.mapi.tenant import (
    settings as tenant_settings,
    statistics as tenant_statistics,
    user_accounts as tenant_user_accounts,
    group_accounts as tenant_group_accounts,
    content_classes,
)
from app.api.v1.endpoints.mapi.namespace import (
    management as ns_management,
    compliance as ns_compliance,
    access as ns_access,
    indexing as ns_indexing,
    statistics as ns_statistics,
    templates as ns_templates,
)
from app.api.v1.endpoints.query import search as query_search
from app.api.v1.endpoints import iiif as iiif_explorer
from app.api.v1.endpoints import lance as lance_explorer

api_router = APIRouter()

# ── Auth (public — no token required) ────────────────────────────────
api_router.include_router(auth.router)

# ── All routes below require authentication ──────────────────────────
_auth = [Depends(get_current_user)]

# ── S3 data-plane ────────────────────────────────────────────────────
api_router.include_router(buckets.router, dependencies=_auth)
api_router.include_router(objects.router, dependencies=_auth)
api_router.include_router(versions.router, dependencies=_auth)
api_router.include_router(multipart.router, dependencies=_auth)
api_router.include_router(credentials.router, dependencies=_auth)

# ── System-level MAPI (requires HCP system admin) ───────────────────
api_router.include_router(sys_tenants.router, prefix="/mapi", dependencies=_auth)
api_router.include_router(sys_user_accounts.router, prefix="/mapi", dependencies=_auth)
api_router.include_router(sys_group_accounts.router, prefix="/mapi", dependencies=_auth)
api_router.include_router(infrastructure.router, prefix="/mapi", dependencies=_auth)
api_router.include_router(operations.router, prefix="/mapi", dependencies=_auth)
api_router.include_router(replication.router, prefix="/mapi", dependencies=_auth)
api_router.include_router(erasure_coding.router, prefix="/mapi", dependencies=_auth)

# ── Tenant-level MAPI (requires tenant admin) ───────────────────────
api_router.include_router(tenant_settings.router, prefix="/mapi", dependencies=_auth)
api_router.include_router(tenant_statistics.router, prefix="/mapi", dependencies=_auth)
api_router.include_router(
    tenant_user_accounts.router, prefix="/mapi", dependencies=_auth
)
api_router.include_router(
    tenant_group_accounts.router, prefix="/mapi", dependencies=_auth
)
api_router.include_router(content_classes.router, prefix="/mapi", dependencies=_auth)

# ── Namespace-level MAPI (requires admin/compliance role) ────────────
# Templates must be registered before management so /export is matched
# before the {ns_name} path parameter.
api_router.include_router(ns_templates.router, prefix="/mapi", dependencies=_auth)
api_router.include_router(ns_management.router, prefix="/mapi", dependencies=_auth)
api_router.include_router(ns_compliance.router, prefix="/mapi", dependencies=_auth)
api_router.include_router(ns_access.router, prefix="/mapi", dependencies=_auth)
api_router.include_router(ns_indexing.router, prefix="/mapi", dependencies=_auth)
api_router.include_router(ns_statistics.router, prefix="/mapi", dependencies=_auth)

# ── Metadata Query API ──────────────────────────────────────────────
api_router.include_router(query_search.router, prefix="/query", dependencies=_auth)

# ── IIIF ──────────────────────────────────────────────────────────────
api_router.include_router(iiif_explorer.router, dependencies=_auth)

# ── Lance Data Explorer ───────────────────────────────────────────────
api_router.include_router(lance_explorer.router, dependencies=_auth)
