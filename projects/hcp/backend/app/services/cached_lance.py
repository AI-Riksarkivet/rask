"""Cached Lance service — wraps LanceService with async KVCache.

Caches table list and schema only. All data reads (rows, vectors,
cells) go directly through Lance with native push-down.

Because LanceService methods are synchronous (lancedb has no async API),
this wrapper is itself async: it checks the cache first, then falls back
to ``asyncio.to_thread(inner.method, ...)`` on a miss.  Endpoints inject
this service directly — no extra ``to_thread`` calls needed at the API layer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.core.config import CacheSettings
from app.services.kv import KVCache
from app.services.lance_service import LanceService

logger = logging.getLogger(__name__)


class CachedLanceService:
    """Async LanceService wrapper with KVCache for metadata-only reads."""

    def __init__(
        self,
        inner: LanceService,
        cache: KVCache,
        cache_settings: CacheSettings,
    ):
        self._inner = inner
        self._cache = cache
        self._cs = cache_settings

    def _key(self, *parts: str) -> str:
        return "lance:" + ":".join(parts)

    # ── Cached metadata reads (async) ────────────────────────────

    async def list_tables(self) -> list[str]:
        """Cache table list — S3 directory listing is expensive."""
        key = self._key("tables", self._inner._base_uri)
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        result = await asyncio.to_thread(self._inner.list_tables)
        await self._cache.set(key, result, ttl=self._cs.cache_default_ttl)
        return result

    async def get_schema(self, table_name: str) -> dict[str, Any]:
        """Cache schema — small, rarely changes, identical for all users."""
        key = self._key("schema", self._inner._base_uri, table_name)
        cached = await self._cache.get(key)
        if cached is not None:
            return cached
        result = await asyncio.to_thread(self._inner.get_schema, table_name)
        await self._cache.set(key, result, ttl=self._cs.cache_config_ttl)
        return result

    # ── Uncached data reads (async via to_thread) ────────────────
    # Lance's native push-down is more efficient than serializing to
    # cache. These just bridge sync→async.

    async def get_rows(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self._inner.get_rows, *args, **kwargs)

    async def get_vector_preview(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self._inner.get_vector_preview, *args, **kwargs)

    async def get_cell_bytes(self, *args: Any, **kwargs: Any) -> bytes | None:
        return await asyncio.to_thread(self._inner.get_cell_bytes, *args, **kwargs)

    async def search(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self._inner.search, *args, **kwargs)

    async def create_fts_index(self, *args: Any, **kwargs: Any) -> None:
        return await asyncio.to_thread(self._inner.create_fts_index, *args, **kwargs)
