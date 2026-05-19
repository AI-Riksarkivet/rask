"""Storage factory — create the right adapter based on STORAGE_BACKEND."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.services.storage.protocol import StorageProtocol

if TYPE_CHECKING:
    from app.core.config import CacheSettings, S3Settings, StorageSettings
    from app.services.kv import KVCache

logger = logging.getLogger(__name__)


async def create_storage(
    settings: StorageSettings,
    access_key: str,
    secret_key: str,
    endpoint_url: str | None = None,
    *,
    s3_settings: S3Settings | None = None,
) -> StorageProtocol:
    """Create a plain (uncached) storage adapter for the configured backend.

    The adapter is returned already connected (``connect()`` has been called).

    Args:
        settings: StorageSettings with backend type and connection params.
        access_key: S3 access key.
        secret_key: S3 secret key.
        endpoint_url: Override endpoint URL (used by HCP per-tenant routing).
        s3_settings: Legacy S3Settings for the HCP code path.
    """
    match settings.storage_backend:
        case "hcp":
            from app.services.storage.adapters.hcp import HcpStorage

            if s3_settings is None:
                raise ValueError("s3_settings required for HCP backend")
            logger.info(
                "Creating HcpStorage (endpoint=%s)",
                endpoint_url or s3_settings.endpoint_url,
            )
            storage = HcpStorage.with_credentials(
                s3_settings,
                access_key,
                secret_key,
                endpoint_url=endpoint_url,
            )
        case "minio" | "generic":
            from app.services.storage.adapters.generic_boto3 import (
                GenericBoto3Storage,
            )

            logger.info(
                "Creating GenericBoto3Storage (backend=%s, endpoint=%s)",
                settings.storage_backend,
                endpoint_url or settings.s3_endpoint_url,
            )
            storage = GenericBoto3Storage.with_credentials(
                settings,
                access_key,
                secret_key,
                endpoint_url=endpoint_url,
            )
        case _:
            raise ValueError(f"Unknown storage backend: {settings.storage_backend}")

    await storage.connect()
    return storage


async def create_cached_storage(
    settings: StorageSettings,
    access_key: str,
    secret_key: str,
    endpoint_url: str | None = None,
    *,
    cache: KVCache,
    cache_settings: CacheSettings,
    s3_settings: S3Settings | None = None,
) -> StorageProtocol:
    """Create a cached storage adapter for the configured backend.

    All backends use CachedStorage (composition-based wrapper).
    The inner adapter is returned already connected.
    """
    from app.services.cached_storage import CachedStorage

    inner = await create_storage(
        settings,
        access_key,
        secret_key,
        endpoint_url,
        s3_settings=s3_settings,
    )
    backend_label = {
        "hcp": "HcpStorage",
        "minio": "GenericBoto3Storage",
        "generic": "GenericBoto3Storage",
    }.get(settings.storage_backend, settings.storage_backend)
    logger.info(
        "Creating CachedStorage[%s] (backend=%s, endpoint=%s)",
        backend_label,
        settings.storage_backend,
        endpoint_url or getattr(s3_settings, "endpoint_url", settings.s3_endpoint_url),
    )
    return CachedStorage(inner, cache, cache_settings)
