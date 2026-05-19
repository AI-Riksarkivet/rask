"""Tests for CachedStorage (composition-based caching wrapper)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.config import CacheSettings
from app.services.cached_storage import CachedStorage


@pytest.fixture
def mock_inner() -> AsyncMock:
    """Mock StorageProtocol implementation."""
    mock = AsyncMock()
    mock.list_buckets.return_value = {"Buckets": [{"Name": "b1"}]}
    mock.head_bucket.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}
    mock.list_objects.return_value = {"Contents": [], "IsTruncated": False}
    mock.head_object.return_value = {"ContentLength": 100}
    mock.get_bucket_versioning.return_value = {"Status": "Enabled"}
    mock.get_bucket_acl.return_value = {"Owner": {}, "Grants": []}
    mock.get_object_acl.return_value = {"Owner": {}, "Grants": []}
    mock.create_bucket.return_value = {}
    mock.delete_bucket.return_value = {}
    mock.put_object.return_value = None
    mock.delete_object.return_value = {}
    mock.delete_objects.return_value = {}
    mock.copy_object.return_value = {}
    mock.put_bucket_versioning.return_value = {}
    mock.put_bucket_acl.return_value = {}
    mock.put_object_acl.return_value = {}
    return mock


@pytest.fixture
def mock_cache() -> AsyncMock:
    """Mock KVCache — get returns None (cache miss) by default."""
    cache = AsyncMock()
    cache.get.return_value = None
    return cache


@pytest.fixture
def cache_settings() -> CacheSettings:
    return CacheSettings()


@pytest.fixture
def cached(
    mock_inner: AsyncMock, mock_cache: AsyncMock, cache_settings: CacheSettings
) -> CachedStorage:
    return CachedStorage(mock_inner, mock_cache, cache_settings)


# ── Read caching: cache miss → call inner → set cache ────────────────


async def test_list_buckets_cache_miss(
    cached: CachedStorage, mock_inner: AsyncMock, mock_cache: AsyncMock
):
    result = await cached.list_buckets()
    assert result["Buckets"][0]["Name"] == "b1"
    mock_inner.list_buckets.assert_called_once()
    mock_cache.set.assert_called_once()


async def test_list_buckets_cache_hit(
    cached: CachedStorage, mock_inner: AsyncMock, mock_cache: AsyncMock
):
    mock_cache.get.return_value = {"Buckets": [{"Name": "cached"}]}
    result = await cached.list_buckets()
    assert result["Buckets"][0]["Name"] == "cached"
    mock_inner.list_buckets.assert_not_called()


async def test_head_bucket_cache_miss(
    cached: CachedStorage, mock_inner: AsyncMock, mock_cache: AsyncMock
):
    await cached.head_bucket("my-bucket")
    mock_inner.head_bucket.assert_called_once_with("my-bucket")
    mock_cache.set.assert_called_once()


async def test_list_objects_cache_miss(
    cached: CachedStorage, mock_inner: AsyncMock, mock_cache: AsyncMock
):
    await cached.list_objects("bucket", prefix="logs/")
    mock_inner.list_objects.assert_called_once()
    mock_cache.set.assert_called_once()


async def test_list_objects_with_continuation_skips_cache(
    cached: CachedStorage, mock_inner: AsyncMock, mock_cache: AsyncMock
):
    await cached.list_objects("bucket", continuation_token="tok123")
    mock_inner.list_objects.assert_called_once()
    mock_cache.set.assert_not_called()


async def test_head_object_cache_miss(
    cached: CachedStorage, mock_inner: AsyncMock, mock_cache: AsyncMock
):
    await cached.head_object("bucket", "key.txt")
    mock_inner.head_object.assert_called_once_with("bucket", "key.txt")
    mock_cache.set.assert_called_once()


async def test_get_bucket_versioning_cache_miss(
    cached: CachedStorage, mock_inner: AsyncMock, mock_cache: AsyncMock
):
    result = await cached.get_bucket_versioning("bucket")
    assert result["Status"] == "Enabled"
    mock_inner.get_bucket_versioning.assert_called_once()


# ── Write operations: delegate + invalidate ────────────────────────


async def test_create_bucket_invalidates(
    cached: CachedStorage, mock_inner: AsyncMock, mock_cache: AsyncMock
):
    await cached.create_bucket("new-bucket")
    mock_inner.create_bucket.assert_called_once_with("new-bucket")
    # Targeted invalidation: list_buckets + head_bucket + versioning + bucket_acl
    delete_keys = [c.args[0] for c in mock_cache.delete.call_args_list]
    assert "s3:list_buckets" in delete_keys
    assert "s3:head_bucket:new-bucket" in delete_keys
    assert "s3:versioning:new-bucket" in delete_keys
    assert "s3:bucket_acl:new-bucket" in delete_keys


async def test_delete_bucket_invalidates(
    cached: CachedStorage, mock_inner: AsyncMock, mock_cache: AsyncMock
):
    await cached.delete_bucket("old-bucket")
    mock_inner.delete_bucket.assert_called_once_with("old-bucket")
    delete_keys = [c.args[0] for c in mock_cache.delete.call_args_list]
    assert "s3:list_buckets" in delete_keys
    assert "s3:head_bucket:old-bucket" in delete_keys


async def test_put_object_invalidates(
    cached: CachedStorage, mock_inner: AsyncMock, mock_cache: AsyncMock
):
    body = MagicMock()
    await cached.put_object("bucket", "key.txt", body)
    mock_inner.put_object.assert_called_once_with("bucket", "key.txt", body)
    mock_cache.delete.assert_called_once_with("s3:head_object:bucket:key.txt")


async def test_delete_object_invalidates(
    cached: CachedStorage, mock_inner: AsyncMock, mock_cache: AsyncMock
):
    await cached.delete_object("bucket", "key.txt")
    mock_inner.delete_object.assert_called_once()
    delete_keys = [c.args[0] for c in mock_cache.delete.call_args_list]
    assert "s3:head_object:bucket:key.txt" in delete_keys
    assert "s3:object_acl:bucket:key.txt" in delete_keys
    assert mock_cache.delete.call_count == 2


async def test_delete_objects_invalidates(
    cached: CachedStorage, mock_inner: AsyncMock, mock_cache: AsyncMock
):
    await cached.delete_objects("bucket", ["a.txt", "b.txt"])
    mock_inner.delete_objects.assert_called_once()
    # 2 head_object + 2 object_acl deletes
    assert mock_cache.delete.call_count == 4


async def test_copy_object_invalidates(
    cached: CachedStorage, mock_inner: AsyncMock, mock_cache: AsyncMock
):
    await cached.copy_object("src", "skey", "dst", "dkey")
    mock_inner.copy_object.assert_called_once()
    delete_keys = [c.args[0] for c in mock_cache.delete.call_args_list]
    assert "s3:head_object:dst:dkey" in delete_keys
    assert "s3:object_acl:dst:dkey" in delete_keys


async def test_put_bucket_versioning_invalidates(
    cached: CachedStorage, mock_inner: AsyncMock, mock_cache: AsyncMock
):
    await cached.put_bucket_versioning("bucket", "Enabled")
    mock_inner.put_bucket_versioning.assert_called_once()
    mock_cache.delete.assert_called_once_with("s3:versioning:bucket")


# ── Key tracking: reads populate tracking, writes invalidate tracked ─


async def test_tracked_list_objects_invalidated_on_put(
    cached: CachedStorage, mock_inner: AsyncMock, mock_cache: AsyncMock
):
    """list_objects keys are tracked and deleted when put_object is called."""
    # Read populates tracking
    await cached.list_objects("bucket", prefix="docs/")
    # Write should invalidate tracked keys
    await cached.put_object("bucket", "docs/new.txt", MagicMock())

    # The tracked list_objects key + the head_object key should be deleted
    delete_keys = [c.args[0] for c in mock_cache.delete.call_args_list]
    assert any("list_objects" in k for k in delete_keys)
    assert "s3:head_object:bucket:docs/new.txt" in delete_keys


async def test_tracked_head_object_invalidated_on_delete_bucket(
    cached: CachedStorage, mock_inner: AsyncMock, mock_cache: AsyncMock
):
    """head_object keys are tracked per bucket and deleted on bucket operations."""
    await cached.head_object("mybucket", "file.txt")
    await cached.delete_bucket("mybucket")

    delete_keys = [c.args[0] for c in mock_cache.delete.call_args_list]
    # Tracked head_object key should be in deletes
    assert "s3:head_object:mybucket:file.txt" in delete_keys


# ── Pass-through operations ──────────────────────────────────────────


async def test_get_object_passes_through(cached: CachedStorage, mock_inner: AsyncMock):
    mock_inner.get_object.return_value = {"Body": b"data"}
    result = await cached.get_object("bucket", "key")
    assert result["Body"] == b"data"
    mock_inner.get_object.assert_called_once_with("bucket", "key", None)


async def test_multipart_passes_through(cached: CachedStorage, mock_inner: AsyncMock):
    mock_inner.create_multipart_upload.return_value = {"UploadId": "u1"}
    result = await cached.create_multipart_upload("bucket", "key")
    assert result["UploadId"] == "u1"
