"""Tests for MAPI content class endpoints."""

from __future__ import annotations

import httpx
import respx
from httpx import AsyncClient

HCP_BASE = "https://test.hcp.example.com:9090/mapi"
T = "my-tenant"
CC = "my-content-class"
PREFIX = f"/api/v1/mapi/tenants/{T}/contentClasses"
MAPI_PREFIX = f"{HCP_BASE}/tenants/{T}/contentClasses"


async def test_list_content_classes(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_PREFIX}").mock(
        return_value=httpx.Response(200, json={"name": ["cc1", "cc2"]})
    )
    resp = await client.get(PREFIX, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == ["cc1", "cc2"]


async def test_create_content_class(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.put(f"{MAPI_PREFIX}").mock(return_value=httpx.Response(200))
    resp = await client.put(PREFIX, headers=auth_headers, json={"name": CC})
    assert resp.status_code == 201
    assert resp.json()["status"] == "created"
    assert resp.json()["name"] == CC
    assert route.called


async def test_get_content_class(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_PREFIX}/{CC}").mock(
        return_value=httpx.Response(200, json={"name": CC, "contentProperties": []})
    )
    resp = await client.get(f"{PREFIX}/{CC}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == CC


async def test_get_content_class_not_found(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_PREFIX}/missing").mock(return_value=httpx.Response(404))
    resp = await client.get(f"{PREFIX}/missing", headers=auth_headers)
    assert resp.status_code == 404


async def test_check_content_class(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.head(f"{MAPI_PREFIX}/{CC}").mock(return_value=httpx.Response(200))
    resp = await client.head(f"{PREFIX}/{CC}", headers=auth_headers)
    assert resp.status_code == 200


async def test_update_content_class(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(f"{MAPI_PREFIX}/{CC}").mock(return_value=httpx.Response(200))
    resp = await client.post(
        f"{PREFIX}/{CC}",
        headers=auth_headers,
        json={"contentProperties": []},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"
    assert route.called


async def test_delete_content_class(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.delete(f"{MAPI_PREFIX}/{CC}").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.delete(f"{PREFIX}/{CC}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    assert route.called
