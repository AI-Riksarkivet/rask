"""Composition-based caching wrapper for any StorageProtocol implementation.

Wraps any StorageProtocol via the decorator pattern. One class works with
any backend — HCP, MinIO, Ceph, AWS, or future adapters.

Uses explicit key tracking instead of pattern-based invalidation,
so the backing cache store doesn't need SCAN support.
"""

from __future__ import annotations

import logging
from typing import IO

from opentelemetry import trace

from app.core.config import CacheSettings
from app.services.kv import KVCache
from app.services.storage.protocol import StorageProtocol

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# Beyond this limit, new keys aren't tracked — cache TTL handles expiry.
_MAX_TRACKED_PER_GROUP = 2048


class CachedStorage:
    """Transparent caching wrapper for any StorageProtocol implementation."""

    def __init__(
        self,
        inner: StorageProtocol,
        cache: KVCache,
        cache_settings: CacheSettings,
    ):
        self._inner = inner
        self._cache = cache
        self._cs = cache_settings
        # Track cached keys by (operation, bucket) for targeted invalidation
        self._tracked: dict[tuple[str, str], set[str]] = {}

    def _key(self, *parts: str) -> str:
        return "s3:" + ":".join(parts)

    def _track(self, op: str, bucket: str, cache_key: str) -> None:
        """Register a cache key for later invalidation."""
        tracked = self._tracked.setdefault((op, bucket), set())
        if len(tracked) >= _MAX_TRACKED_PER_GROUP:
            return  # TTL will handle expiry for excess keys
        tracked.add(cache_key)

    async def _invalidate_tracked(self, op: str, bucket: str) -> None:
        """Delete all tracked cache keys for (operation, bucket)."""
        keys = self._tracked.pop((op, bucket), set())
        for key in keys:
            await self._cache.delete(key)

    # ── Lifecycle (delegate to inner) ──────────────────────────────────

    async def connect(self) -> None:
        await self._inner.connect()

    async def close(self) -> None:
        await self._inner.close()

    # ── Cached reads ───────────────────────────────────────────────────

    async def list_buckets(self) -> dict:
        with tracer.start_as_current_span("cached_s3.list_buckets") as span:
            key = self._key("list_buckets")
            cached = await self._cache.get(key)
            if cached is not None:
                span.set_attribute("cache.hit", True)
                return cached
            span.set_attribute("cache.hit", False)
            result = await self._inner.list_buckets()
            await self._cache.set(key, result, ttl=self._cs.cache_s3_list_ttl)
            return result

    async def head_bucket(self, name: str) -> dict:
        with tracer.start_as_current_span(
            "cached_s3.head_bucket", attributes={"s3.bucket": name}
        ) as span:
            key = self._key("head_bucket", name)
            cached = await self._cache.get(key)
            if cached is not None:
                span.set_attribute("cache.hit", True)
                return cached
            span.set_attribute("cache.hit", False)
            result = await self._inner.head_bucket(name)
            await self._cache.set(key, result, ttl=self._cs.cache_s3_meta_ttl)
            return result

    async def list_objects(
        self,
        bucket: str,
        prefix: str | None = None,
        max_keys: int = 1000,
        continuation_token: str | None = None,
        delimiter: str | None = None,
        fetch_owner: bool = True,
    ) -> dict:
        if continuation_token is not None:
            return await self._inner.list_objects(
                bucket, prefix, max_keys, continuation_token, delimiter, fetch_owner
            )
        with tracer.start_as_current_span(
            "cached_s3.list_objects",
            attributes={"s3.bucket": bucket, "s3.prefix": prefix or ""},
        ) as span:
            key = self._key(
                "list_objects", bucket, prefix or "", delimiter or "", str(max_keys)
            )
            cached = await self._cache.get(key)
            if cached is not None:
                span.set_attribute("cache.hit", True)
                return cached
            span.set_attribute("cache.hit", False)
            result = await self._inner.list_objects(
                bucket, prefix, max_keys, continuation_token, delimiter, fetch_owner
            )
            await self._cache.set(key, result, ttl=self._cs.cache_s3_list_ttl)
            self._track("list_objects", bucket, key)
            return result

    async def head_object(self, bucket: str, key: str) -> dict:
        with tracer.start_as_current_span(
            "cached_s3.head_object",
            attributes={"s3.bucket": bucket, "s3.key": key},
        ) as span:
            cache_key = self._key("head_object", bucket, key)
            cached = await self._cache.get(cache_key)
            if cached is not None:
                span.set_attribute("cache.hit", True)
                return cached
            span.set_attribute("cache.hit", False)
            result = await self._inner.head_object(bucket, key)
            await self._cache.set(cache_key, result, ttl=self._cs.cache_s3_meta_ttl)
            self._track("head_object", bucket, cache_key)
            return result

    async def get_bucket_versioning(self, bucket: str) -> dict:
        with tracer.start_as_current_span(
            "cached_s3.get_bucket_versioning",
            attributes={"s3.bucket": bucket},
        ) as span:
            key = self._key("versioning", bucket)
            cached = await self._cache.get(key)
            if cached is not None:
                span.set_attribute("cache.hit", True)
                return cached
            span.set_attribute("cache.hit", False)
            result = await self._inner.get_bucket_versioning(bucket)
            await self._cache.set(key, result, ttl=self._cs.cache_s3_meta_ttl)
            return result

    async def get_bucket_acl(self, bucket: str) -> dict:
        with tracer.start_as_current_span(
            "cached_s3.get_bucket_acl",
            attributes={"s3.bucket": bucket},
        ) as span:
            key = self._key("bucket_acl", bucket)
            cached = await self._cache.get(key)
            if cached is not None:
                span.set_attribute("cache.hit", True)
                return cached
            span.set_attribute("cache.hit", False)
            result = await self._inner.get_bucket_acl(bucket)
            await self._cache.set(key, result, ttl=self._cs.cache_s3_meta_ttl)
            return result

    async def get_object_acl(self, bucket: str, key: str) -> dict:
        with tracer.start_as_current_span(
            "cached_s3.get_object_acl",
            attributes={"s3.bucket": bucket, "s3.key": key},
        ) as span:
            cache_key = self._key("object_acl", bucket, key)
            cached = await self._cache.get(cache_key)
            if cached is not None:
                span.set_attribute("cache.hit", True)
                return cached
            span.set_attribute("cache.hit", False)
            result = await self._inner.get_object_acl(bucket, key)
            await self._cache.set(cache_key, result, ttl=self._cs.cache_s3_meta_ttl)
            self._track("object_acl", bucket, cache_key)
            return result

    # ── Uncached reads (streams / large results) ──────────────────────

    async def get_object(
        self, bucket: str, key: str, version_id: str | None = None
    ) -> dict:
        return await self._inner.get_object(bucket, key, version_id)

    async def list_object_versions(
        self,
        bucket: str,
        prefix: str | None = None,
        max_keys: int = 1000,
        key_marker: str | None = None,
        version_id_marker: str | None = None,
    ) -> dict:
        return await self._inner.list_object_versions(
            bucket, prefix, max_keys, key_marker, version_id_marker
        )

    async def generate_presigned_url(
        self,
        bucket: str,
        key: str,
        expires_in: int = 3600,
        method: str = "get_object",
        extra_params: dict[str, str | int] | None = None,
    ) -> str:
        return await self._inner.generate_presigned_url(
            bucket, key, expires_in, method, extra_params
        )

    # ── Write operations: delegate + invalidate ────────────────────────

    async def _invalidate_bucket(self, name: str) -> None:
        """Invalidate all cached data for a specific bucket."""
        await self._cache.delete(self._key("list_buckets"))
        await self._cache.delete(self._key("head_bucket", name))
        await self._cache.delete(self._key("versioning", name))
        await self._cache.delete(self._key("bucket_acl", name))
        await self._invalidate_tracked("list_objects", name)
        await self._invalidate_tracked("head_object", name)
        await self._invalidate_tracked("object_acl", name)

    async def create_bucket(self, name: str) -> dict:
        result = await self._inner.create_bucket(name)
        await self._invalidate_bucket(name)
        return result

    async def delete_bucket(self, name: str) -> dict:
        result = await self._inner.delete_bucket(name)
        await self._invalidate_bucket(name)
        return result

    async def put_object(self, bucket: str, key: str, body: IO[bytes]) -> None:
        await self._inner.put_object(bucket, key, body)
        await self._invalidate_tracked("list_objects", bucket)
        await self._cache.delete(self._key("head_object", bucket, key))

    async def delete_object(
        self, bucket: str, key: str, version_id: str | None = None
    ) -> dict:
        result = await self._inner.delete_object(bucket, key, version_id)
        await self._invalidate_tracked("list_objects", bucket)
        await self._cache.delete(self._key("head_object", bucket, key))
        await self._cache.delete(self._key("object_acl", bucket, key))
        return result

    async def delete_objects(self, bucket: str, keys: list[str]) -> dict:
        result = await self._inner.delete_objects(bucket, keys)
        await self._invalidate_tracked("list_objects", bucket)
        for k in keys:
            await self._cache.delete(self._key("head_object", bucket, k))
            await self._cache.delete(self._key("object_acl", bucket, k))
        return result

    async def copy_object(
        self,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
    ) -> dict:
        result = await self._inner.copy_object(src_bucket, src_key, dst_bucket, dst_key)
        await self._invalidate_tracked("list_objects", dst_bucket)
        await self._cache.delete(self._key("head_object", dst_bucket, dst_key))
        await self._cache.delete(self._key("object_acl", dst_bucket, dst_key))
        return result

    async def put_bucket_versioning(self, bucket: str, status: str) -> dict:
        result = await self._inner.put_bucket_versioning(bucket, status)
        await self._cache.delete(self._key("versioning", bucket))
        return result

    async def put_bucket_acl(self, bucket: str, acl: dict) -> dict:
        result = await self._inner.put_bucket_acl(bucket, acl)
        await self._cache.delete(self._key("bucket_acl", bucket))
        return result

    async def put_object_acl(self, bucket: str, key: str, acl: dict) -> dict:
        result = await self._inner.put_object_acl(bucket, key, acl)
        await self._cache.delete(self._key("object_acl", bucket, key))
        return result

    # ── CORS (pass-through + invalidate) ──────────────────────────────

    async def get_bucket_cors(self, bucket: str) -> dict:
        return await self._inner.get_bucket_cors(bucket)

    async def put_bucket_cors(self, bucket: str, cors_configuration: dict) -> dict:
        return await self._inner.put_bucket_cors(bucket, cors_configuration)

    async def delete_bucket_cors(self, bucket: str) -> dict:
        return await self._inner.delete_bucket_cors(bucket)

    # ── Multipart uploads (pass-through, no caching) ──────────────────

    async def create_multipart_upload(self, bucket: str, key: str) -> dict:
        return await self._inner.create_multipart_upload(bucket, key)

    async def upload_part(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        part_number: int,
        body: IO[bytes],
    ) -> dict:
        return await self._inner.upload_part(bucket, key, upload_id, part_number, body)

    async def complete_multipart_upload(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        parts: list[dict],
    ) -> dict:
        result = await self._inner.complete_multipart_upload(
            bucket, key, upload_id, parts
        )
        await self._invalidate_tracked("list_objects", bucket)
        await self._cache.delete(self._key("head_object", bucket, key))
        return result

    async def abort_multipart_upload(
        self, bucket: str, key: str, upload_id: str
    ) -> dict:
        return await self._inner.abort_multipart_upload(bucket, key, upload_id)

    async def list_parts(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        max_parts: int = 1000,
    ) -> dict:
        return await self._inner.list_parts(bucket, key, upload_id, max_parts)

    async def list_multipart_uploads(
        self,
        bucket: str,
        prefix: str | None = None,
        max_uploads: int = 1000,
    ) -> dict:
        return await self._inner.list_multipart_uploads(bucket, prefix, max_uploads)
