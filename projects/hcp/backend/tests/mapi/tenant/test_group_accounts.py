"""Tests for tenant-level group account endpoints."""

from __future__ import annotations

import httpx
import respx
from httpx import AsyncClient

HCP_BASE = "https://test.hcp.example.com:9090/mapi"
T = "my-tenant"
GRP = "dev-group"
T_PREFIX = f"/api/v1/mapi/tenants/{T}/groupAccounts"
T_MAPI = f"{HCP_BASE}/tenants/{T}/groupAccounts"


async def test_list_group_accounts(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{T_MAPI}").mock(
        return_value=httpx.Response(200, json={"groupAccount": [{"groupname": GRP}]})
    )
    resp = await client.get(T_PREFIX, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["groupAccount"][0]["groupname"] == GRP


async def test_create_group_account(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.put(f"{T_MAPI}").mock(return_value=httpx.Response(200))
    resp = await client.put(
        T_PREFIX,
        headers=auth_headers,
        json={"groupname": GRP},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "created"
    assert body["groupname"] == GRP
    assert route.called


async def test_get_group_account(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{T_MAPI}/{GRP}").mock(
        return_value=httpx.Response(200, json={"groupname": GRP})
    )
    resp = await client.get(f"{T_PREFIX}/{GRP}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["groupname"] == GRP


async def test_get_group_not_found(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{T_MAPI}/missing").mock(return_value=httpx.Response(404))
    resp = await client.get(f"{T_PREFIX}/missing", headers=auth_headers)
    assert resp.status_code == 404


async def test_check_group_account(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.head(f"{T_MAPI}/{GRP}").mock(return_value=httpx.Response(200))
    resp = await client.head(f"{T_PREFIX}/{GRP}", headers=auth_headers)
    assert resp.status_code == 200


async def test_modify_group_account(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(f"{T_MAPI}/{GRP}").mock(return_value=httpx.Response(200))
    resp = await client.post(
        f"{T_PREFIX}/{GRP}",
        headers=auth_headers,
        json={"allowNamespaceManagement": True},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"
    assert route.called


async def test_delete_group_account(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.delete(f"{T_MAPI}/{GRP}").mock(return_value=httpx.Response(200))
    resp = await client.delete(f"{T_PREFIX}/{GRP}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    assert route.called


async def test_get_group_data_perms(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{T_MAPI}/{GRP}/dataAccessPermissions").mock(
        return_value=httpx.Response(200, json={"namespacePermission": []})
    )
    resp = await client.get(
        f"{T_PREFIX}/{GRP}/dataAccessPermissions", headers=auth_headers
    )
    assert resp.status_code == 200


async def test_modify_group_data_perms(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(f"{T_MAPI}/{GRP}/dataAccessPermissions").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.post(
        f"{T_PREFIX}/{GRP}/dataAccessPermissions",
        headers=auth_headers,
        json={"namespacePermission": []},
    )
    assert resp.status_code == 200
    assert route.called
