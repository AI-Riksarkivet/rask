"""Shared FastAPI dependencies for S3 and MAPI services."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Annotated, AsyncGenerator

from fastapi import Depends, HTTPException, Request

from app.core.auth_utils import derive_s3_keys
from app.core.config import CacheSettings, MapiSettings, S3Settings, StorageSettings
from app.core.security import (
    HcpCredentials,
    oauth2_scheme,
    verify_token_with_credentials,
)
from app.core.tenant_routing import mapi_host_for_tenant, s3_endpoint_for_tenant
from app.services.kv import KVCache
from app.services.cached_iiif import CachedIiifService
from app.services.mapi_service import AuthenticatedMapiService
from app.services.query_service import AuthenticatedQueryService
from app.schemas.lance import LanceDatasetParams
from app.services.cached_lance import CachedLanceService
from app.services.lance_service import LanceService
from app.services.storage import StorageProtocol
from app.services.storage.factory import create_cached_storage, create_storage

logger = logging.getLogger(__name__)


@lru_cache()
def get_mapi_settings() -> MapiSettings:
    return MapiSettings()


@lru_cache()
def get_s3_settings() -> S3Settings:
    return S3Settings()


@lru_cache()
def get_cache_settings() -> CacheSettings:
    return CacheSettings()


@lru_cache()
def get_storage_settings() -> StorageSettings:
    return StorageSettings()


# ── Service access via app.state ─────────────────────────────────────
#
# All singletons (cache, mapi, s3_cache) live on app.state and are
# initialised in the lifespan handler (see main.py).  Dependencies
# read from request.app.state — no module-level mutable globals.


def get_cache_service(request: Request) -> KVCache | None:
    """Return the shared KVCache from app.state (or None)."""
    return getattr(request.app.state, "cache", None)


def get_iiif_service(request: Request) -> CachedIiifService:
    """Return the shared CachedIiifService from app.state."""
    return request.app.state.iiif


async def get_mapi_service(
    request: Request,
    token: Annotated[str, Depends(oauth2_scheme)],
) -> AsyncGenerator[AuthenticatedMapiService, None]:
    """Yield an AuthenticatedMapiService wrapping the singleton with JWT creds."""
    base = request.app.state.mapi  # MapiService or CachedMapiService
    creds = verify_token_with_credentials(token)
    settings = get_mapi_settings()
    host = mapi_host_for_tenant(creds.tenant, settings.hcp_domain)
    yield AuthenticatedMapiService(base, creds.username, creds.password, host=host)


# ── Metadata Query service ───────────────────────────────────────────


async def get_query_service(
    request: Request,
    token: Annotated[str, Depends(oauth2_scheme)],
) -> AsyncGenerator[AuthenticatedQueryService, None]:
    """Yield an AuthenticatedQueryService wrapping the singleton with JWT creds."""
    base = request.app.state.query  # QueryService or CachedQueryService
    creds = verify_token_with_credentials(token)
    yield AuthenticatedQueryService(base, creds.username, creds.password)


# ── S3 per-credential cache ──────────────────────────────────────────


def _derive_s3_keys(creds: HcpCredentials) -> tuple[str, str]:
    """Derive HCP S3 access_key / secret_key from plain credentials."""
    return derive_s3_keys(creds.username, creds.password)


async def get_s3_service(
    request: Request,
    token: Annotated[str, Depends(oauth2_scheme)],
) -> AsyncGenerator[StorageProtocol, None]:
    """Yield a StorageProtocol implementation keyed by the caller's credentials.

    HCP path: per-user client keyed by HcpCredentials, keys derived from JWT.
    MinIO/generic path: single shared client keyed by "default", uses configured creds.
    """
    storage_settings = get_storage_settings()
    s3_client_cache: dict = request.app.state.s3_cache
    cache: KVCache | None = getattr(request.app.state, "cache", None)
    use_cache = cache is not None and cache.enabled

    if storage_settings.storage_backend == "hcp":
        # HCP: per-user clients with derived credentials
        creds = verify_token_with_credentials(token)
        if creds not in s3_client_cache:
            s3_settings = get_s3_settings()
            access_key, secret_key = _derive_s3_keys(creds)
            endpoint_url = s3_endpoint_for_tenant(creds.tenant, s3_settings.hcp_domain)

            if use_cache:
                assert cache is not None
                s3_client_cache[creds] = await create_cached_storage(
                    storage_settings,
                    access_key,
                    secret_key,
                    endpoint_url=endpoint_url,
                    cache=cache,
                    cache_settings=get_cache_settings(),
                    s3_settings=s3_settings,
                )
            else:
                s3_client_cache[creds] = await create_storage(
                    storage_settings,
                    access_key,
                    secret_key,
                    endpoint_url=endpoint_url,
                    s3_settings=s3_settings,
                )
        yield s3_client_cache[creds]
    else:
        # MinIO / generic: single shared client
        cache_key = "default"
        if cache_key not in s3_client_cache:
            access_key = storage_settings.s3_access_key
            secret_key = storage_settings.s3_secret_key.get_secret_value()

            if use_cache:
                assert cache is not None
                s3_client_cache[cache_key] = await create_cached_storage(
                    storage_settings,
                    access_key,
                    secret_key,
                    cache=cache,
                    cache_settings=get_cache_settings(),
                )
            else:
                s3_client_cache[cache_key] = await create_storage(
                    storage_settings,
                    access_key,
                    secret_key,
                )
        yield s3_client_cache[cache_key]


# ── Lance per-credential cache ────────────────────────────────────────


async def get_lance_service(
    request: Request,
    token: Annotated[str, Depends(oauth2_scheme)],
    params: LanceDatasetParams = Depends(),
) -> AsyncGenerator[CachedLanceService, None]:
    """Yield a CachedLanceService for the given S3 bucket/path, keyed by credentials."""
    try:
        import lancedb as _  # noqa: F811, F401  # ty: ignore[unresolved-import]
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="Lance support not installed. Install with: pip install '.[lance]'",
        )
    creds = verify_token_with_credentials(token)
    access_key, secret_key = _derive_s3_keys(creds)
    settings = get_s3_settings()
    endpoint_url = s3_endpoint_for_tenant(creds.tenant, settings.hcp_domain)
    if not endpoint_url:
        raise HTTPException(
            status_code=400,
            detail="Cannot derive S3 endpoint: hcp_domain or tenant missing",
        )
    s3_uri = (
        f"s3://{params.bucket}/{params.path}"
        if params.path
        else f"s3://{params.bucket}"
    )

    cache_key = (creds, params.bucket, params.path)
    lance_cache: dict = request.app.state.lance_cache
    if cache_key not in lance_cache:
        inner = LanceService.with_credentials(
            s3_uri,
            access_key,
            secret_key,
            endpoint_url,
            verify_ssl=settings.hcp_verify_ssl,
        )
        cache: KVCache | None = getattr(request.app.state, "cache", None)
        if cache is not None and cache.enabled:
            lance_cache[cache_key] = CachedLanceService(
                inner, cache, get_cache_settings()
            )
        else:
            from key_value.aio.stores.null import NullStore

            lance_cache[cache_key] = CachedLanceService(
                inner,
                cache or KVCache(NullStore(), enabled=False),
                get_cache_settings(),
            )
        logger.info(
            "Creating CachedLanceService for %s/%s",
            params.bucket,
            params.path,
        )
    yield lance_cache[cache_key]
