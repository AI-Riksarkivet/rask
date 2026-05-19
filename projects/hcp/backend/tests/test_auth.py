"""Tests for the authentication endpoint."""

from __future__ import annotations

from httpx import AsyncClient


async def test_login_with_valid_credentials(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/token",
        data={"username": "testuser", "password": "testpass"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"


async def test_login_accepts_any_credentials(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/token",
        data={"username": "anyuser", "password": "anypass"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"


async def test_login_token_can_access_protected_route(client: AsyncClient):
    # Get a token
    login_resp = await client.post(
        "/api/v1/auth/token",
        data={"username": "testuser", "password": "testpass"},
    )
    token = login_resp.json()["access_token"]

    # Use token to access a protected route
    resp = await client.get(
        "/api/v1/buckets",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


async def test_protected_route_without_token(client: AsyncClient):
    resp = await client.get("/api/v1/buckets")
    assert resp.status_code == 401


async def test_protected_route_with_invalid_token(client: AsyncClient):
    resp = await client.get(
        "/api/v1/buckets",
        headers={"Authorization": "Bearer invalid.token.here"},
    )
    assert resp.status_code == 401


async def test_login_with_tenant(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/token",
        data={"username": "admin", "password": "pass", "tenant": "dev-ai"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    assert body["token_type"] == "bearer"


async def test_login_with_invalid_tenant_name(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/token",
        data={"username": "admin", "password": "pass", "tenant": "-bad-name-"},
    )
    assert resp.status_code == 422


async def test_login_with_tenant_in_username(client: AsyncClient):
    """Swagger Authorize dialog: tenant/username format."""
    import jwt

    resp = await client.post(
        "/api/v1/auth/token",
        data={"username": "acc-ai/admin", "password": "pass"},
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    payload = jwt.decode(token, options={"verify_signature": False})
    assert payload["sub"] == "admin"
    assert payload["tenant"] == "acc-ai"


async def test_login_tenant_field_takes_precedence_over_username_prefix(
    client: AsyncClient,
):
    import jwt

    resp = await client.post(
        "/api/v1/auth/token",
        data={
            "username": "from-username/admin",
            "password": "pass",
            "tenant": "from-field",
        },
    )
    assert resp.status_code == 200
    token = resp.json()["access_token"]
    payload = jwt.decode(token, options={"verify_signature": False})
    assert payload["tenant"] == "from-field"
    # Username should NOT be split when tenant field is provided
    assert payload["sub"] == "from-username/admin"
