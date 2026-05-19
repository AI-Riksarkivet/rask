"""Tests for namespace compliance and retention class endpoints."""

from __future__ import annotations

import httpx
import respx
from httpx import AsyncClient

HCP_BASE = "https://test.hcp.example.com:9090/mapi"
T = "my-tenant"
NS = "my-ns"
PREFIX = f"/api/v1/mapi/tenants/{T}/namespaces"
MAPI_PREFIX = f"{HCP_BASE}/tenants/{T}/namespaces"

RC = "my-retention-class"
RC_PREFIX = f"/api/v1/mapi/tenants/{T}/namespaces/{NS}/retentionClasses"
RC_MAPI = f"{HCP_BASE}/tenants/{T}/namespaces/{NS}/retentionClasses"


# ── Compliance settings ──────────────────────────────────────────────


async def test_get_compliance(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_PREFIX}/{NS}/complianceSettings").mock(
        return_value=httpx.Response(200, json={"retentionDefault": "0"})
    )
    resp = await client.get(f"{PREFIX}/{NS}/complianceSettings", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["retentionDefault"] == "0"


async def test_modify_compliance(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(f"{MAPI_PREFIX}/{NS}/complianceSettings").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.post(
        f"{PREFIX}/{NS}/complianceSettings",
        headers=auth_headers,
        json={"retentionDefault": "30d"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"
    assert route.called


# ── Retention classes ────────────────────────────────────────────────


async def test_list_retention_classes(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{RC_MAPI}").mock(
        return_value=httpx.Response(200, json={"name": ["rc1", "rc2"]})
    )
    resp = await client.get(RC_PREFIX, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == ["rc1", "rc2"]


async def test_create_retention_class(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.put(f"{RC_MAPI}").mock(return_value=httpx.Response(200))
    resp = await client.put(
        RC_PREFIX,
        headers=auth_headers,
        json={"name": RC, "value": "30d"},
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "created"
    assert resp.json()["name"] == RC
    assert route.called


async def test_get_retention_class(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{RC_MAPI}/{RC}").mock(
        return_value=httpx.Response(200, json={"name": RC, "value": "30d"})
    )
    resp = await client.get(f"{RC_PREFIX}/{RC}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == RC


async def test_get_retention_class_not_found(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{RC_MAPI}/missing").mock(return_value=httpx.Response(404))
    resp = await client.get(f"{RC_PREFIX}/missing", headers=auth_headers)
    assert resp.status_code == 404


async def test_check_retention_class(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.head(f"{RC_MAPI}/{RC}").mock(return_value=httpx.Response(200))
    resp = await client.head(f"{RC_PREFIX}/{RC}", headers=auth_headers)
    assert resp.status_code == 200


async def test_update_retention_class(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(f"{RC_MAPI}/{RC}").mock(return_value=httpx.Response(200))
    resp = await client.post(
        f"{RC_PREFIX}/{RC}",
        headers=auth_headers,
        json={"value": "60d"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"
    assert route.called


async def test_delete_retention_class(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.delete(f"{RC_MAPI}/{RC}").mock(return_value=httpx.Response(200))
    resp = await client.delete(f"{RC_PREFIX}/{RC}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    assert route.called
