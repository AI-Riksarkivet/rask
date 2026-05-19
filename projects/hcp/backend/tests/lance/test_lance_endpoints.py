"""Tests for Lance dataset explorer endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.dependencies import get_lance_service
from app.main import app
from app.services.cached_lance import CachedLanceService
from app.services.lance_service import LanceError


# ── Fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def mock_lance_service() -> AsyncMock:
    mock = AsyncMock(spec=CachedLanceService)
    mock.list_tables.return_value = ["embeddings", "documents"]
    mock.get_schema.return_value = {
        "table_name": "embeddings",
        "fields": [
            {
                "name": "id",
                "type": "int64",
                "nullable": False,
                "is_vector": False,
                "is_binary": False,
                "vector_dim": None,
            },
            {
                "name": "vector",
                "type": "fixed_size_list<item: float>[512]",
                "nullable": True,
                "is_vector": True,
                "is_binary": False,
                "vector_dim": 512,
            },
            {
                "name": "image",
                "type": "binary",
                "nullable": True,
                "is_vector": False,
                "is_binary": True,
                "vector_dim": None,
            },
        ],
    }
    mock.get_rows.return_value = {
        "rows": [
            {
                "id": 1,
                "vector": {
                    "type": "vector",
                    "dim": 512,
                    "norm": 1.0,
                    "min": -0.05,
                    "max": 0.08,
                    "mean": 0.001,
                    "preview": [0.02],
                },
            },
            {
                "id": 2,
                "vector": {
                    "type": "vector",
                    "dim": 512,
                    "norm": 0.98,
                    "min": -0.04,
                    "max": 0.07,
                    "mean": 0.002,
                    "preview": [0.01],
                },
            },
        ],
        "total": 100,
        "limit": 50,
        "offset": 0,
    }
    mock.get_vector_preview.return_value = {
        "stats": {"count": 2, "dim": 512, "min": -0.05, "max": 0.08, "mean": 0.001},
        "preview": [{"norm": 1.0, "sample": [0.02, -0.01]}],
    }
    mock.get_cell_bytes.return_value = b"raw-cell-data"
    return mock


@pytest.fixture
async def lance_client(mock_lance_service, auth_settings):
    """Test client with mocked LanceService dependency."""

    async def _override():
        yield mock_lance_service

    app.dependency_overrides[get_lance_service] = _override
    app.state.lance_cache = {}
    app.state.cache = None
    app.state.s3_cache = {}

    with patch("app.core.security._get_auth_settings", return_value=auth_settings):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac

    app.dependency_overrides.pop(get_lance_service, None)


# ── List tables ──────────────────────────────────────────────────


async def test_list_tables_returns_table_names(lance_client, auth_headers):
    resp = await lance_client.get(
        "/api/v1/lance/tables?bucket=ml-data",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["tables"] == ["embeddings", "documents"]


async def test_list_tables_requires_bucket(lance_client, auth_headers):
    resp = await lance_client.get("/api/v1/lance/tables", headers=auth_headers)
    assert resp.status_code == 422


# ── Schema ───────────────────────────────────────────────────────


async def test_get_schema_returns_fields(lance_client, auth_headers):
    resp = await lance_client.get(
        "/api/v1/lance/schema?bucket=ml-data&table=embeddings",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["table_name"] == "embeddings"
    assert len(data["fields"]) == 3
    vec_field = next(f for f in data["fields"] if f["is_vector"])
    assert vec_field["vector_dim"] == 512
    img_field = next(f for f in data["fields"] if f["is_binary"])
    assert img_field["name"] == "image"


async def test_get_schema_invalid_table_name(lance_client, auth_headers):
    """Pydantic validates table name pattern — rejects path traversal."""
    resp = await lance_client.get(
        "/api/v1/lance/schema?bucket=ml-data&table=../etc",
        headers=auth_headers,
    )
    assert resp.status_code == 422


async def test_get_schema_corrupted_dataset(
    lance_client, auth_headers, mock_lance_service
):
    mock_lance_service.get_schema.side_effect = LanceError("Cannot open table")
    resp = await lance_client.get(
        "/api/v1/lance/schema?bucket=ml-data&table=broken",
        headers=auth_headers,
    )
    assert resp.status_code == 422


# ── Rows ─────────────────────────────────────────────────────────


async def test_get_rows_paginated(lance_client, auth_headers):
    resp = await lance_client.get(
        "/api/v1/lance/rows?bucket=ml-data&table=embeddings&limit=50&offset=0",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 100
    assert data["limit"] == 50
    assert data["offset"] == 0
    assert len(data["rows"]) == 2


async def test_get_rows_with_columns(lance_client, auth_headers, mock_lance_service):
    resp = await lance_client.get(
        "/api/v1/lance/rows?bucket=ml-data&table=embeddings&columns=id,vector",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    call_args = mock_lance_service.get_rows.call_args
    assert call_args.args[0] == "embeddings"
    assert "id" in call_args.args[3]


@pytest.mark.parametrize("limit", [0, -1, 300])
async def test_get_rows_invalid_limit(lance_client, auth_headers, limit):
    resp = await lance_client.get(
        f"/api/v1/lance/rows?bucket=ml-data&table=embeddings&limit={limit}",
        headers=auth_headers,
    )
    assert resp.status_code == 422


# ── Vector preview ───────────────────────────────────────────────


async def test_vector_preview_returns_stats(lance_client, auth_headers):
    resp = await lance_client.get(
        "/api/v1/lance/vector-preview?bucket=ml-data&table=embeddings&column=vector",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["stats"]["dim"] == 512
    assert len(data["preview"]) == 1


async def test_vector_preview_missing_column(
    lance_client, auth_headers, mock_lance_service
):
    mock_lance_service.get_vector_preview.side_effect = LanceError(
        "Cannot read vector column 'missing'"
    )
    resp = await lance_client.get(
        "/api/v1/lance/vector-preview?bucket=ml-data&table=embeddings&column=missing",
        headers=auth_headers,
    )
    assert resp.status_code == 422


# ── Cell ─────────────────────────────────────────────────────────


async def test_get_cell_returns_bytes(lance_client, auth_headers):
    resp = await lance_client.get(
        "/api/v1/lance/cell?bucket=ml-data&table=embeddings&column=image&row=0",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.content == b"raw-cell-data"
    assert resp.headers["content-type"] == "application/octet-stream"


async def test_get_cell_null_returns_404(
    lance_client, auth_headers, mock_lance_service
):
    mock_lance_service.get_cell_bytes.return_value = None
    resp = await lance_client.get(
        "/api/v1/lance/cell?bucket=ml-data&table=embeddings&column=image&row=99",
        headers=auth_headers,
    )
    assert resp.status_code == 404
