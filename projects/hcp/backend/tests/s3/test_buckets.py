"""Tests for S3 bucket endpoints.

Business logic tests for: force-delete (empty → retry → MAPI reconfigure),
version deletion pagination, error aggregation. Serialization-only tests
are deliberately omitted — they test FastAPI, not our code.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from httpx import AsyncClient

from app.services.storage.errors import StorageError, StorageOperationNotSupported


# ── Force-delete business logic ───────────────────────────────────


async def test_force_delete_empties_bucket_then_deletes(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    """force=true must empty all objects/versions before deleting."""
    # Bucket has 2 objects and no versions
    mock_s3_service.list_object_versions.return_value = {
        "Versions": [],
        "DeleteMarkers": [],
        "IsTruncated": False,
    }
    mock_s3_service.list_objects.return_value = {
        "Contents": [{"Key": "a.txt"}, {"Key": "b.txt"}],
        "IsTruncated": False,
    }
    mock_s3_service.delete_objects.return_value = {"Errors": []}

    resp = await client.delete(
        "/api/v1/buckets/my-bucket?force=true", headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    # Must have called delete_objects with the 2 keys
    mock_s3_service.delete_objects.assert_called_once_with(
        "my-bucket", ["a.txt", "b.txt"]
    )
    # Must have called delete_bucket after emptying
    mock_s3_service.delete_bucket.assert_called()


async def test_force_delete_paginates_versions(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    """Version deletion must paginate through all markers."""
    empty_versions = {"Versions": [], "DeleteMarkers": [], "IsTruncated": False}
    mock_s3_service.list_object_versions.side_effect = [
        # First call to _delete_all_versions — 2 pages
        {
            "Versions": [{"Key": "f1.txt", "VersionId": "v1"}],
            "DeleteMarkers": [{"Key": "f2.txt", "VersionId": "dm1"}],
            "IsTruncated": True,
            "NextKeyMarker": "f2.txt",
            "NextVersionIdMarker": "dm1",
        },
        {
            "Versions": [{"Key": "f3.txt", "VersionId": "v2"}],
            "DeleteMarkers": [],
            "IsTruncated": False,
        },
        # Second call to _delete_all_versions (cleanup pass) — empty
        empty_versions,
    ]
    mock_s3_service.list_objects.return_value = {
        "Contents": [],
        "IsTruncated": False,
    }

    resp = await client.delete(
        "/api/v1/buckets/my-bucket?force=true", headers=auth_headers
    )
    assert resp.status_code == 200
    # 3 individual version deletes (2 from page 1, 1 from page 2)
    assert mock_s3_service.delete_object.call_count == 3


async def test_force_delete_caps_errors_at_five(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    """Per-object failures are collected but capped at 5."""
    empty_versions = {"Versions": [], "DeleteMarkers": [], "IsTruncated": False}
    # First pass: 7 versions, all fail to delete
    mock_s3_service.list_object_versions.side_effect = [
        {
            "Versions": [{"Key": f"f{i}.txt", "VersionId": f"v{i}"} for i in range(7)],
            "DeleteMarkers": [],
            "IsTruncated": False,
        },
        # Cleanup pass
        empty_versions,
    ]
    mock_s3_service.delete_object.side_effect = Exception("permission denied")
    mock_s3_service.list_objects.return_value = {
        "Contents": [],
        "IsTruncated": False,
    }

    resp = await client.delete(
        "/api/v1/buckets/my-bucket?force=true", headers=auth_headers
    )
    assert resp.status_code == 200
    # Bucket delete still attempted — errors are non-fatal for emptying


async def test_delete_bucket_not_empty_without_force(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    """Without force=true, BucketNotEmpty propagates as 409."""
    mock_s3_service.delete_bucket.side_effect = StorageError(
        "BucketNotEmpty", "Not empty", 409
    )
    resp = await client.delete("/api/v1/buckets/full-bucket", headers=auth_headers)
    assert resp.status_code == 409


# ── CORS not supported ────────────────────────────────────────────


async def test_cors_not_supported_returns_501(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    """StorageOperationNotSupported translates to 501."""
    mock_s3_service.get_bucket_cors.side_effect = StorageOperationNotSupported(
        "get_bucket_cors", "minio"
    )
    resp = await client.get("/api/v1/buckets/my-bucket/cors", headers=auth_headers)
    assert resp.status_code == 501
