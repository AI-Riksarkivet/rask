"""Cached Metadata Query service — wraps QueryService with KVCache.

Object and operation query results are cached with short TTLs.
Read-only API → no invalidation needed, only TTL expiry.
"""

from __future__ import annotations

import hashlib
import json
import logging

from opentelemetry import trace

from app.core.config import CacheSettings
from app.schemas.query import (
    ObjectQuery,
    ObjectQueryResponse,
    OperationQuery,
    OperationQueryResponse,
)
from app.services.kv import KVCache
from app.services.query_service import QueryService

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


def _hash_params(data: dict) -> str:
    """Produce a short deterministic hash of query parameters."""
    raw = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class CachedQueryService:
    """QueryService wrapper with transparent caching."""

    def __init__(
        self,
        inner: QueryService,
        cache: KVCache,
        cache_settings: CacheSettings,
    ):
        self._inner = inner
        self._cache = cache
        self._cache_settings = cache_settings

    @property
    def settings(self):
        return self._inner.settings

    async def object_query(
        self,
        tenant: str,
        query: ObjectQuery,
        *,
        username: str,
        password: str,
        auth_type: str | None = None,
    ) -> ObjectQueryResponse:
        params = query.model_dump(by_alias=True, exclude_none=True)
        cache_key = f"query:{tenant}:obj:{_hash_params(params)}"

        with tracer.start_as_current_span(
            "cached_query.object_query",
            attributes={"query.tenant": tenant},
        ) as span:
            if self._cache.enabled:
                cached = await self._cache.get(cache_key)
                if cached is not None:
                    span.set_attribute("cache.hit", True)
                    logger.debug("Cache HIT: %s", cache_key)
                    return ObjectQueryResponse.model_validate(cached)
                span.set_attribute("cache.hit", False)

            result = await self._inner.object_query(
                tenant,
                query,
                username=username,
                password=password,
                auth_type=auth_type,
            )

            if self._cache.enabled:
                ttl = self._cache_settings.cache_query_object_ttl
                data = result.model_dump(by_alias=True, exclude_none=True)
                await self._cache.set(cache_key, data, ttl=ttl)
                span.set_attribute("cache.stored", True)
                logger.debug("Cache SET: %s (ttl=%d)", cache_key, ttl)

            return result

    async def operation_query(
        self,
        tenant: str,
        query: OperationQuery,
        *,
        username: str,
        password: str,
        auth_type: str | None = None,
    ) -> OperationQueryResponse:
        params = query.model_dump(by_alias=True, exclude_none=True)
        cache_key = f"query:{tenant}:ops:{_hash_params(params)}"

        with tracer.start_as_current_span(
            "cached_query.operation_query",
            attributes={"query.tenant": tenant},
        ) as span:
            if self._cache.enabled:
                cached = await self._cache.get(cache_key)
                if cached is not None:
                    span.set_attribute("cache.hit", True)
                    logger.debug("Cache HIT: %s", cache_key)
                    return OperationQueryResponse.model_validate(cached)
                span.set_attribute("cache.hit", False)

            result = await self._inner.operation_query(
                tenant,
                query,
                username=username,
                password=password,
                auth_type=auth_type,
            )

            if self._cache.enabled:
                ttl = self._cache_settings.cache_query_operation_ttl
                data = result.model_dump(by_alias=True, exclude_none=True)
                await self._cache.set(cache_key, data, ttl=ttl)
                span.set_attribute("cache.stored", True)
                logger.debug("Cache SET: %s (ttl=%d)", cache_key, ttl)

            return result

    async def close(self):
        pass  # inner owns the client
