"""Tests for S3Ops — verify correct HTTP methods, paths, and payloads."""

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
async def test_list_buckets():
    route = respx.get(f"{BASE}/buckets").mock(
        return_value=httpx.Response(200, json={"buckets": [{"Name": "b1"}]})
    )
    async with _make_client() as client:
        data = await client.s3.list_buckets()
    assert data["buckets"][0]["Name"] == "b1"
    assert route.calls.last.request.headers["authorization"] == "Bearer pre-authed"


@respx.mock
async def test_list_objects():
    route = respx.get(f"{BASE}/buckets/mybucket/objects").mock(
        return_value=httpx.Response(200, json={"objects": [{"Key": "f.txt"}]})
    )
    async with _make_client() as client:
        data = await client.s3.list_objects("mybucket", "prefix/")
    assert data["objects"][0]["Key"] == "f.txt"
    assert "prefix" in str(route.calls.last.request.url)


@respx.mock
async def test_presign_get():
    route = respx.post(f"{BASE}/presign").mock(
        return_value=httpx.Response(200, json={"url": "https://signed-url"})
    )
    async with _make_client() as client:
        url = await client.s3.presign_get("b", "k")
    assert url == "https://signed-url"
    body = json.loads(route.calls.last.request.content)
    assert body["method"] == "get_object"
    assert body["bucket"] == "b"
    assert body["key"] == "k"
    assert "expires_in" in body


@respx.mock
async def test_presign_put():
    route = respx.post(f"{BASE}/presign").mock(
        return_value=httpx.Response(200, json={"url": "https://upload-url"})
    )
    async with _make_client() as client:
        url = await client.s3.presign_put("b", "k")
    assert url == "https://upload-url"
    body = json.loads(route.calls.last.request.content)
    assert body["method"] == "put_object"


@respx.mock
async def test_presign_bulk():
    respx.post(f"{BASE}/buckets/b/objects/presign").mock(
        return_value=httpx.Response(200, json={"urls": [{"key": "a", "url": "u1"}]})
    )
    async with _make_client() as client:
        urls = await client.s3.presign_bulk("b", ["a"])
    assert urls["a"] == "u1"


@respx.mock
async def test_delete_single():
    route = respx.delete(f"{BASE}/buckets/b/objects/path/to/file.txt").mock(
        return_value=httpx.Response(200, json={"status": "deleted"})
    )
    async with _make_client() as client:
        await client.s3.delete("b", "path/to/file.txt")
    assert route.called


@respx.mock
async def test_delete_bulk():
    route = respx.post(f"{BASE}/buckets/b/objects/delete").mock(
        return_value=httpx.Response(200, json={"deleted": 2})
    )
    async with _make_client() as client:
        await client.s3.delete_bulk("b", ["a.txt", "b.txt"])
    body = json.loads(route.calls.last.request.content)
    assert body["keys"] == ["a.txt", "b.txt"]


@respx.mock
async def test_copy():
    route = respx.post(f"{BASE}/buckets/dest-b/objects/dest-key/copy").mock(
        return_value=httpx.Response(200, json={"status": "copied"})
    )
    async with _make_client() as client:
        await client.s3.copy("dest-b", "dest-key", "src-b", "src-key")
    body = json.loads(route.calls.last.request.content)
    assert body["source_bucket"] == "src-b"
    assert body["source_key"] == "src-key"


@respx.mock
async def test_head():
    respx.head(f"{BASE}/buckets/b/objects/file.txt").mock(
        return_value=httpx.Response(
            200, headers={"content-length": "1234", "etag": '"abc"'}
        )
    )
    async with _make_client() as client:
        meta = await client.s3.head("b", "file.txt")
    assert meta["content-length"] == "1234"
    assert meta["etag"] == '"abc"'
