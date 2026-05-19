"""Tests for MapiOps — verify correct HTTP methods, paths, and payloads."""

import json

import httpx
import pytest
import respx

from rahcp_client import HCPClient

pytestmark = pytest.mark.asyncio

BASE = "http://test:8000/api/v1"


def _make_client():
    c = HCPClient(endpoint=BASE)
    c._token = "pre-authed"
    return c


@respx.mock
async def test_list_namespaces():
    route = respx.get(f"{BASE}/mapi/tenants/t1/namespaces").mock(
        return_value=httpx.Response(200, json={"name": ["ns1", "ns2"]})
    )
    async with _make_client() as client:
        data = await client.mapi.list_namespaces("t1")
    assert data["name"] == ["ns1", "ns2"]
    assert route.called


@respx.mock
async def test_list_namespaces_verbose():
    route = respx.get(f"{BASE}/mapi/tenants/t1/namespaces").mock(
        return_value=httpx.Response(200, json=[{"name": "ns1"}])
    )
    async with _make_client() as client:
        await client.mapi.list_namespaces("t1", verbose=True)
    assert "verbose=true" in str(route.calls.last.request.url)


@respx.mock
async def test_get_namespace():
    respx.get(f"{BASE}/mapi/tenants/t1/namespaces/ns1").mock(
        return_value=httpx.Response(200, json={"name": "ns1", "hardQuota": "10 GB"})
    )
    async with _make_client() as client:
        data = await client.mapi.get_namespace("t1", "ns1")
    assert data["name"] == "ns1"


@respx.mock
async def test_create_namespace_uses_put():
    route = respx.put(f"{BASE}/mapi/tenants/t1/namespaces").mock(
        return_value=httpx.Response(201, json={"status": "created"})
    )
    async with _make_client() as client:
        await client.mapi.create_namespace(
            "t1", {"name": "new-ns", "hardQuota": "5 GB"}
        )
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["name"] == "new-ns"


@respx.mock
async def test_update_namespace_uses_post():
    route = respx.post(f"{BASE}/mapi/tenants/t1/namespaces/ns1").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    async with _make_client() as client:
        await client.mapi.update_namespace("t1", "ns1", {"hardQuota": "20 GB"})
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body["hardQuota"] == "20 GB"


@respx.mock
async def test_delete_namespace():
    route = respx.delete(f"{BASE}/mapi/tenants/t1/namespaces/ns1").mock(
        return_value=httpx.Response(200, json={"status": "deleted"})
    )
    async with _make_client() as client:
        await client.mapi.delete_namespace("t1", "ns1")
    assert route.called


@respx.mock
async def test_export_namespace():
    respx.get(f"{BASE}/mapi/tenants/t1/namespaces/ns1/export").mock(
        return_value=httpx.Response(
            200, json={"version": "1.0", "namespaces": [{"name": "ns1"}]}
        )
    )
    async with _make_client() as client:
        data = await client.mapi.export_namespace("t1", "ns1")
    assert data["version"] == "1.0"


@respx.mock
async def test_export_namespaces_uses_get_with_query():
    route = respx.get(f"{BASE}/mapi/tenants/t1/namespaces/export").mock(
        return_value=httpx.Response(200, json={"version": "1.0", "namespaces": []})
    )
    async with _make_client() as client:
        await client.mapi.export_namespaces("t1", ["ns1", "ns2"])
    url = str(route.calls.last.request.url)
    assert "names=ns1" in url
