"""Tests for health and readiness probe endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
from httpx import ASGITransport, AsyncClient

from tests.conftest import HCP_BASE


# ── /liveness ────────────────────────────────────────────────────────


async def test_liveness_returns_ok(client: AsyncClient):
    resp = await client.get("/liveness")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_liveness_does_not_require_auth(client: AsyncClient):
    resp = await client.get("/liveness")
    assert resp.status_code == 200


# ── /readiness ───────────────────────────────────────────────────────


async def test_readiness_ready_when_hcp_reachable(client: AsyncClient, hcp_mock):
    hcp_mock.head(f"{HCP_BASE}/tenants").mock(
        return_value=httpx.Response(200),
    )
    resp = await client.get("/readiness")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ready"
    assert data["checks"]["hcp"] == "reachable"


async def test_readiness_not_ready_when_hcp_unreachable(client: AsyncClient):
    # Mock ping to return False (HCP unreachable)
    transport = client._transport
    assert isinstance(transport, ASGITransport)
    app = transport.app
    with patch.object(
        app.state.mapi,  # type: ignore[union-attr]
        "ping",
        new_callable=AsyncMock,
        return_value=False,
    ):
        resp = await client.get("/readiness")
    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "not ready"
    assert data["checks"]["hcp"] == "unreachable"


async def test_readiness_does_not_require_auth(client: AsyncClient, hcp_mock):
    hcp_mock.head(f"{HCP_BASE}/tenants").mock(
        return_value=httpx.Response(200),
    )
    resp = await client.get("/readiness")
    assert resp.status_code == 200


async def test_readiness_cache_disabled_is_ok(client: AsyncClient, hcp_mock):
    """When no Redis is configured, cache shows disabled and readiness passes."""
    hcp_mock.head(f"{HCP_BASE}/tenants").mock(
        return_value=httpx.Response(200),
    )
    resp = await client.get("/readiness")
    assert resp.status_code == 200
    assert resp.json()["checks"]["cache"] == "disabled"


async def test_readiness_s3_reachable_for_non_hcp_backend(client: AsyncClient):
    """When storage_backend is not HCP and probe succeeds, storage is reachable."""
    transport = client._transport
    assert isinstance(transport, ASGITransport)
    app = transport.app

    probe = AsyncMock()
    probe.list_buckets.return_value = {"Buckets": []}
    app.state.storage_probe = probe  # type: ignore[union-attr]

    with patch("app.main.StorageSettings") as mock_settings_cls:
        mock_settings_cls.return_value.storage_backend = "minio"
        resp = await client.get("/readiness")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ready"
    assert data["checks"]["storage"] == "reachable"

    app.state.storage_probe = None  # type: ignore[union-attr]


async def test_readiness_s3_unreachable_for_non_hcp_backend(client: AsyncClient):
    """When storage_backend is not HCP and probe fails, storage is unreachable."""
    transport = client._transport
    assert isinstance(transport, ASGITransport)
    app = transport.app

    probe = AsyncMock()
    probe.list_buckets.side_effect = ConnectionError("Connection refused")
    app.state.storage_probe = probe  # type: ignore[union-attr]

    with patch("app.main.StorageSettings") as mock_settings_cls:
        mock_settings_cls.return_value.storage_backend = "minio"
        resp = await client.get("/readiness")

    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "not ready"
    assert data["checks"]["storage"] == "unreachable"

    app.state.storage_probe = None  # type: ignore[union-attr]


async def test_readiness_s3_no_probe_configured(client: AsyncClient):
    """When storage_backend is not HCP but no probe exists, storage is unconfigured."""
    transport = client._transport
    assert isinstance(transport, ASGITransport)
    app = transport.app
    app.state.storage_probe = None  # type: ignore[union-attr]

    with patch("app.main.StorageSettings") as mock_settings_cls:
        mock_settings_cls.return_value.storage_backend = "generic"
        resp = await client.get("/readiness")

    assert resp.status_code == 503
    data = resp.json()
    assert data["status"] == "not ready"
    assert data["checks"]["storage"] == "unconfigured"


# ── /health (legacy) ─────────────────────────────────────────────────


async def test_health_returns_ok(client: AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "cache" in data


async def test_health_does_not_require_auth(client: AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200
