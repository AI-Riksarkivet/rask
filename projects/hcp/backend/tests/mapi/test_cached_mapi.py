"""Unit tests for CachedMapiService."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from app.core.config import CacheSettings, MapiSettings
from app.services.kv import KVCache
from app.services.cached_mapi import CachedMapiService
from app.services.mapi_service import MapiService

HCP_BASE = "https://test.hcp.example.com:9090/mapi"


@pytest.fixture
async def mapi(
    cache: KVCache, mapi_settings: MapiSettings, cache_settings: CacheSettings
) -> AsyncGenerator[CachedMapiService, None]:
    inner = MapiService(mapi_settings)
    svc = CachedMapiService(inner, cache, cache_settings)
    yield svc
    await inner.close()


# ── Cache miss → real request → cache populated ───────────────────────


async def test_get_cache_miss_then_hit(
    cache: KVCache,
    mapi: CachedMapiService,
    hcp_mock,
):
    route = hcp_mock.get(f"{HCP_BASE}/tenants").respond(
        200, json={"name": ["t1", "t2"]}
    )

    # First call — miss, hits HCP
    resp1 = await mapi.get("/tenants", username="testuser", password="testpass")
    assert resp1.status_code == 200
    assert resp1.json() == {"name": ["t1", "t2"]}
    assert route.call_count == 1

    # Second call — hit, no HCP request
    resp2 = await mapi.get("/tenants", username="testuser", password="testpass")
    assert resp2.status_code == 200
    assert resp2.json() == {"name": ["t1", "t2"]}
    assert route.call_count == 1  # Still 1


# ── Write invalidation ────────────────────────────────────────────────


async def test_put_invalidates_cache(
    cache: KVCache,
    mapi: CachedMapiService,
    hcp_mock,
):
    get_route = hcp_mock.get(f"{HCP_BASE}/tenants/t1/namespaces").respond(
        200, json={"name": ["ns1"]}
    )
    put_route = hcp_mock.put(f"{HCP_BASE}/tenants/t1/namespaces/ns1").respond(200)

    # Populate cache
    await mapi.get("/tenants/t1/namespaces", username="testuser", password="testpass")
    assert get_route.call_count == 1

    # Second GET — cache hit, no HCP call
    await mapi.get("/tenants/t1/namespaces", username="testuser", password="testpass")
    assert get_route.call_count == 1  # Still 1

    # Write invalidates
    await mapi.put(
        "/tenants/t1/namespaces/ns1",
        body={"some": "data"},
        username="testuser",
        password="testpass",
    )
    assert put_route.call_count == 1

    # Next GET should miss cache — hits HCP again
    resp = await mapi.get(
        "/tenants/t1/namespaces", username="testuser", password="testpass"
    )
    assert resp.status_code == 200
    assert get_route.call_count == 2  # Now 2 — cache was invalidated


# ── No-cache paths ────────────────────────────────────────────────────


async def test_no_cache_paths(
    cache: KVCache,
    mapi: CachedMapiService,
    hcp_mock,
):
    route = hcp_mock.get(f"{HCP_BASE}/logs").respond(200, json={"logs": []})

    await mapi.get("/logs", username="testuser", password="testpass")
    await mapi.get("/logs", username="testuser", password="testpass")
    # Both should hit HCP — /logs is never cached
    assert route.call_count == 2


# ── TTL selection ─────────────────────────────────────────────────────


async def test_ttl_selection(mapi: CachedMapiService):
    assert mapi._select_ttl("/tenants/t1/statistics") == 60  # stats
    assert mapi._select_ttl("/tenants/t1/consoleSecurity") == 600  # config
    assert mapi._select_ttl("/tenants/t1/namespaces") == 300  # default


# ── Cache key includes sorted query params ────────────────────────────


async def test_cache_key_includes_query(mapi: CachedMapiService):
    key1 = mapi._cache_key("/tenants", {"verbose": "true", "offset": "0"})
    key2 = mapi._cache_key("/tenants", {"offset": "0", "verbose": "true"})
    assert key1 == key2  # Sorted, so order doesn't matter
    assert "offset=0" in key1
    assert "verbose=true" in key1


# ── Non-200 responses are not cached ──────────────────────────────────


async def test_error_responses_not_cached(
    cache: KVCache,
    mapi: CachedMapiService,
    hcp_mock,
):
    route = hcp_mock.get(f"{HCP_BASE}/tenants/t1").respond(
        404, json={"error": "not found"}
    )

    await mapi.get("/tenants/t1", username="testuser", password="testpass")
    await mapi.get("/tenants/t1", username="testuser", password="testpass")
    # Both should hit HCP — 404 should not be cached
    assert route.call_count == 2


# ── Cache disabled: works without caching ─────────────────────────────


async def test_works_without_cache(mapi_settings: MapiSettings, hcp_mock):
    """When cache is disabled, CachedMapiService still works normally."""
    from key_value.aio.stores.null import NullStore

    cache_settings = CacheSettings(redis_url="", cache_key_prefix="test")
    cache = KVCache(NullStore(), enabled=False, has_url=False)
    assert not cache.enabled

    inner = MapiService(mapi_settings)
    mapi = CachedMapiService(inner, cache, cache_settings)
    route = hcp_mock.get(f"{HCP_BASE}/tenants").respond(200, json={"name": ["t1"]})

    resp = await mapi.get("/tenants", username="testuser", password="testpass")
    assert resp.json() == {"name": ["t1"]}
    assert route.call_count == 1

    await mapi.close()
    await inner.close()
    await cache.close()
