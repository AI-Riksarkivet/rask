"""Integration tests exercising Boto3Operations against moto S3 mock.

These tests verify real aioboto3 call paths (not AsyncMock) against an
in-memory S3 backend provided by moto.

NOTE: moto 5.x patches botocore at the sync HTTP layer, but aiobotocore
expects async responses (``await http_response.content``).  Until moto
adds native aiobotocore support, these tests are skipped.  Real async
integration testing is covered by the MinIO tests (``test_minio_integration.py``).
"""

from __future__ import annotations

import io

import aioboto3
import pytest
from moto import mock_aws

from app.services.storage.adapters._boto3_ops import Boto3Operations
from app.services.storage.errors import StorageError

pytestmark = [
    pytest.mark.moto,
    pytest.mark.skip(reason="moto 5.x does not support aiobotocore async responses"),
]

BUCKET = "test-bucket"
REGION = "us-east-1"


@pytest.fixture
async def s3_ops():
    """Boto3Operations backed by moto with a pre-created bucket."""
    with mock_aws():
        session = aioboto3.Session()
        async with session.client("s3", region_name=REGION) as client:
            await client.create_bucket(Bucket=BUCKET)
            ops = Boto3Operations()
            ops._client = client
            yield ops


async def test_put_and_get_object(s3_ops: Boto3Operations):
    """Upload an object and retrieve it."""
    await s3_ops.put_object(BUCKET, "hello.txt", io.BytesIO(b"Hello, world!"))
    result = await s3_ops.get_object(BUCKET, "hello.txt")
    body = await result["Body"].read()
    assert body == b"Hello, world!"


async def test_head_object(s3_ops: Boto3Operations):
    """HEAD returns correct ContentLength."""
    data = b"some data here"
    await s3_ops.put_object(BUCKET, "sized.txt", io.BytesIO(data))
    result = await s3_ops.head_object(BUCKET, "sized.txt")
    assert result["ContentLength"] == len(data)


async def test_list_objects_with_prefix(s3_ops: Boto3Operations):
    """Prefix filtering returns only matching keys."""
    await s3_ops.put_object(BUCKET, "docs/a.txt", io.BytesIO(b"a"))
    await s3_ops.put_object(BUCKET, "docs/b.txt", io.BytesIO(b"b"))
    await s3_ops.put_object(BUCKET, "images/c.png", io.BytesIO(b"c"))

    result = await s3_ops.list_objects(BUCKET, prefix="docs/")
    keys = [obj["Key"] for obj in result.get("Contents", [])]
    assert "docs/a.txt" in keys
    assert "docs/b.txt" in keys
    assert "images/c.png" not in keys


async def test_delete_object(s3_ops: Boto3Operations):
    """Deleted object raises NoSuchKey on GET."""
    await s3_ops.put_object(BUCKET, "temp.txt", io.BytesIO(b"temp"))
    await s3_ops.delete_object(BUCKET, "temp.txt")

    with pytest.raises(StorageError) as exc_info:
        await s3_ops.get_object(BUCKET, "temp.txt")
    assert exc_info.value.code == "NoSuchKey"


async def test_copy_object(s3_ops: Boto3Operations):
    """Copied object has the same content as the source."""
    await s3_ops.put_object(BUCKET, "original.txt", io.BytesIO(b"original content"))
    await s3_ops.copy_object(BUCKET, "original.txt", BUCKET, "copy.txt")

    result = await s3_ops.get_object(BUCKET, "copy.txt")
    body = await result["Body"].read()
    assert body == b"original content"


async def test_create_and_complete_multipart(s3_ops: Boto3Operations):
    """Full multipart flow: create → upload 2 parts → complete → verify."""
    init = await s3_ops.create_multipart_upload(BUCKET, "multi.bin")
    upload_id = init["UploadId"]

    part1 = await s3_ops.upload_part(
        BUCKET, "multi.bin", upload_id, 1, io.BytesIO(b"A" * 5 * 1024 * 1024)
    )
    part2 = await s3_ops.upload_part(
        BUCKET, "multi.bin", upload_id, 2, io.BytesIO(b"B" * 3 * 1024 * 1024)
    )

    await s3_ops.complete_multipart_upload(
        BUCKET,
        "multi.bin",
        upload_id,
        [
            {"PartNumber": 1, "ETag": part1["ETag"]},
            {"PartNumber": 2, "ETag": part2["ETag"]},
        ],
    )

    result = await s3_ops.get_object(BUCKET, "multi.bin")
    body = await result["Body"].read()
    assert len(body) == 5 * 1024 * 1024 + 3 * 1024 * 1024


async def test_abort_multipart(s3_ops: Boto3Operations):
    """Aborting a multipart upload invalidates the upload ID."""
    init = await s3_ops.create_multipart_upload(BUCKET, "aborted.bin")
    upload_id = init["UploadId"]
    await s3_ops.upload_part(BUCKET, "aborted.bin", upload_id, 1, io.BytesIO(b"data"))
    await s3_ops.abort_multipart_upload(BUCKET, "aborted.bin", upload_id)

    with pytest.raises(StorageError) as exc_info:
        await s3_ops.list_parts(BUCKET, "aborted.bin", upload_id)
    assert exc_info.value.code == "NoSuchUpload"


async def test_list_parts(s3_ops: Boto3Operations):
    """Listing parts returns correct count and ETags."""
    init = await s3_ops.create_multipart_upload(BUCKET, "parts.bin")
    upload_id = init["UploadId"]

    etags = []
    for i in range(1, 4):
        result = await s3_ops.upload_part(
            BUCKET, "parts.bin", upload_id, i, io.BytesIO(f"part-{i}".encode())
        )
        etags.append(result["ETag"])

    parts_resp = await s3_ops.list_parts(BUCKET, "parts.bin", upload_id)
    parts = parts_resp["Parts"]
    assert len(parts) == 3
    assert [p["ETag"] for p in parts] == etags

    # Clean up
    await s3_ops.abort_multipart_upload(BUCKET, "parts.bin", upload_id)


async def test_generate_presigned_url(s3_ops: Boto3Operations):
    """Presigned URL contains expected query parameters."""
    await s3_ops.put_object(BUCKET, "signed.txt", io.BytesIO(b"data"))
    url = await s3_ops.generate_presigned_url(BUCKET, "signed.txt", expires_in=300)
    assert "signed.txt" in url
    assert "Expires" in url or "X-Amz-Expires" in url


async def test_generate_presigned_url_upload_part(s3_ops: Boto3Operations):
    """extra_params (UploadId, PartNumber) appear in the presigned URL."""
    url = await s3_ops.generate_presigned_url(
        BUCKET,
        "multi.bin",
        expires_in=600,
        method="upload_part",
        extra_params={"UploadId": "test-upload-id", "PartNumber": 3},
    )
    assert "test-upload-id" in url or "UploadId" in url
    assert "partNumber" in url or "PartNumber" in url


async def test_nosuchbucket_raises_storage_error(s3_ops: Boto3Operations):
    """Operations on a non-existent bucket raise StorageError with 404."""
    with pytest.raises(StorageError) as exc_info:
        await s3_ops.head_bucket("no-such-bucket-xyz")
    assert exc_info.value.http_status in (404, 400)
