"""Application cache wrapper around py-key-value.

KVCache provides the same consumer-facing API that CacheService did
(get / set / delete) but backed by py-key-value's composable stores.

Responsibilities:
- OTel tracing + metrics (cache.hits, cache.misses, cache.errors)
- Graceful degradation (all operations swallow exceptions)
- JSON-safe value serialization (handles datetime via ``default=str``)
- Type-safe storage (always wraps values in ``{"_raw": ...}``)
"""

from __future__ import annotations

import json
import logging
from typing import Any

from key_value.aio.protocols.key_value import AsyncKeyValueProtocol
from opentelemetry import metrics, trace

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

_cache_hits = meter.create_counter(
    "cache.hits",
    description="Number of cache hits",
    unit="1",
)
_cache_misses = meter.create_counter(
    "cache.misses",
    description="Number of cache misses",
    unit="1",
)
_cache_errors = meter.create_counter(
    "cache.errors",
    description="Number of cache operation errors",
    unit="1",
)


class KVCache:
    """Async cache wrapper with OTel tracing and graceful degradation.

    Safe to call even when the backing store is down — all operations
    swallow exceptions and return ``None`` / no-op.

    Values are JSON-serialized internally so any JSON-compatible type
    (dicts, lists, strings, numbers, booleans) can be cached.
    """

    def __init__(
        self,
        store: AsyncKeyValueProtocol,
        *,
        enabled: bool = True,
        has_url: bool = False,
        closeable: object | None = None,
    ):
        self._store = store
        self._enabled = enabled
        self._has_url = has_url
        # The underlying store that supports close() — wrappers
        # (PrefixKeysWrapper, TimeoutWrapper, RetryWrapper) don't
        # forward close(), so we keep a direct reference.
        self._closeable = closeable

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def has_url(self) -> bool:
        return self._has_url

    async def connect(self) -> None:
        """Eagerly test the backing store.  Disables caching on failure."""
        if not self._has_url:
            logger.info("No REDIS_URL configured — caching disabled")
            return
        try:
            await self._store.put(key="_startup_ping", value={"ok": True}, ttl=5)
            self._enabled = True
            logger.info("Cache connected — caching enabled")
        except Exception:
            self._enabled = False
            logger.warning("Cache unavailable — caching disabled", exc_info=True)

    async def close(self) -> None:
        """Best-effort close of the backing store."""
        target = self._closeable or self._store
        close_fn = getattr(target, "close", None)
        if callable(close_fn):
            try:
                await close_fn()
            except Exception:
                pass
        self._enabled = False

    async def ping(self) -> bool:
        """Return True if the backing store is reachable."""
        if not self._enabled:
            return False
        try:
            await self._store.put(key="_ping", value={"ok": True}, ttl=5)
            return True
        except Exception:
            return False

    # ── Core operations ──────────────────────────────────────────────

    async def get(self, key: str) -> Any | None:
        if not self._enabled:
            return None
        with tracer.start_as_current_span(
            "cache.get", attributes={"cache.key": key}
        ) as span:
            try:
                result = await self._store.get(key=key)
                span.set_attribute("cache.hit", result is not None)
                attrs = {"cache.key_prefix": key.split(":")[0]}
                if result is not None:
                    _cache_hits.add(1, attrs)
                    return json.loads(result["_raw"])
                _cache_misses.add(1, attrs)
                return None
            except Exception:
                span.set_attribute("cache.error", True)
                _cache_errors.add(1, {"operation": "get"})
                logger.warning("Cache GET failed for %s", key, exc_info=True)
                return None

    async def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        if not self._enabled:
            return
        with tracer.start_as_current_span(
            "cache.set", attributes={"cache.key": key, "cache.ttl": ttl or 0}
        ) as span:
            try:
                raw = json.dumps(value, default=str)
                await self._store.put(key=key, value={"_raw": raw}, ttl=ttl)
            except Exception:
                span.set_attribute("cache.error", True)
                _cache_errors.add(1, {"operation": "set"})
                logger.warning("Cache SET failed for %s", key, exc_info=True)

    async def delete(self, key: str) -> None:
        if not self._enabled:
            return
        with tracer.start_as_current_span(
            "cache.delete", attributes={"cache.key": key}
        ) as span:
            try:
                await self._store.delete(key=key)
            except Exception:
                span.set_attribute("cache.error", True)
                _cache_errors.add(1, {"operation": "delete"})
                logger.warning("Cache DELETE failed for %s", key, exc_info=True)
