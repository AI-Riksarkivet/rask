"""Integration tests exercising GenericBoto3Storage against a real MinIO instance.

These tests require a running MinIO server. They are skipped automatically
when MinIO is not reachable. Run them via:

  - Docker Compose:  docker compose -f .docker/docker-compose.yml up -d minio
                     cd backend && uv run pytest -m minio
  - Dagger:          dagger call test-minio-integration --source=.

Environment variables (with defaults matching docker-compose / Dagger):
  MINIO_ENDPOINT   http://localhost:9000
  S3_ACCESS_KEY    minioadmin
  S3_SECRET_KEY    minioadmin123
"""

from __future__ import annotations

import io
import os
import socket
import time
import uuid
from collections.abc import AsyncGenerator
from urllib.parse import urlparse

import pytest

from pydantic import SecretStr

from app.core.config import StorageSettings
from app.services.storage.adapters.generic_boto3 import GenericBoto3Storage
from app.services.storage.errors import StorageError, StorageOperationNotSupported

pytestmark = pytest.mark.minio

MINIO_ENDPOINT = os.environ.get("MINIO_ENDPOINT", "http://localhost:9000")
MINIO_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "minioadmin")
MINIO_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "minioadmin123")

# Max time to wait for MinIO to become ready (seconds)
_CONNECT_TIMEOUT = int(os.environ.get("MINIO_CONNECT_TIMEOUT", "30"))


def _minio_is_reachable() -> bool:
    """Wait for MinIO TCP port to accept connections (no boto3 — can't hang)."""
    parsed = urlparse(MINIO_ENDPOINT)
    host = parsed.hostname or "localhost"
    port = parsed.port or 9000
    deadline = time.monotonic() + _CONNECT_TIMEOUT
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            time.sleep(1)
    return False


# Skip the entire module if MinIO is not available
if not _minio_is_reachable():
    pytest.skip(
        "MinIO not reachable — skipping integration tests", allow_module_level=True
    )


def _unique_bucket() -> str:
    """Generate a unique bucket name to avoid test collisions."""
    return f"test-{uuid.uuid4().hex[:12]}"


@pytest.fixture
async def storage() -> AsyncGenerator[GenericBoto3Storage]:
    """GenericBoto3Storage connected to the real MinIO."""
    settings = StorageSettings(
        storage_backend="minio",
        s3_endpoint_url=MINIO_ENDPOINT,
        s3_region="us-east-1",
        s3_verify_ssl=False,
        s3_addressing_style="path",
        s3_access_key=MINIO_ACCESS_KEY,
        s3_secret_key=SecretStr(MINIO_SECRET_KEY),
    )
    svc = GenericBoto3Storage(settings)
    await svc.connect()
    yield svc
    await svc.close()


@pytest.fixture
async def bucket(storage: GenericBoto3Storage) -> AsyncGenerator[str]:
    """Create a unique bucket, yield its name, then clean up."""
    name = _unique_bucket()
    await storage.create_bucket(name)
    yield name
    # Teardown: delete all objects then the bucket
    try:
        result = await storage.list_objects(name)
        for obj in result.get("Contents", []):
            await storage.delete_object(name, obj["Key"])
        # Also clean up any versioned delete markers
        try:
            versions = await storage.list_object_versions(name)
            for v in versions.get("Versions", []):
                await storage.delete_object(name, v["Key"], version_id=v["VersionId"])
            for dm in versions.get("DeleteMarkers", []):
                await storage.delete_object(name, dm["Key"], version_id=dm["VersionId"])
        except Exception:
            pass
        await storage.delete_bucket(name)
    except Exception:
        pass


# ── Bucket operations ─────────────────────────────────────────────


async def test_create_and_head_bucket(storage: GenericBoto3Storage):
    name = _unique_bucket()
    await storage.create_bucket(name)
    try:
        await storage.head_bucket(name)
    finally:
        await storage.delete_bucket(name)


async def test_list_buckets(storage: GenericBoto3Storage):
    name = _unique_bucket()
    await storage.create_bucket(name)
    try:
        result = await storage.list_buckets()
        names = [b["Name"] for b in result["Buckets"]]
        assert name in names
    finally:
        await storage.delete_bucket(name)


async def test_delete_bucket(storage: GenericBoto3Storage):
    name = _unique_bucket()
    await storage.create_bucket(name)
    await storage.delete_bucket(name)
    with pytest.raises(StorageError):
        await storage.head_bucket(name)


async def test_head_nonexistent_bucket_raises(storage: GenericBoto3Storage):
    with pytest.raises(StorageError):
        await storage.head_bucket("no-such-bucket-xyz-999")


# ── Object operations ─────────────────────────────────────────────


async def test_put_get_object(storage: GenericBoto3Storage, bucket: str):
    await storage.put_object(bucket, "hello.txt", io.BytesIO(b"Hello, MinIO!"))
    result = await storage.get_object(bucket, "hello.txt")
    body = await result["Body"].read()
    assert body == b"Hello, MinIO!"


async def test_head_object(storage: GenericBoto3Storage, bucket: str):
    data = b"some data for head"
    await storage.put_object(bucket, "sized.txt", io.BytesIO(data))
    result = await storage.head_object(bucket, "sized.txt")
    assert result["ContentLength"] == len(data)


async def test_delete_object(storage: GenericBoto3Storage, bucket: str):
    await storage.put_object(bucket, "temp.txt", io.BytesIO(b"temp"))
    await storage.delete_object(bucket, "temp.txt")
    with pytest.raises(StorageError) as exc_info:
        await storage.get_object(bucket, "temp.txt")
    assert exc_info.value.code == "NoSuchKey"


async def test_copy_object(storage: GenericBoto3Storage, bucket: str):
    await storage.put_object(bucket, "original.txt", io.BytesIO(b"original content"))
    await storage.copy_object(bucket, "original.txt", bucket, "copy.txt")
    result = await storage.get_object(bucket, "copy.txt")
    body = await result["Body"].read()
    assert body == b"original content"


async def test_list_objects_with_prefix(storage: GenericBoto3Storage, bucket: str):
    await storage.put_object(bucket, "docs/a.txt", io.BytesIO(b"a"))
    await storage.put_object(bucket, "docs/b.txt", io.BytesIO(b"b"))
    await storage.put_object(bucket, "images/c.png", io.BytesIO(b"c"))

    result = await storage.list_objects(bucket, prefix="docs/")
    keys = [obj["Key"] for obj in result.get("Contents", [])]
    assert "docs/a.txt" in keys
    assert "docs/b.txt" in keys
    assert "images/c.png" not in keys


async def test_list_objects_with_delimiter(storage: GenericBoto3Storage, bucket: str):
    await storage.put_object(bucket, "a/1.txt", io.BytesIO(b"1"))
    await storage.put_object(bucket, "b/2.txt", io.BytesIO(b"2"))
    await storage.put_object(bucket, "root.txt", io.BytesIO(b"r"))

    result = await storage.list_objects(bucket, delimiter="/")
    prefixes = [p["Prefix"] for p in result.get("CommonPrefixes", [])]
    assert "a/" in prefixes
    assert "b/" in prefixes


# ── Batch delete ──────────────────────────────────────────────────


async def test_delete_objects_batch(storage: GenericBoto3Storage, bucket: str):
    for i in range(5):
        await storage.put_object(
            bucket, f"batch/{i}.txt", io.BytesIO(f"data-{i}".encode())
        )
    result = await storage.delete_objects(bucket, [f"batch/{i}.txt" for i in range(5)])
    assert result == {} or "Errors" not in result

    listing = await storage.list_objects(bucket, prefix="batch/")
    assert listing.get("Contents") is None or len(listing["Contents"]) == 0


# ── Versioning ────────────────────────────────────────────────────


async def test_bucket_versioning(storage: GenericBoto3Storage, bucket: str):
    # Initially versioning is not set
    result = await storage.get_bucket_versioning(bucket)
    assert result.get("Status") is None

    await storage.put_bucket_versioning(bucket, "Enabled")
    result = await storage.get_bucket_versioning(bucket)
    assert result["Status"] == "Enabled"

    await storage.put_bucket_versioning(bucket, "Suspended")
    result = await storage.get_bucket_versioning(bucket)
    assert result["Status"] == "Suspended"


async def test_object_versions(storage: GenericBoto3Storage, bucket: str):
    await storage.put_bucket_versioning(bucket, "Enabled")

    await storage.put_object(bucket, "versioned.txt", io.BytesIO(b"v1"))
    await storage.put_object(bucket, "versioned.txt", io.BytesIO(b"v2"))

    versions = await storage.list_object_versions(bucket, prefix="versioned.txt")
    version_list = versions.get("Versions", [])
    assert len(version_list) == 2

    # Latest version should be v2
    latest_vid = version_list[0]["VersionId"]
    result = await storage.get_object(bucket, "versioned.txt", version_id=latest_vid)
    body = await result["Body"].read()
    assert body == b"v2"


# ── Multipart uploads ────────────────────────────────────────────


async def test_multipart_upload(storage: GenericBoto3Storage, bucket: str):
    """Full multipart flow: create -> upload 2 parts -> complete -> verify."""
    init = await storage.create_multipart_upload(bucket, "multi.bin")
    upload_id = init["UploadId"]

    # MinIO requires minimum 5 MiB per part (except the last)
    part1_data = b"A" * 5 * 1024 * 1024  # 5 MiB — minimum for non-last part
    part2_data = b"B" * 1024  # 1 KiB — last part can be any size

    p1 = await storage.upload_part(
        bucket, "multi.bin", upload_id, 1, io.BytesIO(part1_data)
    )
    p2 = await storage.upload_part(
        bucket, "multi.bin", upload_id, 2, io.BytesIO(part2_data)
    )

    await storage.complete_multipart_upload(
        bucket,
        "multi.bin",
        upload_id,
        [
            {"PartNumber": 1, "ETag": p1["ETag"]},
            {"PartNumber": 2, "ETag": p2["ETag"]},
        ],
    )

    # Check content-length via head instead of downloading the full body
    head = await storage.head_object(bucket, "multi.bin")
    assert head["ContentLength"] == len(part1_data) + len(part2_data)


async def test_abort_multipart_upload(storage: GenericBoto3Storage, bucket: str):
    init = await storage.create_multipart_upload(bucket, "aborted.bin")
    upload_id = init["UploadId"]
    await storage.upload_part(
        bucket, "aborted.bin", upload_id, 1, io.BytesIO(b"A" * 1024)
    )
    await storage.abort_multipart_upload(bucket, "aborted.bin", upload_id)

    # After abort, listing parts should fail or return empty
    with pytest.raises(StorageError):
        await storage.list_parts(bucket, "aborted.bin", upload_id)


async def test_list_parts(storage: GenericBoto3Storage, bucket: str):
    init = await storage.create_multipart_upload(bucket, "parts.bin")
    upload_id = init["UploadId"]

    for i in range(1, 4):
        await storage.upload_part(
            bucket, "parts.bin", upload_id, i, io.BytesIO(f"part-{i}".encode())
        )

    parts_resp = await storage.list_parts(bucket, "parts.bin", upload_id)
    assert len(parts_resp["Parts"]) == 3

    await storage.abort_multipart_upload(bucket, "parts.bin", upload_id)


# ── Presigned URLs ────────────────────────────────────────────────


async def test_presigned_url_contains_signature(
    storage: GenericBoto3Storage, bucket: str
):
    await storage.put_object(bucket, "signed.txt", io.BytesIO(b"data"))
    url = await storage.generate_presigned_url(bucket, "signed.txt", expires_in=300)
    assert "signed.txt" in url
    assert "X-Amz-Signature" in url


# ── ACL operations raise NotSupported ─────────────────────────────


async def test_acl_operations_raise_not_supported(
    storage: GenericBoto3Storage, bucket: str
):
    with pytest.raises(StorageOperationNotSupported):
        await storage.get_bucket_acl(bucket)
    with pytest.raises(StorageOperationNotSupported):
        await storage.put_bucket_acl(bucket, {})
    with pytest.raises(StorageOperationNotSupported):
        await storage.get_object_acl(bucket, "key")
    with pytest.raises(StorageOperationNotSupported):
        await storage.put_object_acl(bucket, "key", {})


# ── CORS operations raise NotSupported ────────────────────────────


async def test_cors_operations_raise_not_supported(
    storage: GenericBoto3Storage, bucket: str
):
    with pytest.raises(StorageOperationNotSupported):
        await storage.get_bucket_cors(bucket)
    with pytest.raises(StorageOperationNotSupported):
        await storage.put_bucket_cors(bucket, {})
    with pytest.raises(StorageOperationNotSupported):
        await storage.delete_bucket_cors(bucket)


# ── StorageProtocol compliance ────────────────────────────────────


def test_implements_storage_protocol(storage: GenericBoto3Storage):
    from app.services.storage.protocol import StorageProtocol

    assert isinstance(storage, StorageProtocol)
