"""Tests for tenant-level settings endpoints."""

from __future__ import annotations

import httpx
import respx
from httpx import AsyncClient

HCP_BASE = "https://test.hcp.example.com:9090/mapi"


async def test_get_tenant(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{HCP_BASE}/tenants/my-tenant").mock(
        return_value=httpx.Response(
            200, json={"name": "my-tenant", "hardQuota": "10GB"}
        )
    )
    resp = await client.get("/api/v1/mapi/tenants/my-tenant", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "my-tenant"


async def test_get_tenant_not_found(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{HCP_BASE}/tenants/missing").mock(return_value=httpx.Response(404))
    resp = await client.get("/api/v1/mapi/tenants/missing", headers=auth_headers)
    assert resp.status_code == 404


async def test_check_tenant_exists(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.head(f"{HCP_BASE}/tenants/my-tenant").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.head("/api/v1/mapi/tenants/my-tenant", headers=auth_headers)
    assert resp.status_code == 200


async def test_modify_tenant(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(f"{HCP_BASE}/tenants/my-tenant").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.post(
        "/api/v1/mapi/tenants/my-tenant",
        headers=auth_headers,
        json={"administrationAllowed": True},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"
    assert route.called


async def test_get_console_security(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{HCP_BASE}/tenants/my-tenant/consoleSecurity").mock(
        return_value=httpx.Response(200, json={"minimumPasswordLength": 8})
    )
    resp = await client.get(
        "/api/v1/mapi/tenants/my-tenant/consoleSecurity", headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["minimumPasswordLength"] == 8


async def test_get_contact_info(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{HCP_BASE}/tenants/my-tenant/contactInfo").mock(
        return_value=httpx.Response(200, json={"firstName": "John"})
    )
    resp = await client.get(
        "/api/v1/mapi/tenants/my-tenant/contactInfo", headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["firstName"] == "John"


async def test_get_tenant_cors(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{HCP_BASE}/tenants/t1/cors").mock(
        return_value=httpx.Response(200, json={"cors": "<CORSRule/>"})
    )
    resp = await client.get("/api/v1/mapi/tenants/t1/cors", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["cors"] == "<CORSRule/>"


async def test_put_tenant_cors(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.put(f"{HCP_BASE}/tenants/t1/cors").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.put(
        "/api/v1/mapi/tenants/t1/cors",
        headers=auth_headers,
        json={"cors": "<CORSRule/>"},
    )
    assert resp.status_code == 201
    assert resp.json()["tenant"] == "t1"
    assert route.called


async def test_delete_tenant_cors(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.delete(f"{HCP_BASE}/tenants/t1/cors").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.delete("/api/v1/mapi/tenants/t1/cors", headers=auth_headers)
    assert resp.status_code == 200
    assert route.called
