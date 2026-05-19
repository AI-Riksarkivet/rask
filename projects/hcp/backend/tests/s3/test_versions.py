"""Tests for S3 object version listing endpoint."""

from __future__ import annotations

from unittest.mock import MagicMock

from httpx import AsyncClient


async def test_list_versions_empty(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    """Listing versions on a bucket with no versions returns empty lists."""
    resp = await client.get("/api/v1/buckets/my-bucket/versions", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["versions"] == []
    assert body["delete_markers"] == []
    assert body["is_truncated"] is False


async def test_list_versions_with_data(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    """Listing versions returns version entries."""
    mock_s3_service.list_object_versions.return_value = {
        "Versions": [
            {
                "Key": "file.txt",
                "VersionId": "v1",
                "IsLatest": True,
                "LastModified": "2024-01-01T00:00:00Z",
                "ETag": '"abc"',
                "Size": 100,
                "StorageClass": "STANDARD",
            },
            {
                "Key": "file.txt",
                "VersionId": "v0",
                "IsLatest": False,
                "LastModified": "2023-12-01T00:00:00Z",
                "ETag": '"def"',
                "Size": 90,
                "StorageClass": "STANDARD",
            },
        ],
        "DeleteMarkers": [
            {
                "Key": "deleted.txt",
                "VersionId": "dm1",
                "IsLatest": True,
                "LastModified": "2024-01-02T00:00:00Z",
            },
        ],
        "IsTruncated": False,
    }
    resp = await client.get("/api/v1/buckets/my-bucket/versions", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["versions"]) == 2
    assert body["versions"][0]["VersionId"] == "v1"
    assert body["versions"][0]["IsLatest"] is True
    assert len(body["delete_markers"]) == 1
    assert body["key_count"] == 3


async def test_list_versions_with_prefix(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    """Prefix parameter is forwarded to the storage layer."""
    resp = await client.get(
        "/api/v1/buckets/my-bucket/versions?prefix=logs/",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    mock_s3_service.list_object_versions.assert_called_once_with(
        "my-bucket", "logs/", 1000, None, None
    )


async def test_download_versioned_object(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    """GET with version_id passes it through to the storage layer."""
    resp = await client.get(
        "/api/v1/buckets/my-bucket/objects/file.txt?version_id=v1",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    mock_s3_service.get_object.assert_called_once_with("my-bucket", "file.txt", "v1")


async def test_delete_versioned_object(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    """DELETE with version_id passes it through to the storage layer."""
    resp = await client.delete(
        "/api/v1/buckets/my-bucket/objects/file.txt?version_id=v1",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    mock_s3_service.delete_object.assert_called_once_with("my-bucket", "file.txt", "v1")
