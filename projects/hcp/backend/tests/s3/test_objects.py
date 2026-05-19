"""Tests for S3 object CRUD endpoints."""

from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from botocore.exceptions import ClientError
from httpx import AsyncClient

from app.services.storage.errors import StorageError


async def test_list_objects_empty(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    mock_s3_service.list_objects.return_value = {
        "Contents": [],
        "IsTruncated": False,
        "KeyCount": 0,
    }
    resp = await client.get("/api/v1/buckets/my-bucket/objects", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["objects"] == []
    assert body["is_truncated"] is False
    assert body["key_count"] == 0


async def test_list_objects_with_data(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    mock_s3_service.list_objects.return_value = {
        "Contents": [
            {
                "Key": "file1.txt",
                "Size": 100,
                "LastModified": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "ETag": '"abc"',
            },
            {
                "Key": "file2.txt",
                "Size": 200,
                "LastModified": datetime(2024, 2, 1, tzinfo=timezone.utc),
                "ETag": '"def"',
            },
        ],
        "IsTruncated": False,
        "KeyCount": 2,
    }
    resp = await client.get("/api/v1/buckets/my-bucket/objects", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["objects"]) == 2
    # FastAPI serializes using aliases (Key, Size)
    assert body["objects"][0]["Key"] == "file1.txt"
    assert body["objects"][0]["Size"] == 100


async def test_list_objects_with_prefix(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    mock_s3_service.list_objects.return_value = {
        "Contents": [],
        "IsTruncated": False,
        "KeyCount": 0,
    }
    resp = await client.get(
        "/api/v1/buckets/my-bucket/objects",
        headers=auth_headers,
        params={"prefix": "logs/"},
    )
    assert resp.status_code == 200
    mock_s3_service.list_objects.assert_called_once_with(
        "my-bucket", "logs/", 1000, None, None
    )


async def test_list_objects_bucket_not_found(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    mock_s3_service.list_objects.side_effect = StorageError(
        "NoSuchBucket", "Not found", 404
    )
    resp = await client.get("/api/v1/buckets/missing/objects", headers=auth_headers)
    assert resp.status_code == 404


async def test_upload_object(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    resp = await client.post(
        "/api/v1/buckets/my-bucket/objects/test.txt",
        headers=auth_headers,
        files={"file": ("test.txt", b"hello world", "text/plain")},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["bucket"] == "my-bucket"
    assert body["key"] == "test.txt"
    assert body["status"] == "uploaded"
    mock_s3_service.put_object.assert_called_once()


async def test_download_object(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    mock_s3_service.get_object.return_value = {
        "Body": MagicMock(iter_chunks=lambda chunk_size=1024: iter([b"file content"])),
        "ContentType": "text/plain",
        "ContentLength": 12,
        "ETag": '"etag123"',
    }
    resp = await client.get(
        "/api/v1/buckets/my-bucket/objects/test.txt", headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.content == b"file content"


async def test_download_object_not_found(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    mock_s3_service.get_object.side_effect = StorageError("NoSuchKey", "Not found", 404)
    resp = await client.get(
        "/api/v1/buckets/my-bucket/objects/missing.txt", headers=auth_headers
    )
    assert resp.status_code == 404


async def test_head_object(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    mock_s3_service.head_object.return_value = {
        "ContentLength": 1024,
        "ContentType": "application/pdf",
        "ETag": '"hash123"',
        "LastModified": datetime(2024, 3, 15, tzinfo=timezone.utc),
    }
    resp = await client.head(
        "/api/v1/buckets/my-bucket/objects/doc.pdf", headers=auth_headers
    )
    assert resp.status_code == 200


async def test_delete_object(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    resp = await client.delete(
        "/api/v1/buckets/my-bucket/objects/test.txt", headers=auth_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "deleted"
    assert body["key"] == "test.txt"


async def test_copy_object(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    resp = await client.post(
        "/api/v1/buckets/dst-bucket/objects/dst-key/copy",
        headers=auth_headers,
        json={"source_bucket": "src-bucket", "source_key": "src-key"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "copied"
    mock_s3_service.copy_object.assert_called_once_with(
        "src-bucket", "src-key", "dst-bucket", "dst-key"
    )


async def test_delete_objects_bulk(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    mock_s3_service.delete_objects.return_value = {"Deleted": [], "Errors": []}
    resp = await client.post(
        "/api/v1/buckets/my-bucket/objects/delete",
        headers=auth_headers,
        json={"keys": ["file1.txt", "file2.txt"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] == 2
    assert body["errors"] == []


async def test_download_objects_bulk(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    """Bulk download starts a task (202), then serves a zip archive (200)."""

    def mock_get_object(bucket, key):
        data = f"content of {key}".encode()
        body = AsyncMock()
        body.read = AsyncMock(return_value=data)
        body.iter_chunks = lambda chunk_size=1024: iter([data])
        return {
            "Body": body,
            "ContentType": "text/plain",
            "ContentLength": len(data),
            "ETag": '"abc"',
        }

    mock_s3_service.get_object.side_effect = mock_get_object
    resp = await client.post(
        "/api/v1/buckets/my-bucket/objects/download",
        headers=auth_headers,
        json={"keys": ["file1.txt", "file2.txt"]},
    )
    assert resp.status_code == 202
    task_id = resp.json()["task_id"]

    # Poll until the background task completes (avoids flaky sleep)
    for _ in range(50):
        resp = await client.get(
            f"/api/v1/buckets/my-bucket/objects/download/{task_id}",
            headers=auth_headers,
        )
        if (
            resp.status_code == 200
            and resp.headers.get("content-type") == "application/zip"
        ):
            break
        await asyncio.sleep(0.02)

    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert sorted(zf.namelist()) == ["file1.txt", "file2.txt"]
    assert zf.read("file1.txt") == b"content of file1.txt"
    assert zf.read("file2.txt") == b"content of file2.txt"


async def test_download_objects_bulk_skips_missing(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    """Bulk download skips objects that fail to fetch."""

    def mock_get_object(bucket, key):
        if key == "missing.txt":
            raise ClientError(
                error_response={
                    "Error": {"Code": "NoSuchKey", "Message": "Not found"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                operation_name="GetObject",
            )
        body = AsyncMock()
        body.read = AsyncMock(return_value=b"data")
        body.iter_chunks = lambda chunk_size=1024: iter([b"data"])
        return {
            "Body": body,
            "ContentType": "text/plain",
            "ContentLength": 4,
            "ETag": '"abc"',
        }

    mock_s3_service.get_object.side_effect = mock_get_object
    resp = await client.post(
        "/api/v1/buckets/my-bucket/objects/download",
        headers=auth_headers,
        json={"keys": ["good.txt", "missing.txt"]},
    )
    assert resp.status_code == 202
    task_id = resp.json()["task_id"]

    # Poll until the background task completes
    for _ in range(50):
        resp = await client.get(
            f"/api/v1/buckets/my-bucket/objects/download/{task_id}",
            headers=auth_headers,
        )
        if (
            resp.status_code == 200
            and resp.headers.get("content-type") == "application/zip"
        ):
            break
        await asyncio.sleep(0.02)

    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    assert zf.namelist() == ["good.txt"]


async def test_download_objects_bulk_empty_keys(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    """Bulk download rejects empty keys list."""
    resp = await client.post(
        "/api/v1/buckets/my-bucket/objects/download",
        headers=auth_headers,
        json={"keys": []},
    )
    assert resp.status_code == 400


async def test_bulk_presign_returns_urls(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    mock_s3_service.generate_presigned_url.side_effect = (
        lambda bucket, key, expires_in, *a: (
            f"https://s3.example.com/{bucket}/{key}?sig=abc"
        )
    )
    resp = await client.post(
        "/api/v1/buckets/my-bucket/objects/presign",
        headers=auth_headers,
        json={"keys": ["file1.txt", "file2.txt"], "expires_in": 7200},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["urls"]) == 2
    assert body["urls"][0]["key"] == "file1.txt"
    assert "s3.example.com" in body["urls"][0]["url"]
    assert body["expires_in"] == 7200


async def test_bulk_presign_skips_failed_keys(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    def mock_presign(bucket, key, expires_in, *a):
        if key == "missing.txt":
            raise ClientError(
                error_response={
                    "Error": {"Code": "NoSuchKey", "Message": "Not found"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                operation_name="GeneratePresignedUrl",
            )
        return f"https://s3.example.com/{bucket}/{key}?sig=abc"

    mock_s3_service.generate_presigned_url.side_effect = mock_presign
    resp = await client.post(
        "/api/v1/buckets/my-bucket/objects/presign",
        headers=auth_headers,
        json={"keys": ["good.txt", "missing.txt"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["urls"]) == 1
    assert body["urls"][0]["key"] == "good.txt"


async def test_bulk_presign_empty_keys(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    resp = await client.post(
        "/api/v1/buckets/my-bucket/objects/presign",
        headers=auth_headers,
        json={"keys": []},
    )
    assert resp.status_code == 422


async def test_get_object_acl(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    mock_s3_service.get_object_acl.return_value = {
        "Owner": {"ID": "owner-123"},
        "Grants": [],
    }
    resp = await client.get(
        "/api/v1/buckets/my-bucket/objects/test.txt/acl", headers=auth_headers
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["owner"]["ID"] == "owner-123"
    assert body["grants"] == []


async def test_put_object_acl(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    resp = await client.put(
        "/api/v1/buckets/my-bucket/objects/test.txt/acl",
        headers=auth_headers,
        json={"Owner": {"ID": "owner"}, "Grants": []},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"


# ── Create folder ─────────────────────────────────────────────────


async def test_create_folder(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    resp = await client.post(
        "/api/v1/buckets/my-bucket/objects/folder",
        headers=auth_headers,
        json={"folder_name": "new-folder"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["bucket"] == "my-bucket"
    assert body["key"] == "new-folder/"
    assert body["status"] == "created"
    mock_s3_service.put_object.assert_called_once()


async def test_create_folder_with_trailing_slash(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    resp = await client.post(
        "/api/v1/buckets/my-bucket/objects/folder",
        headers=auth_headers,
        json={"folder_name": "already-slashed/"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["key"] == "already-slashed/"


async def test_create_folder_nested(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    resp = await client.post(
        "/api/v1/buckets/my-bucket/objects/folder",
        headers=auth_headers,
        json={"folder_name": "parent/child"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["key"] == "parent/child/"


# ── S3 stats ──────────────────────────────────────────────────────────


async def test_s3_stats_starts_background_task_and_poll_returns_result(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    """POST starts counting in background, GET polls until ready."""
    mock_s3_service.list_objects.side_effect = [
        {
            "Contents": [{"Key": f"file{i}.txt"} for i in range(1000)],
            "CommonPrefixes": [{"Prefix": "a/"}, {"Prefix": "b/"}],
            "IsTruncated": True,
            "NextContinuationToken": "token-2",
            "KeyCount": 1000,
        },
        {
            "Contents": [{"Key": f"file{i}.txt"} for i in range(1000, 1500)],
            "CommonPrefixes": [{"Prefix": "c/"}],
            "IsTruncated": False,
            "KeyCount": 500,
        },
    ]

    # Start the count task
    resp = await client.post(
        "/api/v1/buckets/my-bucket/objects/s3_stats?prefix=data/&delimiter=/",
        headers=auth_headers,
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "counting"
    task_id = body["task_id"]

    # Let background task complete
    await asyncio.sleep(0.2)

    # Poll for result
    resp2 = await client.get(
        f"/api/v1/buckets/my-bucket/objects/s3_stats/{task_id}",
        headers=auth_headers,
    )
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert body2["status"] == "ready"
    assert body2["files"] == 1500
    assert body2["folders"] == 3
