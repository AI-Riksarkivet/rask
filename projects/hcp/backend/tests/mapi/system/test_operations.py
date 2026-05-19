"""Tests for system operations endpoints (health check, logs)."""

from __future__ import annotations

import httpx
import respx
from httpx import AsyncClient

HCP_BASE = "https://test.hcp.example.com:9090/mapi"


# ── Health Check ─────────────────────────────────────────────────────

HC_PREFIX = "/api/v1/mapi/healthCheckReport"
HC_MAPI = f"{HCP_BASE}/healthCheckReport"


async def test_get_health_check_status(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{HC_MAPI}").mock(
        return_value=httpx.Response(200, json={"status": "IDLE"})
    )
    resp = await client.get(HC_PREFIX, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "IDLE"


async def test_prepare_health_check(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(f"{HC_MAPI}/prepare").mock(return_value=httpx.Response(200))
    resp = await client.post(
        f"{HC_PREFIX}/prepare",
        headers=auth_headers,
        json={"collectCurrent": True},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert route.called


async def test_download_health_check(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.post(f"{HC_MAPI}/download").mock(
        return_value=httpx.Response(200, content=b"PK\x03\x04fakezipdata")
    )
    resp = await client.post(f"{HC_PREFIX}/download", headers=auth_headers, json={})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    assert (
        resp.headers["content-disposition"]
        == "attachment; filename=hcp-health-check.zip"
    )


async def test_cancel_health_check(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(f"{HCP_BASE}/healthCheckReport").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.post(f"{HC_PREFIX}/cancel", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert route.called


async def test_health_check_hcp_error(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{HC_MAPI}").mock(return_value=httpx.Response(403))
    resp = await client.get(HC_PREFIX, headers=auth_headers)
    assert resp.status_code == 403


# ── Logs ─────────────────────────────────────────────────────────────

LOG_PREFIX = "/api/v1/mapi/logs"
LOG_MAPI = f"{HCP_BASE}/logs"


async def test_get_log_status(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{LOG_MAPI}").mock(
        return_value=httpx.Response(200, json={"status": "IDLE"})
    )
    resp = await client.get(LOG_PREFIX, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "IDLE"


async def test_log_mark(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(f"{LOG_MAPI}").mock(return_value=httpx.Response(200))
    resp = await client.post(
        LOG_PREFIX, headers=auth_headers, params={"mark": "deploy-v2"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert route.called


async def test_log_cancel(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(f"{LOG_MAPI}").mock(return_value=httpx.Response(200))
    resp = await client.post(LOG_PREFIX, headers=auth_headers, params={"cancel": True})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert route.called


async def test_prepare_logs(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(f"{LOG_MAPI}/prepare").mock(return_value=httpx.Response(200))
    resp = await client.post(
        f"{LOG_PREFIX}/prepare",
        headers=auth_headers,
        json={"startDate": "2024-01-01"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert route.called


async def test_download_logs(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.post(f"{LOG_MAPI}/download").mock(
        return_value=httpx.Response(200, content=b"PK\x03\x04fakezipdata")
    )
    resp = await client.post(f"{LOG_PREFIX}/download", headers=auth_headers, json={})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/octet-stream"
    assert resp.headers["content-disposition"] == "attachment; filename=hcp-logs.zip"


async def test_logs_hcp_error(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{LOG_MAPI}").mock(return_value=httpx.Response(403))
    resp = await client.get(LOG_PREFIX, headers=auth_headers)
    assert resp.status_code == 403
