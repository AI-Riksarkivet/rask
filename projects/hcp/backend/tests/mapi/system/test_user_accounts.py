"""Tests for system-level user account endpoints."""

from __future__ import annotations

import httpx
import respx
from httpx import AsyncClient

HCP_BASE = "https://test.hcp.example.com:9090/mapi"
S_PREFIX = "/api/v1/mapi/userAccounts"
S_MAPI = f"{HCP_BASE}/userAccounts"


async def test_list_system_users(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{S_MAPI}").mock(
        return_value=httpx.Response(200, json={"username": ["admin", "monitor"]})
    )
    resp = await client.get(S_PREFIX, headers=auth_headers)
    assert resp.status_code == 200


async def test_get_system_user(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{S_MAPI}/admin").mock(
        return_value=httpx.Response(200, json={"username": "admin"})
    )
    resp = await client.get(f"{S_PREFIX}/admin", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["username"] == "admin"


async def test_check_system_user(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.head(f"{S_MAPI}/admin").mock(return_value=httpx.Response(200))
    resp = await client.head(f"{S_PREFIX}/admin", headers=auth_headers)
    assert resp.status_code == 200


async def test_modify_system_user_password(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(f"{S_MAPI}/admin").mock(return_value=httpx.Response(200))
    resp = await client.post(
        f"{S_PREFIX}/admin",
        headers=auth_headers,
        params={"password": "newpass"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"
    assert route.called
