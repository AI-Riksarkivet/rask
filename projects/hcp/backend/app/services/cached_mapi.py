"""Cached MAPI service — wraps MapiService with KVCache via composition.

GET responses are cached; PUT/POST/DELETE invalidate affected entries.
Zero changes to endpoint files required.
"""

from __future__ import annotations

import logging
from typing import Any, Final
from urllib.parse import urlencode

import httpx
from opentelemetry import trace

from app.core.config import CacheSettings, MapiSettings
from app.services.kv import KVCache
from app.services.mapi_service import MapiService

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# Paths that should never be cached
_NO_CACHE_PATHS: Final = ("/logs", "/healthCheck", "/support")

# Path fragments → TTL override
_TTL_RULES: Final[list[tuple[str, str]]] = [
    ("statistics", "cache_stats_ttl"),
    ("chargeback", "cache_stats_ttl"),
    ("consoleSecurity", "cache_config_ttl"),
    ("permissions", "cache_config_ttl"),
    ("cifsShares", "cache_config_ttl"),
    ("protocolSettings", "cache_config_ttl"),
    ("emailNotification", "cache_config_ttl"),
    ("namespaceDefaults", "cache_config_ttl"),
    ("searchSecurity", "cache_config_ttl"),
]


class CachedMapiService:
    """MapiService wrapper with transparent caching on GET requests."""

    def __init__(
        self,
        inner: MapiService,
        cache: KVCache,
        cache_settings: CacheSettings,
    ):
        self._inner = inner
        self._cache = cache
        self._cache_settings = cache_settings
        self._cached_keys: set[str] = set()

    @property
    def settings(self) -> MapiSettings:
        return self._inner.settings

    def _cache_key(
        self,
        path: str,
        query: dict[str, Any] | None,
        host: str | None = None,
    ) -> str:
        prefix = f"mapi:{host}:" if host else "mapi:"
        key = f"{prefix}{path}"
        if query:
            filtered = {k: v for k, v in sorted(query.items()) if v is not None}
            if filtered:
                key += "?" + urlencode(filtered, doseq=True)
        return key

    def _select_ttl(self, path: str) -> int:
        for fragment, attr in _TTL_RULES:
            if fragment in path:
                return getattr(self._cache_settings, attr)
        return self._cache_settings.cache_default_ttl

    def _should_cache(self, path: str) -> bool:
        return not any(path.startswith(p) or path.endswith(p) for p in _NO_CACHE_PATHS)

    async def request(
        self,
        method: str,
        path: str,
        *,
        host: str | None = None,
        body: Any | None = None,
        query: dict[str, Any] | None = None,
        content_type: str = "application/json",
        accept: str = "application/json",
        username: str,
        password: str,
        auth_type: str | None = None,
        raw_body: bytes | None = None,
    ) -> httpx.Response:
        is_get = method.upper() == "GET"

        with tracer.start_as_current_span(
            "cached_mapi.request",
            attributes={"mapi.method": method, "mapi.path": path},
        ) as span:
            if is_get and self._cache.enabled and self._should_cache(path):
                cache_key = self._cache_key(path, query, host=host)
                cached = await self._cache.get(cache_key)
                if cached is not None:
                    span.set_attribute("cache.hit", True)
                    logger.debug("Cache HIT: %s", cache_key)
                    return httpx.Response(
                        status_code=200,
                        json=cached,
                    )
                span.set_attribute("cache.hit", False)

            resp = await self._inner.request(
                method,
                path,
                host=host,
                body=body,
                query=query,
                content_type=content_type,
                accept=accept,
                username=username,
                password=password,
                auth_type=auth_type,
                raw_body=raw_body,
            )

            if not self._cache.enabled:
                return resp

            if is_get and self._should_cache(path) and 200 <= resp.status_code < 300:
                cache_key = self._cache_key(path, query, host=host)
                try:
                    data = resp.json()
                    ttl = self._select_ttl(path)
                    await self._cache.set(cache_key, data, ttl=ttl)
                    self._cached_keys.add(cache_key)
                    span.set_attribute("cache.stored", True)
                    logger.debug("Cache SET: %s (ttl=%d)", cache_key, ttl)
                except (ValueError, TypeError):
                    pass  # Non-JSON responses are not cached
            elif not is_get:
                await self._invalidate_for_write(path, host=host)

            return resp

    async def _invalidate_for_write(self, path: str, host: str | None = None) -> None:
        """Invalidate cache entries affected by a write to *path*."""
        prefix = f"mapi:{host}:" if host else "mapi:"

        # Invalidate resource and sub-resources
        full_prefix = f"{prefix}{path}"
        to_delete = {k for k in self._cached_keys if k.startswith(full_prefix)}
        for k in to_delete:
            await self._cache.delete(k)
        self._cached_keys -= to_delete

        # Invalidate parent collection
        parts = path.rstrip("/").rsplit("/", 1)
        if len(parts) == 2:
            parent_prefix = f"{prefix}{parts[0]}"
            to_delete = {k for k in self._cached_keys if k.startswith(parent_prefix)}
            for k in to_delete:
                await self._cache.delete(k)
            self._cached_keys -= to_delete

    # ── Forwarded convenience methods ─────────────────────────────────

    async def get(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("GET", path, **kwargs)

    async def put(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("PUT", path, **kwargs)

    async def post(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("POST", path, **kwargs)

    async def delete(self, path: str, **kwargs) -> httpx.Response:
        return await self.request("DELETE", path, **kwargs)

    async def fetch_json(self, path: str, **kwargs) -> dict:
        return await self._inner.fetch_json(path, **kwargs)

    async def send(self, method: str, path: str, **kwargs) -> httpx.Response:
        return await self._inner.send(method, path, **kwargs)

    async def close(self):
        pass  # inner owns the client

    async def ping(self) -> bool:
        return await self._inner.ping()
