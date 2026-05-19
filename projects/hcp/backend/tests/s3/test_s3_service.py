"""Tests for HcpStorage adapter (mocked aioboto3 client)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.config import S3Settings
from app.services.storage.adapters.hcp import HcpStorage


@pytest.fixture
def s3_settings() -> S3Settings:
    return S3Settings(
        hcp_username="testuser",
        hcp_password="testpass",
        hcp_verify_ssl=False,
        s3_endpoint_url="https://s3.test.com",
        s3_region="us-east-1",
    )


@pytest.fixture
def mock_client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def service(s3_settings: S3Settings, mock_client: AsyncMock) -> HcpStorage:
    svc = HcpStorage(s3_settings)
    svc._ops._client = mock_client
    return svc


# ── Bucket operations ───────────────────────────────────────────────


async def test_list_buckets(service: HcpStorage, mock_client: AsyncMock):
    mock_client.list_buckets.return_value = {"Buckets": [{"Name": "b1"}]}
    result = await service.list_buckets()
    assert result["Buckets"][0]["Name"] == "b1"
    mock_client.list_buckets.assert_called_once()


async def test_create_bucket(service: HcpStorage, mock_client: AsyncMock):
    await service.create_bucket("new-bucket")
    mock_client.create_bucket.assert_called_once_with(Bucket="new-bucket")


async def test_head_bucket(service: HcpStorage, mock_client: AsyncMock):
    await service.head_bucket("my-bucket")
    mock_client.head_bucket.assert_called_once_with(Bucket="my-bucket")


async def test_delete_bucket(service: HcpStorage, mock_client: AsyncMock):
    await service.delete_bucket("old-bucket")
    mock_client.delete_bucket.assert_called_once_with(Bucket="old-bucket")


# ── Object operations ──────────────────────────────────────────────


async def test_list_objects_basic(service: HcpStorage, mock_client: AsyncMock):
    mock_client.list_objects_v2.return_value = {"Contents": []}
    await service.list_objects("bucket")
    mock_client.list_objects_v2.assert_called_once_with(
        Bucket="bucket", MaxKeys=1000, FetchOwner=True
    )


async def test_list_objects_with_prefix_and_token(
    service: HcpStorage, mock_client: AsyncMock
):
    mock_client.list_objects_v2.return_value = {"Contents": []}
    await service.list_objects(
        "bucket", prefix="logs/", max_keys=10, continuation_token="tok123"
    )
    mock_client.list_objects_v2.assert_called_once_with(
        Bucket="bucket",
        MaxKeys=10,
        Prefix="logs/",
        ContinuationToken="tok123",
        FetchOwner=True,
    )


async def test_put_object(service: HcpStorage, mock_client: AsyncMock):
    body = MagicMock()
    await service.put_object("bucket", "key.txt", body)
    mock_client.upload_fileobj.assert_called_once_with(body, "bucket", "key.txt")


async def test_get_object(service: HcpStorage, mock_client: AsyncMock):
    mock_client.get_object.return_value = {"Body": b"data"}
    result = await service.get_object("bucket", "key.txt")
    assert result["Body"] == b"data"
    mock_client.get_object.assert_called_once_with(Bucket="bucket", Key="key.txt")


async def test_head_object(service: HcpStorage, mock_client: AsyncMock):
    mock_client.head_object.return_value = {"ContentLength": 100}
    result = await service.head_object("bucket", "key.txt")
    assert result["ContentLength"] == 100


async def test_delete_object(service: HcpStorage, mock_client: AsyncMock):
    await service.delete_object("bucket", "key.txt")
    mock_client.delete_object.assert_called_once_with(Bucket="bucket", Key="key.txt")


async def test_copy_object(service: HcpStorage, mock_client: AsyncMock):
    await service.copy_object("src-bucket", "src-key", "dst-bucket", "dst-key")
    mock_client.copy_object.assert_called_once_with(
        CopySource={"Bucket": "src-bucket", "Key": "src-key"},
        Bucket="dst-bucket",
        Key="dst-key",
    )


async def test_delete_objects(service: HcpStorage, mock_client: AsyncMock):
    result = await service.delete_objects("bucket", ["f1.txt", "f2.txt"])
    assert mock_client.delete_object.call_count == 2
    assert result == {}


# ── Versioning ──────────────────────────────────────────────────────


async def test_get_bucket_versioning(service: HcpStorage, mock_client: AsyncMock):
    mock_client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    result = await service.get_bucket_versioning("bucket")
    assert result["Status"] == "Enabled"


async def test_put_bucket_versioning(service: HcpStorage, mock_client: AsyncMock):
    await service.put_bucket_versioning("bucket", "Suspended")
    mock_client.put_bucket_versioning.assert_called_once_with(
        Bucket="bucket",
        VersioningConfiguration={"Status": "Suspended"},
    )


# ── ACLs ────────────────────────────────────────────────────────────


async def test_get_bucket_acl(service: HcpStorage, mock_client: AsyncMock):
    mock_client.get_bucket_acl.return_value = {"Owner": {}, "Grants": []}
    result = await service.get_bucket_acl("bucket")
    assert "Owner" in result


async def test_put_bucket_acl(service: HcpStorage, mock_client: AsyncMock):
    acl = {"Owner": {"ID": "owner"}, "Grants": []}
    await service.put_bucket_acl("bucket", acl)
    mock_client.put_bucket_acl.assert_called_once_with(
        Bucket="bucket",
        AccessControlPolicy=acl,
    )


async def test_get_object_acl(service: HcpStorage, mock_client: AsyncMock):
    mock_client.get_object_acl.return_value = {"Owner": {}, "Grants": []}
    await service.get_object_acl("bucket", "key")
    mock_client.get_object_acl.assert_called_once_with(Bucket="bucket", Key="key")


async def test_put_object_acl(service: HcpStorage, mock_client: AsyncMock):
    acl = {"Owner": {"ID": "owner"}, "Grants": []}
    await service.put_object_acl("bucket", "key", acl)
    mock_client.put_object_acl.assert_called_once_with(
        Bucket="bucket",
        Key="key",
        AccessControlPolicy=acl,
    )
