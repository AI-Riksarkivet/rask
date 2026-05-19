"""Tests for system infrastructure endpoints (statistics, network, licenses)."""

from __future__ import annotations

import httpx
import respx
from httpx import AsyncClient

HCP_BASE = "https://test.hcp.example.com:9090/mapi"
STATS_PREFIX = "/api/v1/mapi"


# ── Node statistics ─────────────────────────────────────────────────


async def test_get_node_statistics(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{HCP_BASE}/nodes/statistics").mock(
        return_value=httpx.Response(
            200,
            json={
                "requestTime": 1706745600000,
                "nodes": [
                    {
                        "nodeNumber": 1,
                        "cpuUser": 12.5,
                        "cpuSystem": 3.2,
                        "openHttpConnections": 42,
                        "openHttpsConnections": 118,
                    },
                    {
                        "nodeNumber": 2,
                        "cpuUser": 8.1,
                        "cpuSystem": 2.0,
                        "openHttpConnections": 30,
                        "openHttpsConnections": 95,
                    },
                ],
            },
        )
    )
    resp = await client.get(f"{STATS_PREFIX}/nodes/statistics", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["requestTime"] == 1706745600000
    assert len(body["nodes"]) == 2
    assert body["nodes"][0]["nodeNumber"] == 1
    assert body["nodes"][0]["cpuUser"] == 12.5
    assert body["nodes"][1]["nodeNumber"] == 2


async def test_get_node_statistics_empty(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{HCP_BASE}/nodes/statistics").mock(
        return_value=httpx.Response(200, json={"nodes": []})
    )
    resp = await client.get(f"{STATS_PREFIX}/nodes/statistics", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["nodes"] == []


async def test_get_node_statistics_hcp_error(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{HCP_BASE}/nodes/statistics").mock(return_value=httpx.Response(403))
    resp = await client.get(f"{STATS_PREFIX}/nodes/statistics", headers=auth_headers)
    assert resp.status_code == 403


# ── Service statistics ──────────────────────────────────────────────


async def test_get_service_statistics(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{HCP_BASE}/services/statistics").mock(
        return_value=httpx.Response(
            200,
            json={
                "requestTime": 1706745600000,
                "services": [
                    {
                        "name": "GarbageCollection",
                        "state": "RUNNING",
                        "performanceLevel": "MEDIUM",
                        "objectsExamined": 50000,
                        "objectsServiced": 120,
                    },
                    {
                        "name": "ContentVerification",
                        "state": "IDLE",
                        "performanceLevel": "LOW",
                        "objectsExamined": 0,
                        "objectsServiced": 0,
                    },
                ],
            },
        )
    )
    resp = await client.get(f"{STATS_PREFIX}/services/statistics", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["requestTime"] == 1706745600000
    assert len(body["services"]) == 2
    assert body["services"][0]["name"] == "GarbageCollection"
    assert body["services"][0]["state"] == "RUNNING"
    assert body["services"][0]["objectsExamined"] == 50000
    assert body["services"][1]["name"] == "ContentVerification"
    assert body["services"][1]["state"] == "IDLE"


async def test_get_service_statistics_empty(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{HCP_BASE}/services/statistics").mock(
        return_value=httpx.Response(200, json={"services": []})
    )
    resp = await client.get(f"{STATS_PREFIX}/services/statistics", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["services"] == []


async def test_get_service_statistics_hcp_error(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{HCP_BASE}/services/statistics").mock(
        return_value=httpx.Response(403)
    )
    resp = await client.get(f"{STATS_PREFIX}/services/statistics", headers=auth_headers)
    assert resp.status_code == 403
