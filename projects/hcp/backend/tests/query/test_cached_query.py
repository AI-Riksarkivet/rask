"""Unit tests for CachedQueryService."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from app.core.config import CacheSettings, MapiSettings
from app.schemas.query import ObjectQuery, OperationQuery
from app.services.kv import KVCache
from app.services.cached_query import CachedQueryService
from app.services.query_service import QueryService

from .conftest import QUERY_URL


@pytest.fixture
async def query_svc(
    cache: KVCache, query_settings: MapiSettings, cache_settings: CacheSettings
) -> AsyncGenerator[CachedQueryService, None]:
    inner = QueryService(query_settings)
    svc = CachedQueryService(inner, cache, cache_settings)
    yield svc
    await inner.close()


_OBJ_RESPONSE = {
    "status": {"totalResults": 2, "results": 2, "code": "COMPLETE"},
    "resultSet": [
        {
            "urlName": "/docs/a.pdf",
            "operation": "CREATED",
            "changeTimeMilliseconds": "1706745600000",
            "version": "0",
        },
        {
            "urlName": "/docs/b.pdf",
            "operation": "CREATED",
            "changeTimeMilliseconds": "1707350400000",
            "version": "0",
        },
    ],
}

_OP_RESPONSE = {
    "status": {"totalResults": 1, "results": 1, "code": "COMPLETE"},
    "resultSet": [
        {
            "urlName": "/docs/a.pdf",
            "operation": "DELETED",
            "changeTimeMilliseconds": "1710460800000",
            "version": "0",
        },
    ],
}


# ── Object query: cache miss → cache hit ──────────────────────────────


async def test_object_query_cache_miss_then_hit(
    query_svc: CachedQueryService, hcp_mock
):
    route = hcp_mock.post(QUERY_URL).respond(200, json=_OBJ_RESPONSE)

    # First call — miss, hits HCP
    r1 = await query_svc.object_query(
        "mock",
        ObjectQuery(query="*:*", count=5),
        username="testuser",
        password="testpass",
    )
    assert r1.status.total_results == 2
    assert route.call_count == 1

    # Second call — hit, no HCP request
    r2 = await query_svc.object_query(
        "mock",
        ObjectQuery(query="*:*", count=5),
        username="testuser",
        password="testpass",
    )
    assert r2.status.total_results == 2
    assert route.call_count == 1  # Still 1


# ── Operation query: cache miss → cache hit ───────────────────────────


async def test_operation_query_cache_miss_then_hit(
    query_svc: CachedQueryService, hcp_mock
):
    route = hcp_mock.post(QUERY_URL).respond(200, json=_OP_RESPONSE)

    r1 = await query_svc.operation_query(
        "mock", OperationQuery(count=10), username="testuser", password="testpass"
    )
    assert r1.status.total_results == 1
    assert route.call_count == 1

    r2 = await query_svc.operation_query(
        "mock", OperationQuery(count=10), username="testuser", password="testpass"
    )
    assert r2.status.total_results == 1
    assert route.call_count == 1  # Still 1


# ── Different params create separate cache entries ────────────────────


async def test_different_params_miss_cache(query_svc: CachedQueryService, hcp_mock):
    route = hcp_mock.post(QUERY_URL).respond(200, json=_OBJ_RESPONSE)

    await query_svc.object_query(
        "mock",
        ObjectQuery(query="*:*", count=5),
        username="testuser",
        password="testpass",
    )
    assert route.call_count == 1

    # Different count → different cache key → miss
    await query_svc.object_query(
        "mock",
        ObjectQuery(query="*:*", count=10),
        username="testuser",
        password="testpass",
    )
    assert route.call_count == 2


# ── Cache disabled: works without caching ─────────────────────────────


async def test_works_without_cache(query_settings: MapiSettings, hcp_mock):
    from key_value.aio.stores.null import NullStore

    cache_settings = CacheSettings(redis_url="", cache_key_prefix="test")
    cache = KVCache(NullStore(), enabled=False, has_url=False)
    assert not cache.enabled

    inner = QueryService(query_settings)
    svc = CachedQueryService(inner, cache, cache_settings)
    route = hcp_mock.post(QUERY_URL).respond(200, json=_OBJ_RESPONSE)

    r1 = await svc.object_query(
        "mock", ObjectQuery(query="*:*"), username="testuser", password="testpass"
    )
    assert r1.status.total_results == 2

    # Without cache, second call should also hit HCP
    r2 = await svc.object_query(
        "mock", ObjectQuery(query="*:*"), username="testuser", password="testpass"
    )
    assert r2.status.total_results == 2
    assert route.call_count == 2

    await svc.close()
    await inner.close()
    await cache.close()
