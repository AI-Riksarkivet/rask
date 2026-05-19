"""Unit tests for KVCache."""

from __future__ import annotations

from app.services.kv import KVCache


# ── Basic get/set/delete ───────────────────────────────────────────────


async def test_get_returns_none_on_miss(cache: KVCache):
    assert await cache.get("nonexistent") is None


async def test_set_and_get(cache: KVCache):
    await cache.set("key1", {"hello": "world"}, ttl=60)
    result = await cache.get("key1")
    assert result == {"hello": "world"}


async def test_delete(cache: KVCache):
    await cache.set("key1", "value")
    await cache.delete("key1")
    assert await cache.get("key1") is None


async def test_set_with_datetime(cache: KVCache):
    """datetime objects are serialized via default=str."""
    from datetime import datetime

    data = {"ts": datetime(2024, 1, 1, 12, 0)}
    await cache.set("dt", data)
    result = await cache.get("dt")
    assert result is not None
    assert result["ts"] == "2024-01-01 12:00:00"


# ── Graceful degradation ──────────────────────────────────────────────


async def test_disabled_when_no_url():
    from key_value.aio.stores.null import NullStore

    kv = KVCache(NullStore(), enabled=False, has_url=False)
    assert not kv.enabled
    # All ops are no-ops
    assert await kv.get("k") is None
    await kv.set("k", "v")
    await kv.delete("k")


async def test_connect_enables_when_store_works():
    from key_value.aio.stores.memory import MemoryStore

    kv = KVCache(MemoryStore(), enabled=False, has_url=True)
    assert not kv.enabled
    await kv.connect()
    assert kv.enabled


async def test_connect_stays_disabled_without_url():
    from key_value.aio.stores.null import NullStore

    kv = KVCache(NullStore(), enabled=False, has_url=False)
    await kv.connect()
    assert not kv.enabled


async def test_ping_returns_true_when_enabled(cache: KVCache):
    assert await cache.ping() is True


async def test_ping_returns_false_when_disabled():
    from key_value.aio.stores.null import NullStore

    kv = KVCache(NullStore(), enabled=False, has_url=False)
    assert await kv.ping() is False
