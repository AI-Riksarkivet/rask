"""Tests for system-level group account endpoints."""

from __future__ import annotations

import httpx
import respx
from httpx import AsyncClient

HCP_BASE = "https://test.hcp.example.com:9090/mapi"
S_PREFIX = "/api/v1/mapi/groupAccounts"
S_MAPI = f"{HCP_BASE}/groupAccounts"


async def test_list_system_groups(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{S_MAPI}").mock(
        return_value=httpx.Response(
            200, json={"groupname": ["admins", "monitors", "auditors"]}
        )
    )
    resp = await client.get(S_PREFIX, headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["groupname"] == ["admins", "monitors", "auditors"]
    assert len(body["groupname"]) == 3


async def test_list_system_groups_empty(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{S_MAPI}").mock(
        return_value=httpx.Response(200, json={"groupname": []})
    )
    resp = await client.get(S_PREFIX, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["groupname"] == []


async def test_get_system_group(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{S_MAPI}/admins").mock(
        return_value=httpx.Response(
            200,
            json={
                "groupname": "admins",
                "roles": {"role": ["ADMINISTRATOR"]},
                "allowNamespaceManagement": True,
            },
        )
    )
    resp = await client.get(f"{S_PREFIX}/admins", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["groupname"] == "admins"
    assert body["roles"]["role"] == ["ADMINISTRATOR"]
    assert body["allowNamespaceManagement"] is True


async def test_get_system_group_not_found(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{S_MAPI}/missing").mock(return_value=httpx.Response(404))
    resp = await client.get(f"{S_PREFIX}/missing", headers=auth_headers)
    assert resp.status_code == 404


async def test_check_system_group(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.head(f"{S_MAPI}/admins").mock(return_value=httpx.Response(200))
    resp = await client.head(f"{S_PREFIX}/admins", headers=auth_headers)
    assert resp.status_code == 200


async def test_check_system_group_not_found(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.head(f"{S_MAPI}/missing").mock(return_value=httpx.Response(404))
    resp = await client.head(f"{S_PREFIX}/missing", headers=auth_headers)
    assert resp.status_code == 404


async def test_list_system_groups_hcp_error(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{S_MAPI}").mock(return_value=httpx.Response(403))
    resp = await client.get(S_PREFIX, headers=auth_headers)
    assert resp.status_code == 403
