"""Factory for creating KVCache instances.

Reads ``CacheSettings`` and assembles the appropriate py-key-value
store chain:

- **Redis configured**: ``RedisStore → PrefixKeysWrapper → TimeoutWrapper → RetryWrapper``
- **No Redis**: ``NullStore`` (all reads → None, writes → no-op)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from key_value.aio.protocols.key_value import AsyncKeyValueProtocol

from app.services.kv.store import KVCache

if TYPE_CHECKING:
    from app.core.config import CacheSettings

logger = logging.getLogger(__name__)


def create_kv_cache(settings: CacheSettings) -> KVCache:
    """Build a KVCache from application settings.

    Returns an unconnected cache — call ``await cache.connect()``
    before use (typically in the FastAPI lifespan handler).
    """
    if not settings.redis_url:
        from key_value.aio.stores.null import NullStore

        logger.info("No REDIS_URL — creating NullStore (caching disabled)")
        return KVCache(NullStore(), enabled=False, has_url=False)

    from key_value.aio.stores.redis import RedisStore
    from key_value.aio.wrappers.prefix_keys import PrefixKeysWrapper
    from key_value.aio.wrappers.retry import RetryWrapper
    from key_value.aio.wrappers.timeout import TimeoutWrapper

    redis_store = RedisStore(url=settings.redis_url)
    store: AsyncKeyValueProtocol = redis_store

    if settings.cache_key_prefix:
        store = PrefixKeysWrapper(key_value=store, prefix=settings.cache_key_prefix)

    store = TimeoutWrapper(key_value=store, timeout=5.0)
    store = RetryWrapper(key_value=store, max_retries=2, initial_delay=0.1)

    logger.info("Creating RedisStore KVCache (prefix=%s)", settings.cache_key_prefix)
    return KVCache(store, enabled=False, has_url=True, closeable=redis_store)
