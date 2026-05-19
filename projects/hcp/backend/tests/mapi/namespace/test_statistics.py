"""Tests for namespace statistics and chargeback endpoints."""

from __future__ import annotations

import httpx
import respx
from httpx import AsyncClient

HCP_BASE = "https://test.hcp.example.com:9090/mapi"
T = "my-tenant"
NS = "my-ns"
PREFIX = f"/api/v1/mapi/tenants/{T}/namespaces"
MAPI_PREFIX = f"{HCP_BASE}/tenants/{T}/namespaces"


# ── Namespace statistics ────────────────────────────────────────────


async def test_get_ns_statistics(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_PREFIX}/{NS}/statistics").mock(
        return_value=httpx.Response(
            200,
            json={
                "objectCount": 100,
                "storageCapacityUsed": 536870912,
                "ingestedVolume": 1073741824,
                "customMetadataCount": 25,
                "customMetadataSize": 10240,
                "shredCount": 5,
                "shredSize": 2048,
            },
        )
    )
    resp = await client.get(f"{PREFIX}/{NS}/statistics", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["objectCount"] == 100
    assert body["storageCapacityUsed"] == 536870912
    assert body["ingestedVolume"] == 1073741824
    assert body["customMetadataCount"] == 25
    assert body["shredCount"] == 5


async def test_get_ns_statistics_hcp_error(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_PREFIX}/{NS}/statistics").mock(
        return_value=httpx.Response(403)
    )
    resp = await client.get(f"{PREFIX}/{NS}/statistics", headers=auth_headers)
    assert resp.status_code == 403


async def test_get_ns_statistics_not_found(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_PREFIX}/missing/statistics").mock(
        return_value=httpx.Response(404)
    )
    resp = await client.get(f"{PREFIX}/missing/statistics", headers=auth_headers)
    assert resp.status_code == 404


# ── Namespace chargeback ────────────────────────────────────────────


async def test_get_ns_chargeback(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_PREFIX}/{NS}/chargebackReport").mock(
        return_value=httpx.Response(
            200,
            json={
                "chargebackData": [
                    {
                        "systemName": "hcp-system",
                        "tenantName": T,
                        "namespaceName": NS,
                        "startTime": "2024-01-01T00:00:00Z",
                        "endTime": "2024-01-31T23:59:59Z",
                        "objectCount": 50,
                        "storageCapacityUsed": 268435456,
                        "bytesIn": 512000,
                        "bytesOut": 256000,
                        "reads": 300,
                        "writes": 150,
                        "deletes": 5,
                    }
                ]
            },
        )
    )
    resp = await client.get(f"{PREFIX}/{NS}/chargebackReport", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["chargebackData"]) == 1
    entry = body["chargebackData"][0]
    assert entry["namespaceName"] == NS
    assert entry["objectCount"] == 50
    assert entry["reads"] == 300


async def test_get_ns_chargeback_empty(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_PREFIX}/{NS}/chargebackReport").mock(
        return_value=httpx.Response(200, json={"chargebackData": []})
    )
    resp = await client.get(f"{PREFIX}/{NS}/chargebackReport", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["chargebackData"] == []


async def test_get_ns_chargeback_with_params(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.get(f"{MAPI_PREFIX}/{NS}/chargebackReport").mock(
        return_value=httpx.Response(200, json={"chargebackData": []})
    )
    resp = await client.get(
        f"{PREFIX}/{NS}/chargebackReport",
        headers=auth_headers,
        params={"start": "2024-01-01", "end": "2024-01-31", "granularity": "hour"},
    )
    assert resp.status_code == 200
    assert route.called


async def test_get_ns_chargeback_hcp_error(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_PREFIX}/{NS}/chargebackReport").mock(
        return_value=httpx.Response(403)
    )
    resp = await client.get(f"{PREFIX}/{NS}/chargebackReport", headers=auth_headers)
    assert resp.status_code == 403
