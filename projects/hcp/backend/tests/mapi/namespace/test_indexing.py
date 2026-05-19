"""Tests for namespace custom metadata indexing endpoints."""

from __future__ import annotations

import httpx
import respx
from httpx import AsyncClient

HCP_BASE = "https://test.hcp.example.com:9090/mapi"
T = "my-tenant"
NS = "my-ns"
PREFIX = f"/api/v1/mapi/tenants/{T}/namespaces"
MAPI_PREFIX = f"{HCP_BASE}/tenants/{T}/namespaces"


# ── Custom metadata indexing settings ────────────────────────────────


async def test_get_custom_metadata_indexing(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_PREFIX}/{NS}/customMetadataIndexingSettings").mock(
        return_value=httpx.Response(
            200,
            json={
                "fullIndexingEnabled": True,
                "contentClasses": ["doc", "image"],
            },
        )
    )
    resp = await client.get(
        f"{PREFIX}/{NS}/customMetadataIndexingSettings", headers=auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["fullIndexingEnabled"] is True
    assert data["contentClasses"] == ["doc", "image"]


async def test_get_custom_metadata_indexing_not_found(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_PREFIX}/{NS}/customMetadataIndexingSettings").mock(
        return_value=httpx.Response(404)
    )
    resp = await client.get(
        f"{PREFIX}/{NS}/customMetadataIndexingSettings", headers=auth_headers
    )
    assert resp.status_code == 404


async def test_modify_custom_metadata_indexing(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(f"{MAPI_PREFIX}/{NS}/customMetadataIndexingSettings").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.post(
        f"{PREFIX}/{NS}/customMetadataIndexingSettings",
        headers=auth_headers,
        json={"fullIndexingEnabled": False},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"
    assert route.called
