"""Tests for MAPI replication endpoints (service, certs, links, content, schedule)."""

from __future__ import annotations

import httpx
import respx
from httpx import AsyncClient

HCP_BASE = "https://test.hcp.example.com:9090/mapi"
SVC = "/api/v1/mapi/services/replication"
MAPI_SVC = f"{HCP_BASE}/services/replication"
LINK = "link-1"


# ── Replication Service ──────────────────────────────────────────────


async def test_get_replication_service(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_SVC}").mock(
        return_value=httpx.Response(200, json={"enableDNSFailover": True})
    )
    resp = await client.get(SVC, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["enableDNSFailover"] is True


async def test_modify_replication_service(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(f"{MAPI_SVC}").mock(return_value=httpx.Response(200))
    resp = await client.post(
        SVC,
        headers=auth_headers,
        params={"shutDownAllLinks": "true"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"
    assert route.called


# ── Certificates ─────────────────────────────────────────────────────


async def test_list_certificates(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_SVC}/certificates").mock(
        return_value=httpx.Response(200, json={"certificate": []})
    )
    resp = await client.get(f"{SVC}/certificates", headers=auth_headers)
    assert resp.status_code == 200


async def test_get_certificate(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_SVC}/certificates/cert-1").mock(
        return_value=httpx.Response(200, json={"id": "cert-1", "issuer": "CA"})
    )
    resp = await client.get(f"{SVC}/certificates/cert-1", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == "cert-1"


async def test_delete_certificate(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.delete(f"{MAPI_SVC}/certificates/cert-1").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.delete(f"{SVC}/certificates/cert-1", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    assert resp.json()["certificateId"] == "cert-1"
    assert route.called


async def test_download_server_certificate(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    # Note: /certificates/server is matched by the {certificate_id} route first,
    # so the response goes through parse_json_response rather than returning raw text.
    hcp_mock.get(f"{MAPI_SVC}/certificates/server").mock(
        return_value=httpx.Response(200, json={"certificate": "PEM-DATA"})
    )
    resp = await client.get(f"{SVC}/certificates/server", headers=auth_headers)
    assert resp.status_code == 200


# ── Links ────────────────────────────────────────────────────────────


async def test_list_links(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_SVC}/links").mock(
        return_value=httpx.Response(200, json={"link": [{"name": LINK}]})
    )
    resp = await client.get(f"{SVC}/links", headers=auth_headers)
    assert resp.status_code == 200


async def test_create_link(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.put(f"{MAPI_SVC}/links").mock(return_value=httpx.Response(200))
    resp = await client.put(
        f"{SVC}/links",
        headers=auth_headers,
        json={
            "name": LINK,
            "type": "ACTIVE_ACTIVE",
            "connection": {"remoteHost": "10.0.0.1"},
        },
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "created"
    assert resp.json()["name"] == LINK
    assert route.called


async def test_get_link(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_SVC}/links/{LINK}").mock(
        return_value=httpx.Response(200, json={"name": LINK, "type": "ACTIVE_ACTIVE"})
    )
    resp = await client.get(f"{SVC}/links/{LINK}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == LINK


async def test_get_link_not_found(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_SVC}/links/missing").mock(return_value=httpx.Response(404))
    resp = await client.get(f"{SVC}/links/missing", headers=auth_headers)
    assert resp.status_code == 404


async def test_check_link(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.head(f"{MAPI_SVC}/links/{LINK}").mock(return_value=httpx.Response(200))
    resp = await client.head(f"{SVC}/links/{LINK}", headers=auth_headers)
    assert resp.status_code == 200


async def test_modify_link_suspend(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(f"{MAPI_SVC}/links/{LINK}").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.post(
        f"{SVC}/links/{LINK}",
        headers=auth_headers,
        params={"suspend": True},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"
    assert route.called


async def test_delete_link(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.delete(f"{MAPI_SVC}/links/{LINK}").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.delete(f"{SVC}/links/{LINK}", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    assert resp.json()["name"] == LINK
    assert route.called


# ── Link Content ─────────────────────────────────────────────────────


async def test_get_link_content(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_SVC}/links/{LINK}/content").mock(
        return_value=httpx.Response(200, json={"tenantCount": 2})
    )
    resp = await client.get(f"{SVC}/links/{LINK}/content", headers=auth_headers)
    assert resp.status_code == 200


async def test_list_link_tenants(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_SVC}/links/{LINK}/content/tenants").mock(
        return_value=httpx.Response(200, json={"tenant": ["t1"]})
    )
    resp = await client.get(f"{SVC}/links/{LINK}/content/tenants", headers=auth_headers)
    assert resp.status_code == 200


async def test_add_tenant_to_link(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.route(
        method="PUT", url=f"{MAPI_SVC}/links/{LINK}/content/tenants/t1"
    ).mock(return_value=httpx.Response(200))
    resp = await client.put(
        f"{SVC}/links/{LINK}/content/tenants/t1", headers=auth_headers
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == "created"
    assert resp.json()["tenant"] == "t1"
    assert route.called


async def test_remove_tenant_from_link(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.delete(f"{MAPI_SVC}/links/{LINK}/content/tenants/t1").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.delete(
        f"{SVC}/links/{LINK}/content/tenants/t1", headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    assert resp.json()["tenant"] == "t1"
    assert route.called


# ── Link Schedule ────────────────────────────────────────────────────


async def test_get_link_schedule(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_SVC}/links/{LINK}/schedule").mock(
        return_value=httpx.Response(200, json={"local": {}, "remote": {}})
    )
    resp = await client.get(f"{SVC}/links/{LINK}/schedule", headers=auth_headers)
    assert resp.status_code == 200


async def test_set_link_schedule(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(f"{MAPI_SVC}/links/{LINK}/schedule").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.post(
        f"{SVC}/links/{LINK}/schedule",
        headers=auth_headers,
        json={"local": {}, "remote": {}},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"
    assert route.called


# ── Error propagation ────────────────────────────────────────────────


async def test_replication_hcp_error(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_SVC}").mock(return_value=httpx.Response(403))
    resp = await client.get(SVC, headers=auth_headers)
    assert resp.status_code == 403
