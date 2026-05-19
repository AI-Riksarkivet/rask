"""Tests for system-level tenant endpoints (list & create)."""

from __future__ import annotations

import httpx
import respx
from httpx import AsyncClient

HCP_BASE = "https://test.hcp.example.com:9090/mapi"


async def test_list_tenants(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{HCP_BASE}/tenants").mock(
        return_value=httpx.Response(200, json={"name": ["tenant-a", "tenant-b"]})
    )
    resp = await client.get("/api/v1/mapi/tenants", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == ["tenant-a", "tenant-b"]


async def test_list_tenants_hcp_error(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{HCP_BASE}/tenants").mock(return_value=httpx.Response(403))
    resp = await client.get("/api/v1/mapi/tenants", headers=auth_headers)
    assert resp.status_code == 403


async def test_create_tenant(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.put(f"{HCP_BASE}/tenants").mock(return_value=httpx.Response(200))
    resp = await client.put(
        "/api/v1/mapi/tenants",
        headers=auth_headers,
        params={"username": "admin", "password": "pass123"},
        json={"name": "new-tenant"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "created"
    assert body["name"] == "new-tenant"
    assert route.called


async def test_create_tenant_conflict(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.put(f"{HCP_BASE}/tenants").mock(return_value=httpx.Response(409))
    resp = await client.put(
        "/api/v1/mapi/tenants",
        headers=auth_headers,
        params={"username": "admin", "password": "pass123"},
        json={"name": "existing-tenant"},
    )
    assert resp.status_code == 409
