"""Composable key-value cache backed by py-key-value.

Replaces the monolithic CacheService with a thin wrapper around
py-key-value's AsyncKeyValue stores.
"""

from __future__ import annotations

from app.services.kv.factory import create_kv_cache
from app.services.kv.store import KVCache

__all__ = ["KVCache", "create_kv_cache"]
