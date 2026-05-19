"""Tests for GenericBoto3Storage adapter (mocked aioboto3 client)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from pydantic import SecretStr

from app.core.config import StorageSettings
from app.services.storage.adapters.generic_boto3 import GenericBoto3Storage
from app.services.storage.errors import StorageError, StorageOperationNotSupported


@pytest.fixture
def storage_settings() -> StorageSettings:
    return StorageSettings(
        storage_backend="minio",
        s3_endpoint_url="http://localhost:9000",
        s3_region="us-east-1",
        s3_verify_ssl=False,
        s3_addressing_style="path",
        s3_access_key="minioadmin",
        s3_secret_key=SecretStr("minioadmin123"),
    )


@pytest.fixture
def mock_client() -> AsyncMock:
    return AsyncMock()


@pytest.fixture
def service(
    storage_settings: StorageSettings, mock_client: AsyncMock
) -> GenericBoto3Storage:
    svc = GenericBoto3Storage(storage_settings)
    svc._ops._client = mock_client
    return svc


# ── Bucket operations ───────────────────────────────────────────────


async def test_list_buckets(service: GenericBoto3Storage, mock_client: AsyncMock):
    mock_client.list_buckets.return_value = {"Buckets": [{"Name": "b1"}]}
    result = await service.list_buckets()
    assert result["Buckets"][0]["Name"] == "b1"
    mock_client.list_buckets.assert_called_once()


async def test_create_bucket(service: GenericBoto3Storage, mock_client: AsyncMock):
    await service.create_bucket("new-bucket")
    mock_client.create_bucket.assert_called_once_with(Bucket="new-bucket")


async def test_head_bucket(service: GenericBoto3Storage, mock_client: AsyncMock):
    await service.head_bucket("my-bucket")
    mock_client.head_bucket.assert_called_once_with(Bucket="my-bucket")


async def test_delete_bucket(service: GenericBoto3Storage, mock_client: AsyncMock):
    await service.delete_bucket("old-bucket")
    mock_client.delete_bucket.assert_called_once_with(Bucket="old-bucket")


# ── Object operations ──────────────────────────────────────────────


async def test_list_objects_basic(service: GenericBoto3Storage, mock_client: AsyncMock):
    mock_client.list_objects_v2.return_value = {"Contents": []}
    await service.list_objects("bucket")
    mock_client.list_objects_v2.assert_called_once_with(
        Bucket="bucket", MaxKeys=1000, FetchOwner=True
    )


async def test_list_objects_with_prefix(
    service: GenericBoto3Storage, mock_client: AsyncMock
):
    mock_client.list_objects_v2.return_value = {"Contents": []}
    await service.list_objects("bucket", prefix="logs/", max_keys=10)
    mock_client.list_objects_v2.assert_called_once_with(
        Bucket="bucket", MaxKeys=10, Prefix="logs/", FetchOwner=True
    )


async def test_put_object(service: GenericBoto3Storage, mock_client: AsyncMock):
    body = MagicMock()
    await service.put_object("bucket", "key.txt", body)
    mock_client.upload_fileobj.assert_called_once_with(body, "bucket", "key.txt")


async def test_get_object(service: GenericBoto3Storage, mock_client: AsyncMock):
    mock_client.get_object.return_value = {"Body": b"data"}
    result = await service.get_object("bucket", "key.txt")
    assert result["Body"] == b"data"
    mock_client.get_object.assert_called_once_with(Bucket="bucket", Key="key.txt")


async def test_get_object_with_version_id(
    service: GenericBoto3Storage, mock_client: AsyncMock
):
    mock_client.get_object.return_value = {"Body": b"data"}
    await service.get_object("bucket", "key.txt", version_id="v1")
    mock_client.get_object.assert_called_once_with(
        Bucket="bucket", Key="key.txt", VersionId="v1"
    )


async def test_head_object(service: GenericBoto3Storage, mock_client: AsyncMock):
    mock_client.head_object.return_value = {"ContentLength": 100}
    result = await service.head_object("bucket", "key.txt")
    assert result["ContentLength"] == 100


async def test_delete_object(service: GenericBoto3Storage, mock_client: AsyncMock):
    await service.delete_object("bucket", "key.txt")
    mock_client.delete_object.assert_called_once_with(Bucket="bucket", Key="key.txt")


async def test_delete_object_with_version_id(
    service: GenericBoto3Storage, mock_client: AsyncMock
):
    await service.delete_object("bucket", "key.txt", version_id="v1")
    mock_client.delete_object.assert_called_once_with(
        Bucket="bucket", Key="key.txt", VersionId="v1"
    )


async def test_copy_object(service: GenericBoto3Storage, mock_client: AsyncMock):
    await service.copy_object("src-bucket", "src-key", "dst-bucket", "dst-key")
    mock_client.copy_object.assert_called_once_with(
        CopySource={"Bucket": "src-bucket", "Key": "src-key"},
        Bucket="dst-bucket",
        Key="dst-key",
    )


# ── Native batch delete ─────────────────────────────────────────────


async def test_delete_objects_native_batch(
    service: GenericBoto3Storage, mock_client: AsyncMock
):
    """GenericBoto3Storage uses native delete_objects (not individual deletes)."""
    mock_client.delete_objects.return_value = {"Deleted": []}
    result = await service.delete_objects("bucket", ["f1.txt", "f2.txt"])
    assert result == {}
    mock_client.delete_objects.assert_called_once_with(
        Bucket="bucket",
        Delete={
            "Objects": [{"Key": "f1.txt"}, {"Key": "f2.txt"}],
            "Quiet": True,
        },
    )


async def test_delete_objects_with_errors(
    service: GenericBoto3Storage, mock_client: AsyncMock
):
    mock_client.delete_objects.return_value = {
        "Errors": [{"Key": "f1.txt", "Code": "AccessDenied", "Message": "no access"}]
    }
    result = await service.delete_objects("bucket", ["f1.txt"])
    assert len(result["Errors"]) == 1
    assert result["Errors"][0]["Key"] == "f1.txt"


# ── ACL methods raise NotSupported ───────────────────────────────────


async def test_get_bucket_acl_raises(service: GenericBoto3Storage):
    with pytest.raises(StorageOperationNotSupported):
        await service.get_bucket_acl("bucket")


async def test_put_bucket_acl_raises(service: GenericBoto3Storage):
    with pytest.raises(StorageOperationNotSupported):
        await service.put_bucket_acl("bucket", {})


async def test_get_object_acl_raises(service: GenericBoto3Storage):
    with pytest.raises(StorageOperationNotSupported):
        await service.get_object_acl("bucket", "key")


async def test_put_object_acl_raises(service: GenericBoto3Storage):
    with pytest.raises(StorageOperationNotSupported):
        await service.put_object_acl("bucket", "key", {})


# ── Versioning ──────────────────────────────────────────────────────


async def test_get_bucket_versioning(
    service: GenericBoto3Storage, mock_client: AsyncMock
):
    mock_client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    result = await service.get_bucket_versioning("bucket")
    assert result["Status"] == "Enabled"


async def test_put_bucket_versioning(
    service: GenericBoto3Storage, mock_client: AsyncMock
):
    await service.put_bucket_versioning("bucket", "Suspended")
    mock_client.put_bucket_versioning.assert_called_once_with(
        Bucket="bucket", VersioningConfiguration={"Status": "Suspended"}
    )


# ── Object versions ─────────────────────────────────────────────────


async def test_list_object_versions(
    service: GenericBoto3Storage, mock_client: AsyncMock
):
    mock_client.list_object_versions.return_value = {
        "Versions": [],
        "DeleteMarkers": [],
    }
    result = await service.list_object_versions("bucket")
    assert "Versions" in result


# ── Presigned URLs ───────────────────────────────────────────────────


async def test_generate_presigned_url(
    service: GenericBoto3Storage, mock_client: AsyncMock
):
    mock_client.generate_presigned_url.return_value = "https://example.com/presign"
    result = await service.generate_presigned_url("bucket", "key")
    assert result == "https://example.com/presign"
    mock_client.generate_presigned_url.assert_called_once_with(
        "get_object",
        Params={"Bucket": "bucket", "Key": "key"},
        ExpiresIn=3600,
    )


# ── Multipart uploads ───────────────────────────────────────────────


async def test_create_multipart_upload(
    service: GenericBoto3Storage, mock_client: AsyncMock
):
    mock_client.create_multipart_upload.return_value = {"UploadId": "u1"}
    result = await service.create_multipart_upload("bucket", "key")
    assert result["UploadId"] == "u1"


async def test_upload_part(service: GenericBoto3Storage, mock_client: AsyncMock):
    body = MagicMock()
    mock_client.upload_part.return_value = {"ETag": '"etag"'}
    result = await service.upload_part("bucket", "key", "u1", 1, body)
    assert result["ETag"] == '"etag"'


async def test_complete_multipart_upload(
    service: GenericBoto3Storage, mock_client: AsyncMock
):
    parts = [{"PartNumber": 1, "ETag": '"etag"'}]
    mock_client.complete_multipart_upload.return_value = {"ETag": '"final"'}
    result = await service.complete_multipart_upload("bucket", "key", "u1", parts)
    assert result["ETag"] == '"final"'


async def test_abort_multipart_upload(
    service: GenericBoto3Storage, mock_client: AsyncMock
):
    await service.abort_multipart_upload("bucket", "key", "u1")
    mock_client.abort_multipart_upload.assert_called_once_with(
        Bucket="bucket", Key="key", UploadId="u1"
    )


async def test_list_parts(service: GenericBoto3Storage, mock_client: AsyncMock):
    mock_client.list_parts.return_value = {"Parts": []}
    result = await service.list_parts("bucket", "key", "u1")
    assert result["Parts"] == []


# ── Error wrapping ───────────────────────────────────────────────────


async def test_client_error_wraps_to_storage_error(
    service: GenericBoto3Storage, mock_client: AsyncMock
):
    mock_client.list_buckets.side_effect = ClientError(
        error_response={
            "Error": {"Code": "AccessDenied", "Message": "forbidden"},
            "ResponseMetadata": {"HTTPStatusCode": 403},
        },
        operation_name="ListBuckets",
    )
    with pytest.raises(StorageError) as exc_info:
        await service.list_buckets()
    assert exc_info.value.code == "AccessDenied"
    assert exc_info.value.http_status == 403


# ── with_credentials factory ─────────────────────────────────────────


def test_with_credentials(storage_settings: StorageSettings):
    svc = GenericBoto3Storage.with_credentials(
        storage_settings,
        "mykey",
        "mysecret",
        endpoint_url="http://custom:9000",
    )
    assert svc.settings is storage_settings
