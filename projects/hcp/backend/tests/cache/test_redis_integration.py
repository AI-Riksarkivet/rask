"""Integration tests requiring a real Redis connection.

These tests are marked with ``@pytest.mark.redis`` and skipped
unless the ``REDIS_URL`` environment variable is set.  In CI they run
via ``dagger call test-integration --source=.`` which spins up a real
Redis sidecar.
"""

from __future__ import annotations

import os

import pytest

from app.core.config import CacheSettings
from app.services.kv import KVCache, create_kv_cache

REDIS_URL = os.environ.get("REDIS_URL", "")

pytestmark = [
    pytest.mark.redis,
    pytest.mark.skipif(not REDIS_URL, reason="REDIS_URL not set"),
]


@pytest.fixture
async def real_cache():
    settings = CacheSettings(
        redis_url=REDIS_URL,
        cache_key_prefix="integration-test",
        cache_default_ttl=10,
    )
    kv = create_kv_cache(settings)
    await kv.connect()
    yield kv
    await kv.close()


async def test_real_redis_connect(real_cache: KVCache):
    """KVCache connects and reports enabled with a real Redis."""
    assert real_cache.enabled is True


async def test_real_redis_set_get_delete(real_cache: KVCache):
    """Round-trip set/get/delete against real Redis."""
    await real_cache.set("hello", {"msg": "world"})
    result = await real_cache.get("hello")
    assert result == {"msg": "world"}

    await real_cache.delete("hello")
    assert await real_cache.get("hello") is None
