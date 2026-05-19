"""Tests for namespace access endpoints (permissions, protocols, CORS)."""

from __future__ import annotations

import httpx
import respx
from httpx import AsyncClient

HCP_BASE = "https://test.hcp.example.com:9090/mapi"
T = "my-tenant"
NS = "my-ns"
PREFIX = f"/api/v1/mapi/tenants/{T}/namespaces"
MAPI_PREFIX = f"{HCP_BASE}/tenants/{T}/namespaces"
NS_PATH = f"{HCP_BASE}/tenants/{T}/namespaces/{NS}"
API_BASE = f"/api/v1/mapi/tenants/{T}/namespaces/{NS}"


# ── Permissions ──────────────────────────────────────────────────────


async def test_get_permissions(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_PREFIX}/{NS}/permissions").mock(
        return_value=httpx.Response(200, json={"namespacePermission": []})
    )
    resp = await client.get(f"{PREFIX}/{NS}/permissions", headers=auth_headers)
    assert resp.status_code == 200


# ── CORS ─────────────────────────────────────────────────────────────


async def test_get_ns_cors(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{MAPI_PREFIX}/{NS}/cors").mock(
        return_value=httpx.Response(200, json={"cors": "<CORSRule/>"})
    )
    resp = await client.get(f"{PREFIX}/{NS}/cors", headers=auth_headers)
    assert resp.status_code == 200


async def test_set_ns_cors(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.put(f"{MAPI_PREFIX}/{NS}/cors").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.put(
        f"{PREFIX}/{NS}/cors",
        headers=auth_headers,
        json={"cors": "<CORSRule/>"},
    )
    assert resp.status_code == 201
    assert resp.json()["namespace"] == NS
    assert route.called


async def test_delete_ns_cors(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.delete(f"{MAPI_PREFIX}/{NS}/cors").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.delete(f"{PREFIX}/{NS}/cors", headers=auth_headers)
    assert resp.status_code == 200
    assert route.called


# ── GET protocol settings ────────────────────────────────────────────


async def test_get_http_protocol(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{NS_PATH}/protocols/http").mock(
        return_value=httpx.Response(
            200,
            json={
                "httpsEnabled": True,
                "httpEnabled": False,
                "restEnabled": True,
                "restRequiresAuthentication": True,
                "hs3Enabled": True,
                "hs3RequiresAuthentication": True,
                "webdavEnabled": False,
            },
        )
    )
    resp = await client.get(f"{API_BASE}/protocols/http", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["httpsEnabled"] is True
    assert body["restEnabled"] is True
    assert body["hs3Enabled"] is True
    assert body["webdavEnabled"] is False


async def test_get_nfs_protocol(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{NS_PATH}/protocols/nfs").mock(
        return_value=httpx.Response(
            200,
            json={
                "enabled": False,
                "uid": 65534,
                "gid": 65534,
            },
        )
    )
    resp = await client.get(f"{API_BASE}/protocols/nfs", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["uid"] == 65534
    assert body["gid"] == 65534


async def test_get_cifs_protocol(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{NS_PATH}/protocols/cifs").mock(
        return_value=httpx.Response(
            200,
            json={
                "enabled": False,
                "caseForcing": "DISABLED",
                "caseSensitive": True,
                "requiresAuthentication": True,
            },
        )
    )
    resp = await client.get(f"{API_BASE}/protocols/cifs", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["caseForcing"] == "DISABLED"


async def test_get_protocol_namespace_not_found(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{NS_PATH}/protocols/http").mock(return_value=httpx.Response(404))
    resp = await client.get(f"{API_BASE}/protocols/http", headers=auth_headers)
    assert resp.status_code == 404


async def test_get_default_protocols_legacy(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.get(f"{NS_PATH}/protocols").mock(
        return_value=httpx.Response(
            200,
            json={
                "httpEnabled": True,
                "httpsEnabled": True,
            },
        )
    )
    resp = await client.get(f"{API_BASE}/protocols", headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["httpEnabled"] is True


# ── REST API setup (HttpProtocol) ────────────────────────────────────


async def test_enable_rest_api(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(f"{NS_PATH}/protocols/http").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.post(
        f"{API_BASE}/protocols/http",
        headers=auth_headers,
        json={
            "restEnabled": True,
            "restRequiresAuthentication": True,
            "httpsEnabled": True,
            "httpEnabled": False,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"
    assert route.called


async def test_disable_rest_api(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.post(f"{NS_PATH}/protocols/http").mock(return_value=httpx.Response(200))
    resp = await client.post(
        f"{API_BASE}/protocols/http",
        headers=auth_headers,
        json={"restEnabled": False},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"


async def test_enable_s3_api(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.post(f"{NS_PATH}/protocols/http").mock(return_value=httpx.Response(200))
    resp = await client.post(
        f"{API_BASE}/protocols/http",
        headers=auth_headers,
        json={
            "hs3Enabled": True,
            "hs3RequiresAuthentication": True,
        },
    )
    assert resp.status_code == 200


async def test_enable_webdav_with_basic_auth(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.post(f"{NS_PATH}/protocols/http").mock(return_value=httpx.Response(200))
    resp = await client.post(
        f"{API_BASE}/protocols/http",
        headers=auth_headers,
        json={
            "webdavEnabled": True,
            "webdavBasicAuthEnabled": True,
            "webdavBasicAuthUsername": "webdavuser",
            "webdavBasicAuthPassword": "webdavpass",
        },
    )
    assert resp.status_code == 200


async def test_configure_http_ip_restrictions(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.post(f"{NS_PATH}/protocols/http").mock(return_value=httpx.Response(200))
    resp = await client.post(
        f"{API_BASE}/protocols/http",
        headers=auth_headers,
        json={
            "ipSettings": {
                "allowAddresses": ["10.0.0.0/8", "192.168.1.0/24"],
                "denyAddresses": ["10.0.0.5"],
                "allowIfInBothLists": False,
            }
        },
    )
    assert resp.status_code == 200


async def test_enable_ad_sso(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.post(f"{NS_PATH}/protocols/http").mock(return_value=httpx.Response(200))
    resp = await client.post(
        f"{API_BASE}/protocols/http",
        headers=auth_headers,
        json={"httpActiveDirectorySSOEnabled": True},
    )
    assert resp.status_code == 200


async def test_http_protocol_forbidden(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.post(f"{NS_PATH}/protocols/http").mock(return_value=httpx.Response(403))
    resp = await client.post(
        f"{API_BASE}/protocols/http",
        headers=auth_headers,
        json={"restEnabled": True},
    )
    assert resp.status_code == 403


# ── NFS setup ────────────────────────────────────────────────────────


async def test_enable_nfs(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(f"{NS_PATH}/protocols/nfs").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.post(
        f"{API_BASE}/protocols/nfs",
        headers=auth_headers,
        json={"enabled": True},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"
    assert route.called


async def test_disable_nfs(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.post(f"{NS_PATH}/protocols/nfs").mock(return_value=httpx.Response(200))
    resp = await client.post(
        f"{API_BASE}/protocols/nfs",
        headers=auth_headers,
        json={"enabled": False},
    )
    assert resp.status_code == 200


async def test_configure_nfs_uid_gid(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.post(f"{NS_PATH}/protocols/nfs").mock(return_value=httpx.Response(200))
    resp = await client.post(
        f"{API_BASE}/protocols/nfs",
        headers=auth_headers,
        json={
            "enabled": True,
            "uid": 1000,
            "gid": 1000,
        },
    )
    assert resp.status_code == 200


async def test_configure_nfs_ip_allow_list(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.post(f"{NS_PATH}/protocols/nfs").mock(return_value=httpx.Response(200))
    resp = await client.post(
        f"{API_BASE}/protocols/nfs",
        headers=auth_headers,
        json={
            "enabled": True,
            "ipSettings": {
                "allowAddresses": ["10.0.0.0/8", "172.16.0.0/12"],
            },
        },
    )
    assert resp.status_code == 200


async def test_nfs_full_configuration(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    """Test enabling NFS with all settings at once."""
    hcp_mock.post(f"{NS_PATH}/protocols/nfs").mock(return_value=httpx.Response(200))
    resp = await client.post(
        f"{API_BASE}/protocols/nfs",
        headers=auth_headers,
        json={
            "enabled": True,
            "uid": 65534,
            "gid": 65534,
            "ipSettings": {
                "allowAddresses": ["10.10.0.0/16"],
            },
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"


async def test_nfs_protocol_namespace_not_found(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.post(f"{NS_PATH}/protocols/nfs").mock(return_value=httpx.Response(404))
    resp = await client.post(
        f"{API_BASE}/protocols/nfs",
        headers=auth_headers,
        json={"enabled": True},
    )
    assert resp.status_code == 404


async def test_nfs_protocol_forbidden(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.post(f"{NS_PATH}/protocols/nfs").mock(return_value=httpx.Response(403))
    resp = await client.post(
        f"{API_BASE}/protocols/nfs",
        headers=auth_headers,
        json={"enabled": True},
    )
    assert resp.status_code == 403


# ── CIFS setup ───────────────────────────────────────────────────────


async def test_enable_cifs(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(f"{NS_PATH}/protocols/cifs").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.post(
        f"{API_BASE}/protocols/cifs",
        headers=auth_headers,
        json={
            "enabled": True,
            "caseForcing": "LOWER",
            "caseSensitive": False,
            "requiresAuthentication": True,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"
    assert route.called


async def test_configure_cifs_ip_restrictions(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.post(f"{NS_PATH}/protocols/cifs").mock(return_value=httpx.Response(200))
    resp = await client.post(
        f"{API_BASE}/protocols/cifs",
        headers=auth_headers,
        json={
            "ipSettings": {
                "allowAddresses": ["192.168.0.0/16"],
                "denyAddresses": ["192.168.1.100"],
                "allowIfInBothLists": True,
            },
        },
    )
    assert resp.status_code == 200


# ── SMTP setup ───────────────────────────────────────────────────────


async def test_enable_smtp(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    route = hcp_mock.post(f"{NS_PATH}/protocols/smtp").mock(
        return_value=httpx.Response(200)
    )
    resp = await client.post(
        f"{API_BASE}/protocols/smtp",
        headers=auth_headers,
        json={
            "enabled": True,
            "emailFormat": ".eml",
            "emailLocation": "/inbox",
            "separateAttachments": True,
        },
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"
    assert route.called


# ── Modify default protocols (legacy) ────────────────────────────────


async def test_modify_default_protocols(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    hcp_mock.post(f"{NS_PATH}/protocols").mock(return_value=httpx.Response(200))
    resp = await client.post(
        f"{API_BASE}/protocols",
        headers=auth_headers,
        json={"httpEnabled": True, "httpsEnabled": True},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"


# ── Protocol with auth required ──────────────────────────────────────


async def test_protocol_endpoint_requires_auth(client: AsyncClient):
    resp = await client.get(f"{API_BASE}/protocols/http")
    assert resp.status_code == 401

    resp = await client.post(f"{API_BASE}/protocols/nfs", json={"enabled": True})
    assert resp.status_code == 401
