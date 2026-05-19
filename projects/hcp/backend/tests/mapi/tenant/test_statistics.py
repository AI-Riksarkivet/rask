"""Tests for tenant-level statistics and chargeback endpoints."""

from __future__ import annotations

import httpx
import respx
from httpx import AsyncClient

HCP_BASE = "https://test.hcp.example.com:9090/mapi"
T = "my-tenant"
PREFIX = f"/api/v1/mapi/tenants/{T}"
MAPI_PREFIX = f"{HCP_BASE}/tenants/{T}"


# ── Tenant statistics ───────────────────────────────────────────────


async def test_get_tenant_statistics(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_PREFIX}/statistics").mock(
        return_value=httpx.Response(
            200,
            json={
                "objectCount": 42,
                "storageCapacityUsed": 1073741824,
                "ingestedVolume": 2147483648,
                "customMetadataCount": 10,
                "customMetadataSize": 5120,
                "shredCount": 0,
                "shredSize": 0,
            },
        )
    )
    resp = await client.get(f"{PREFIX}/statistics", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["objectCount"] == 42
    assert body["storageCapacityUsed"] == 1073741824
    assert body["ingestedVolume"] == 2147483648
    assert body["customMetadataCount"] == 10


async def test_get_tenant_statistics_hcp_error(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_PREFIX}/statistics").mock(return_value=httpx.Response(403))
    resp = await client.get(f"{PREFIX}/statistics", headers=auth_headers)
    assert resp.status_code == 403


async def test_get_tenant_statistics_not_found(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{HCP_BASE}/tenants/missing/statistics").mock(
        return_value=httpx.Response(404)
    )
    resp = await client.get(
        "/api/v1/mapi/tenants/missing/statistics", headers=auth_headers
    )
    assert resp.status_code == 404


# ── Tenant chargeback ───────────────────────────────────────────────


async def test_get_tenant_chargeback(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_PREFIX}/chargebackReport").mock(
        return_value=httpx.Response(
            200,
            json={
                "chargebackData": [
                    {
                        "systemName": "hcp-system",
                        "tenantName": T,
                        "startTime": "2024-01-01T00:00:00Z",
                        "endTime": "2024-01-31T23:59:59Z",
                        "objectCount": 100,
                        "storageCapacityUsed": 5368709120,
                        "bytesIn": 1024000,
                        "bytesOut": 512000,
                        "reads": 500,
                        "writes": 200,
                        "deletes": 10,
                    }
                ]
            },
        )
    )
    resp = await client.get(f"{PREFIX}/chargebackReport", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["chargebackData"]) == 1
    entry = body["chargebackData"][0]
    assert entry["tenantName"] == T
    assert entry["objectCount"] == 100
    assert entry["reads"] == 500
    assert entry["writes"] == 200


async def test_get_tenant_chargeback_empty(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_PREFIX}/chargebackReport").mock(
        return_value=httpx.Response(200, json={"chargebackData": []})
    )
    resp = await client.get(f"{PREFIX}/chargebackReport", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["chargebackData"] == []


async def test_get_tenant_chargeback_with_params(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.get(f"{MAPI_PREFIX}/chargebackReport").mock(
        return_value=httpx.Response(200, json={"chargebackData": []})
    )
    resp = await client.get(
        f"{PREFIX}/chargebackReport",
        headers=auth_headers,
        params={"start": "2024-01-01", "end": "2024-01-31", "granularity": "day"},
    )
    assert resp.status_code == 200
    assert route.called


async def test_get_tenant_chargeback_hcp_error(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_PREFIX}/chargebackReport").mock(
        return_value=httpx.Response(403)
    )
    resp = await client.get(f"{PREFIX}/chargebackReport", headers=auth_headers)
    assert resp.status_code == 403
