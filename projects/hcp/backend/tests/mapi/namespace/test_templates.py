"""Tests for namespace template export endpoints."""

from __future__ import annotations

import httpx
import respx
from httpx import AsyncClient

HCP_BASE = "https://test.hcp.example.com:9090/mapi"
T = "my-tenant"
NS = "my-ns"
PREFIX = f"/api/v1/mapi/tenants/{T}/namespaces"
MAPI_NS = f"{HCP_BASE}/tenants/{T}/namespaces/{NS}"


def _mock_sub_resources(hcp_mock: respx.Router, ns_name: str = NS):
    """Register mock responses for all sub-resources of a namespace."""
    base = f"{HCP_BASE}/tenants/{T}/namespaces/{ns_name}"
    hcp_mock.get(url__startswith=f"{base}?verbose=true").mock(
        return_value=httpx.Response(
            200,
            json={
                "name": ns_name,
                "id": "abc-123",
                "description": "Test NS",
                "hardQuota": "10 GB",
                "softQuota": 85,
                "hashScheme": "SHA-256",
                "searchEnabled": True,
                "creationTime": "2024-01-01",
            },
        )
    )
    hcp_mock.get(f"{base}/versioningSettings").mock(
        return_value=httpx.Response(200, json={"enabled": True, "prune": False})
    )
    hcp_mock.get(f"{base}/complianceSettings").mock(
        return_value=httpx.Response(200, json={"retentionDefault": "30d"})
    )
    hcp_mock.get(f"{base}/permissions").mock(
        return_value=httpx.Response(
            200, json={"readAllowed": True, "writeAllowed": True}
        )
    )
    hcp_mock.get(f"{base}/protocols/http").mock(
        return_value=httpx.Response(200, json={"restEnabled": True})
    )
    hcp_mock.get(f"{base}/protocols/cifs").mock(return_value=httpx.Response(404))
    hcp_mock.get(f"{base}/protocols/nfs").mock(return_value=httpx.Response(404))
    hcp_mock.get(f"{base}/protocols/smtp").mock(return_value=httpx.Response(404))
    hcp_mock.get(f"{base}/customMetadataIndexingSettings").mock(
        return_value=httpx.Response(404)
    )
    hcp_mock.get(f"{base}/cors").mock(return_value=httpx.Response(404))
    hcp_mock.get(f"{base}/replicationCollisionSettings").mock(
        return_value=httpx.Response(404)
    )
    hcp_mock.get(f"{base}/retentionClasses").mock(
        return_value=httpx.Response(200, json={"name": ["rc-30d"]})
    )
    hcp_mock.get(f"{base}/retentionClasses/rc-30d").mock(
        return_value=httpx.Response(200, json={"name": "rc-30d", "value": "30d"})
    )


# ── Single namespace export ──────────────────────────────────────────


async def test_export_single_namespace(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    _mock_sub_resources(hcp_mock)
    resp = await client.get(f"{PREFIX}/{NS}/export", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["version"] == "1.0"
    assert data["sourceTenant"] == T
    assert "exportedAt" in data
    assert len(data["namespaces"]) == 1

    ns = data["namespaces"][0]
    assert ns["name"] == NS
    assert ns["description"] == "Test NS"
    assert ns["hardQuota"] == "10 GB"
    assert ns["hashScheme"] == "SHA-256"


async def test_export_excludes_readonly_fields(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    _mock_sub_resources(hcp_mock)
    resp = await client.get(f"{PREFIX}/{NS}/export", headers=auth_headers)
    ns = resp.json()["namespaces"][0]
    # These read-only fields should be excluded
    assert "id" not in ns
    assert "creationTime" not in ns


async def test_export_includes_sub_resources(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    _mock_sub_resources(hcp_mock)
    resp = await client.get(f"{PREFIX}/{NS}/export", headers=auth_headers)
    ns = resp.json()["namespaces"][0]
    assert ns["versioning"] == {"enabled": True, "prune": False}
    assert ns["compliance"] == {"retentionDefault": "30d"}
    assert ns["permissions"] == {"readAllowed": True, "writeAllowed": True}
    assert ns["protocols"] == {"http": {"restEnabled": True}}
    assert ns["retentionClasses"] == [{"name": "rc-30d", "value": "30d"}]
    # 404 sub-resources should not appear
    assert "indexing" not in ns
    assert "cors" not in ns
    assert "replicationCollision" not in ns


# ── Bulk namespace export ────────────────────────────────────────────


async def test_export_multiple_namespaces(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    for name in ["ns-a", "ns-b"]:
        _mock_sub_resources(hcp_mock, ns_name=name)

    resp = await client.get(f"{PREFIX}/export?names=ns-a,ns-b", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["namespaces"]) == 2
    names = [ns["name"] for ns in data["namespaces"]]
    assert "ns-a" in names
    assert "ns-b" in names


async def test_export_bulk_requires_names_param(
    client: AsyncClient, auth_headers: dict, hcp_mock: respx.Router
):
    resp = await client.get(f"{PREFIX}/export", headers=auth_headers)
    assert resp.status_code == 422  # Missing required query param
