"""Tests for MAPI erasure coding topology endpoints."""

from __future__ import annotations

import httpx
import respx
from httpx import AsyncClient

HCP_BASE = "https://test.hcp.example.com:9090/mapi"
EC = "/api/v1/mapi/services/erasureCoding"
MAPI_EC = f"{HCP_BASE}/services/erasureCoding"
TOPO = "topo-1"


# ── EC Topologies CRUD ───────────────────────────────────────────────


async def test_list_ec_topologies(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_EC}/ecTopologies").mock(
        return_value=httpx.Response(200, json={"ecTopology": [{"name": TOPO}]})
    )
    resp = await client.get(f"{EC}/ecTopologies", headers=auth_headers)
    assert resp.status_code == 200


async def test_create_ec_topology(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.put(f"{MAPI_EC}/ecTopologies").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.put(
        f"{EC}/ecTopologies",
        headers=auth_headers,
        json={
            "name": TOPO,
            "type": "FULLY_CONNECTED",
            "replicationLinks": [{"name": "link1"}],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "created"
    assert resp.json()["name"] == TOPO
    assert route.called


async def test_get_ec_topology(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_EC}/ecTopologies/{TOPO}").mock(
        return_value=httpx.Response(200, json={"name": TOPO, "type": "FULLY_CONNECTED"})
    )
    resp = await client.get(f"{EC}/ecTopologies/{TOPO}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == TOPO


async def test_get_ec_topology_not_found(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_EC}/ecTopologies/missing").mock(
        return_value=httpx.Response(404)
    )
    resp = await client.get(f"{EC}/ecTopologies/missing", headers=auth_headers)
    assert resp.status_code == 404


async def test_check_ec_topology(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.head(f"{MAPI_EC}/ecTopologies/{TOPO}").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.head(f"{EC}/ecTopologies/{TOPO}", headers=auth_headers)
    assert resp.status_code == 200


async def test_retire_ec_topology(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(f"{MAPI_EC}/ecTopologies/{TOPO}").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.post(
        f"{EC}/ecTopologies/{TOPO}",
        headers=auth_headers,
        params={"retire": True},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"
    assert route.called


async def test_delete_ec_topology(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.delete(f"{MAPI_EC}/ecTopologies/{TOPO}").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.delete(f"{EC}/ecTopologies/{TOPO}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    assert route.called


async def test_delete_ec_topology_force(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.delete(f"{MAPI_EC}/ecTopologies/{TOPO}").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.delete(
        f"{EC}/ecTopologies/{TOPO}",
        headers=auth_headers,
        params={"force": True},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    assert route.called


# ── EC Topology Tenants ──────────────────────────────────────────────


async def test_list_ec_topology_tenants(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_EC}/ecTopologies/{TOPO}/tenants").mock(
        return_value=httpx.Response(200, json={"tenant": ["t1"]})
    )
    resp = await client.get(f"{EC}/ecTopologies/{TOPO}/tenants", headers=auth_headers)
    assert resp.status_code == 200


async def test_add_tenant_to_ec_topology(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.route(
        method="PUT", url=f"{MAPI_EC}/ecTopologies/{TOPO}/tenants/t1"
    ).mock(return_value=httpx.Response(200))
    resp = await client.put(
        f"{EC}/ecTopologies/{TOPO}/tenants/t1",
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "created"
    assert resp.json()["tenant"] == "t1"
    assert route.called


async def test_remove_tenant_from_ec_topology(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.delete(f"{MAPI_EC}/ecTopologies/{TOPO}/tenants/t1").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.delete(
        f"{EC}/ecTopologies/{TOPO}/tenants/t1",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    assert resp.json()["tenant"] == "t1"
    assert route.called


# ── EC Link Candidates ──────────────────────────────────────────────


async def test_get_ec_link_candidates(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_EC}/linkCandidates").mock(
        return_value=httpx.Response(200, json={"linkCandidate": []})
    )
    resp = await client.get(f"{EC}/linkCandidates", headers=auth_headers)
    assert resp.status_code == 200


# ── Error propagation ────────────────────────────────────────────────


async def test_ec_hcp_error(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_EC}/ecTopologies").mock(return_value=httpx.Response(403))
    resp = await client.get(f"{EC}/ecTopologies", headers=auth_headers)
    assert resp.status_code == 403
