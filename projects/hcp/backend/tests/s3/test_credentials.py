"""Tests for S3 presigned URL and credential derivation endpoints."""

from __future__ import annotations

import base64
import hashlib
from unittest.mock import MagicMock

from httpx import AsyncClient

from app.services.storage.errors import StorageError

from pydantic import SecretStr

from app.core.config import StorageSettings


# ── Presigned URL endpoint ───────────────────────────────────────────


async def test_presign_get_object(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    resp = await client.post(
        "/api/v1/presign",
        headers=auth_headers,
        json={"bucket": "my-bucket", "key": "report.pdf"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "url" in body
    assert body["bucket"] == "my-bucket"
    assert body["key"] == "report.pdf"
    assert body["expires_in"] == 3600
    assert body["method"] == "get_object"
    mock_s3_service.generate_presigned_url.assert_called_once_with(
        "my-bucket", "report.pdf", 3600, "get_object"
    )


async def test_presign_put_object(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    resp = await client.post(
        "/api/v1/presign",
        headers=auth_headers,
        json={
            "bucket": "uploads",
            "key": "data.csv",
            "expires_in": 600,
            "method": "put_object",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["method"] == "put_object"
    assert body["expires_in"] == 600
    mock_s3_service.generate_presigned_url.assert_called_once_with(
        "uploads", "data.csv", 600, "put_object"
    )


async def test_presign_custom_expiry(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    resp = await client.post(
        "/api/v1/presign",
        headers=auth_headers,
        json={"bucket": "b", "key": "k", "expires_in": 86400},
    )
    assert resp.status_code == 200
    assert resp.json()["expires_in"] == 86400


async def test_presign_invalid_method(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    resp = await client.post(
        "/api/v1/presign",
        headers=auth_headers,
        json={"bucket": "b", "key": "k", "method": "delete_object"},
    )
    assert resp.status_code == 422


async def test_presign_expiry_too_large(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    resp = await client.post(
        "/api/v1/presign",
        headers=auth_headers,
        json={"bucket": "b", "key": "k", "expires_in": 999999},
    )
    assert resp.status_code == 422


async def test_presign_s3_error(
    client: AsyncClient, auth_headers: dict, mock_s3_service: MagicMock
):
    mock_s3_service.generate_presigned_url.side_effect = StorageError(
        "NoSuchBucket", "Not found", 404
    )
    resp = await client.post(
        "/api/v1/presign",
        headers=auth_headers,
        json={"bucket": "missing", "key": "k"},
    )
    assert resp.status_code == 404


async def test_presign_requires_auth(client: AsyncClient):
    resp = await client.post(
        "/api/v1/presign",
        json={"bucket": "b", "key": "k"},
    )
    assert resp.status_code == 401


# ── S3 credentials endpoint ─────────────────────────────────────────


async def test_get_credentials(client: AsyncClient, auth_headers: dict):
    resp = await client.get("/api/v1/credentials", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()

    # Verify the derivation is correct for "testuser" / "testpass"
    expected_access_key = base64.b64encode(b"testuser").decode()
    expected_secret_key = hashlib.md5(b"testpass").hexdigest()

    assert body["access_key_id"] == expected_access_key
    assert body["secret_access_key"] == expected_secret_key
    assert body["username"] == "testuser"


async def test_get_credentials_requires_auth(client: AsyncClient):
    resp = await client.get("/api/v1/credentials")
    assert resp.status_code == 401


# ── MinIO/generic credentials ─────────────────────────────────────────


async def test_get_credentials_minio_backend(client: AsyncClient, auth_headers: dict):
    """When storage_backend=minio, /credentials returns configured keys."""
    from app.api.dependencies import get_storage_settings
    from app.main import app

    minio_settings = StorageSettings(
        storage_backend="minio",
        s3_access_key="minioadmin",
        s3_secret_key=SecretStr("minioadmin123"),
        s3_endpoint_url="http://localhost:9000",
    )
    app.dependency_overrides[get_storage_settings] = lambda: minio_settings
    try:
        resp = await client.get("/api/v1/credentials", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["access_key_id"] == "minioadmin"
        assert body["secret_access_key"] == "minioadmin123"
        assert body["endpoint_url"] == "http://localhost:9000"
        assert body["username"] is None
        assert body["tenant"] is None
    finally:
        app.dependency_overrides.pop(get_storage_settings, None)
