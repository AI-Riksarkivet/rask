"""Cached IIIF service — wraps IiifService with async KVCache.

Caches manifest data (image ID lists) which rarely change.
Image URL building is pure computation and needs no caching.
"""

from __future__ import annotations

import logging

from app.core.config import CacheSettings
from app.services.iiif_service import IiifService
from app.services.kv import KVCache

logger = logging.getLogger(__name__)


class CachedIiifService:
    """Async IiifService wrapper with KVCache for manifest lookups."""

    def __init__(
        self,
        inner: IiifService,
        cache: KVCache,
        cache_settings: CacheSettings,
    ):
        self._inner = inner
        self._cache = cache
        self._cs = cache_settings

    def _key(self, *parts: str) -> str:
        return "iiif:" + ":".join(parts)

    # ── Cached reads ─────────────────────────────────────────────

    async def get_image_ids(self, batch_id: str) -> list[str]:
        """Cache image IDs — manifest content rarely changes."""
        key = self._key("manifest", batch_id)
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        result = await self._inner.get_image_ids(batch_id)
        await self._cache.set(key, result, ttl=self._cs.cache_config_ttl)
        return result

    # ── Pure delegation (no caching needed) ──────────────────────

    def build_image_url(
        self, image_id: str, query_params: str = "full/max/0/default.jpg"
    ) -> str:
        return self._inner.build_image_url(image_id, query_params)

    def build_image_urls(
        self, image_ids: list[str], query_params: str = "full/max/0/default.jpg"
    ) -> dict[str, str]:
        return self._inner.build_image_urls(image_ids, query_params)

    @staticmethod
    def file_extension(query_params: str = "full/max/0/default.jpg") -> str:
        return IiifService.file_extension(query_params)

    async def close(self) -> None:
        await self._inner.close()
