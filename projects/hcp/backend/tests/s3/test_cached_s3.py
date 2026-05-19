"""Unit tests for CachedStorage wrapping HcpStorage."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import AsyncMock

import pytest

from app.core.config import CacheSettings, S3Settings
from app.services.kv import KVCache
from app.services.cached_storage import CachedStorage


@pytest.fixture
def mock_boto_client():
    """AsyncMock aioboto3 client for HcpStorage."""
    return AsyncMock()


@pytest.fixture
def s3(
    cache: KVCache,
    s3_settings: S3Settings,
    cache_settings: CacheSettings,
    mock_boto_client: AsyncMock,
) -> CachedStorage:
    from app.services.storage.adapters.hcp import HcpStorage

    inner = HcpStorage(s3_settings)
    inner._ops._client = mock_boto_client
    return CachedStorage(inner, cache, cache_settings)


# -- list_buckets caching --------------------------------------------------


async def test_list_buckets_cached(s3: CachedStorage, mock_boto_client: AsyncMock):
    mock_boto_client.list_buckets.return_value = {"Buckets": [{"Name": "b1"}]}

    # First call — miss
    result = await s3.list_buckets()
    assert result == {"Buckets": [{"Name": "b1"}]}
    assert mock_boto_client.list_buckets.call_count == 1

    # Second call — hit
    result2 = await s3.list_buckets()
    assert result2 == {"Buckets": [{"Name": "b1"}]}
    assert mock_boto_client.list_buckets.call_count == 1  # Still 1


# -- head_bucket caching ---------------------------------------------------


async def test_head_bucket_cached(s3: CachedStorage, mock_boto_client: AsyncMock):
    mock_boto_client.head_bucket.return_value = {
        "ResponseMetadata": {"HTTPStatusCode": 200}
    }

    await s3.head_bucket("mybucket")
    await s3.head_bucket("mybucket")
    assert mock_boto_client.head_bucket.call_count == 1


# -- list_objects first page cached, continuation not -----------------------


async def test_list_objects_first_page_cached(
    s3: CachedStorage, mock_boto_client: AsyncMock
):
    mock_boto_client.list_objects_v2.return_value = {
        "Contents": [{"Key": "a.txt"}],
        "IsTruncated": False,
        "KeyCount": 1,
    }

    await s3.list_objects("bucket1", prefix="docs/")
    await s3.list_objects("bucket1", prefix="docs/")
    assert mock_boto_client.list_objects_v2.call_count == 1


async def test_list_objects_continuation_not_cached(
    s3: CachedStorage, mock_boto_client: AsyncMock
):
    mock_boto_client.list_objects_v2.return_value = {
        "Contents": [],
        "IsTruncated": False,
        "KeyCount": 0,
    }

    await s3.list_objects("b", continuation_token="tok1")
    await s3.list_objects("b", continuation_token="tok1")
    assert mock_boto_client.list_objects_v2.call_count == 2


# -- head_object caching ---------------------------------------------------


async def test_head_object_cached(s3: CachedStorage, mock_boto_client: AsyncMock):
    mock_boto_client.head_object.return_value = {"ContentLength": 42}

    await s3.head_object("bucket", "key.txt")
    result = await s3.head_object("bucket", "key.txt")
    assert result == {"ContentLength": 42}
    assert mock_boto_client.head_object.call_count == 1


# -- get_object is never cached --------------------------------------------


async def test_get_object_not_cached(s3: CachedStorage, mock_boto_client: AsyncMock):
    mock_boto_client.get_object.return_value = {"Body": b"data"}

    await s3.get_object("bucket", "key.txt")
    await s3.get_object("bucket", "key.txt")
    assert mock_boto_client.get_object.call_count == 2


# -- ACL caching -----------------------------------------------------------


async def test_bucket_acl_cached(s3: CachedStorage, mock_boto_client: AsyncMock):
    mock_boto_client.get_bucket_acl.return_value = {"Owner": {}, "Grants": []}

    await s3.get_bucket_acl("bucket")
    await s3.get_bucket_acl("bucket")
    assert mock_boto_client.get_bucket_acl.call_count == 1


async def test_object_acl_cached(s3: CachedStorage, mock_boto_client: AsyncMock):
    mock_boto_client.get_object_acl.return_value = {"Owner": {}, "Grants": []}

    await s3.get_object_acl("bucket", "key")
    await s3.get_object_acl("bucket", "key")
    assert mock_boto_client.get_object_acl.call_count == 1


# -- versioning caching ----------------------------------------------------


async def test_versioning_cached(s3: CachedStorage, mock_boto_client: AsyncMock):
    mock_boto_client.get_bucket_versioning.return_value = {"Status": "Enabled"}

    await s3.get_bucket_versioning("bucket")
    await s3.get_bucket_versioning("bucket")
    assert mock_boto_client.get_bucket_versioning.call_count == 1


# -- Write invalidation ----------------------------------------------------


async def test_create_bucket_invalidates(
    s3: CachedStorage, mock_boto_client: AsyncMock, cache: KVCache
):
    mock_boto_client.list_buckets.return_value = {"Buckets": []}
    mock_boto_client.create_bucket.return_value = {}

    # Populate cache
    await s3.list_buckets()
    assert mock_boto_client.list_buckets.call_count == 1

    # Create invalidates list_buckets
    await s3.create_bucket("new-bucket")

    # Next list_buckets should miss
    await s3.list_buckets()
    assert mock_boto_client.list_buckets.call_count == 2


async def test_delete_object_invalidates(
    s3: CachedStorage, mock_boto_client: AsyncMock, cache: KVCache
):
    mock_boto_client.head_object.return_value = {"ContentLength": 10}
    mock_boto_client.delete_object.return_value = {}

    # Populate cache
    await s3.head_object("bucket", "file.txt")
    assert mock_boto_client.head_object.call_count == 1

    # Delete invalidates head_object
    await s3.delete_object("bucket", "file.txt")

    # Next head_object should miss
    await s3.head_object("bucket", "file.txt")
    assert mock_boto_client.head_object.call_count == 2


async def test_put_object_invalidates(
    s3: CachedStorage, mock_boto_client: AsyncMock, cache: KVCache
):
    mock_boto_client.list_objects_v2.return_value = {
        "Contents": [],
        "IsTruncated": False,
        "KeyCount": 0,
    }

    # Populate cache
    await s3.list_objects("bucket")
    assert mock_boto_client.list_objects_v2.call_count == 1

    # Put object invalidates list_objects
    mock_boto_client.upload_fileobj.return_value = None
    await s3.put_object("bucket", "new.txt", BytesIO(b"data"))

    # Next list_objects should miss
    await s3.list_objects("bucket")
    assert mock_boto_client.list_objects_v2.call_count == 2


async def test_put_versioning_invalidates(
    s3: CachedStorage, mock_boto_client: AsyncMock, cache: KVCache
):
    mock_boto_client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    mock_boto_client.put_bucket_versioning.return_value = {}

    await s3.get_bucket_versioning("bucket")
    assert mock_boto_client.get_bucket_versioning.call_count == 1

    await s3.put_bucket_versioning("bucket", "Suspended")

    await s3.get_bucket_versioning("bucket")
    assert mock_boto_client.get_bucket_versioning.call_count == 2


async def test_put_bucket_acl_invalidates(
    s3: CachedStorage, mock_boto_client: AsyncMock, cache: KVCache
):
    mock_boto_client.get_bucket_acl.return_value = {"Owner": {}, "Grants": []}
    mock_boto_client.put_bucket_acl.return_value = {}

    await s3.get_bucket_acl("bucket")
    assert mock_boto_client.get_bucket_acl.call_count == 1

    await s3.put_bucket_acl("bucket", {"Owner": {}, "Grants": []})

    await s3.get_bucket_acl("bucket")
    assert mock_boto_client.get_bucket_acl.call_count == 2


async def test_put_object_acl_invalidates(
    s3: CachedStorage, mock_boto_client: AsyncMock, cache: KVCache
):
    mock_boto_client.get_object_acl.return_value = {"Owner": {}, "Grants": []}
    mock_boto_client.put_object_acl.return_value = {}

    await s3.get_object_acl("bucket", "key")
    assert mock_boto_client.get_object_acl.call_count == 1

    await s3.put_object_acl("bucket", "key", {"Owner": {}, "Grants": []})

    await s3.get_object_acl("bucket", "key")
    assert mock_boto_client.get_object_acl.call_count == 2


async def test_copy_object_invalidates_dst(
    s3: CachedStorage, mock_boto_client: AsyncMock, cache: KVCache
):
    mock_boto_client.list_objects_v2.return_value = {
        "Contents": [],
        "IsTruncated": False,
        "KeyCount": 0,
    }
    mock_boto_client.copy_object.return_value = {}

    await s3.list_objects("dst-bucket")
    assert mock_boto_client.list_objects_v2.call_count == 1

    await s3.copy_object("src-bucket", "src.txt", "dst-bucket", "dst.txt")

    await s3.list_objects("dst-bucket")
    assert mock_boto_client.list_objects_v2.call_count == 2


async def test_delete_objects_invalidates(
    s3: CachedStorage, mock_boto_client: AsyncMock, cache: KVCache
):
    mock_boto_client.head_object.return_value = {"ContentLength": 10}
    mock_boto_client.delete_object.return_value = {}

    await s3.head_object("bucket", "a.txt")
    await s3.head_object("bucket", "b.txt")
    assert mock_boto_client.head_object.call_count == 2

    await s3.delete_objects("bucket", ["a.txt", "b.txt"])

    # Both should miss cache now
    await s3.head_object("bucket", "a.txt")
    await s3.head_object("bucket", "b.txt")
    assert mock_boto_client.head_object.call_count == 4
