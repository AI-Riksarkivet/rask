"""Tests for app.core.middleware.RequestIDMiddleware."""

from __future__ import annotations

from httpx import AsyncClient


async def test_response_includes_request_id(client: AsyncClient, auth_headers: dict):
    """Every response should have an X-Request-ID header."""
    resp = await client.get("/liveness")
    assert "x-request-id" in resp.headers
    assert len(resp.headers["x-request-id"]) > 0


async def test_request_id_is_echoed_back(client: AsyncClient, auth_headers: dict):
    """When X-Request-ID is provided, it should be echoed in the response."""
    custom_id = "test-request-id-12345"
    resp = await client.get(
        "/liveness",
        headers={"X-Request-ID": custom_id},
    )
    assert resp.headers["x-request-id"] == custom_id


async def test_request_id_generated_when_missing(client: AsyncClient):
    """When no X-Request-ID is sent, a UUID is generated."""
    resp = await client.get("/liveness")
    request_id = resp.headers["x-request-id"]
    # UUID hex format: 32 hex characters
    assert len(request_id) == 32
    int(request_id, 16)  # Should be valid hex


async def test_request_id_on_protected_route(client: AsyncClient, auth_headers: dict):
    """Middleware runs on authenticated routes too."""
    resp = await client.get("/api/v1/buckets", headers=auth_headers)
    assert resp.status_code == 200
    assert "x-request-id" in resp.headers


async def test_request_id_on_error_response(client: AsyncClient):
    """Middleware adds request ID even on 401 responses."""
    resp = await client.get("/api/v1/buckets")
    assert resp.status_code == 401
    assert "x-request-id" in resp.headers
