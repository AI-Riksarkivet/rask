"""Tests for app.services.mapi_service.MapiService."""

from __future__ import annotations

import base64
import hashlib

import httpx
import pytest
import respx

from app.core.auth_utils import build_hcp_auth_token, get_hcp_auth_header
from app.core.config import MapiSettings
from app.services.mapi_service import MapiService

HCP_BASE = "https://test.hcp.example.com:9090/mapi"


@pytest.fixture
def settings() -> MapiSettings:
    return MapiSettings(
        hcp_host="test.hcp.example.com",
        hcp_port=9090,
        hcp_username="admin",
        hcp_password="secret",
        hcp_auth_type="hcp",
        hcp_verify_ssl=False,
        hcp_timeout=30,
    )


@pytest.fixture
def service(settings: MapiSettings) -> MapiService:
    return MapiService(settings)


# ── Authentication ──────────────────────────────────────────────────


def test_build_hcp_auth_token():
    token = build_hcp_auth_token("admin", "secret")
    expected_user = base64.b64encode(b"admin").decode()
    expected_pass = hashlib.md5(b"secret").hexdigest()
    assert token == f"{expected_user}:{expected_pass}"


def test_get_auth_header_hcp_type():
    header = get_hcp_auth_header("admin", "secret")
    assert header.startswith("HCP ")


def test_get_auth_header_ad_type():
    header = get_hcp_auth_header("admin", "secret", auth_type="ad")
    assert header == "AD admin:secret"


def test_get_auth_header_custom_credentials():
    header = get_hcp_auth_header(username="other", password="pass", auth_type="ad")
    assert header == "AD other:pass"


# ── URL building ────────────────────────────────────────────────────


def test_build_url_simple_path(service: MapiService):
    url = service._build_url("/tenants")
    assert url == "https://test.hcp.example.com:9090/mapi/tenants"


def test_build_url_with_query(service: MapiService):
    url = service._build_url("/tenants", query={"verbose": "true", "empty": None})
    assert "verbose=true" in url
    assert "empty" not in url


def test_build_url_custom_host(service: MapiService):
    url = service._build_url("/tenants", host="custom.hcp.local")
    assert "custom.hcp.local:9090" in url


# ── HTTP requests (respx-mocked) ───────────────────────────────────


@respx.mock
async def test_request_sends_correct_headers(service: MapiService):
    route = respx.get(f"{HCP_BASE}/tenants").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    resp = await service.get("/tenants", username="admin", password="secret")

    assert resp.status_code == 200
    assert route.called
    request = route.calls.last.request
    assert "authorization" in request.headers
    assert request.headers["accept"] == "application/json"


@respx.mock
async def test_request_with_json_body(service: MapiService):
    route = respx.post(f"{HCP_BASE}/tenants/t1").mock(return_value=httpx.Response(200))
    await service.request(
        "POST", "/tenants/t1", body={"name": "t1"}, username="admin", password="secret"
    )

    request = route.calls.last.request
    assert b"t1" in request.content


@respx.mock
async def test_request_with_raw_body(service: MapiService):
    route = respx.put(f"{HCP_BASE}/path").mock(return_value=httpx.Response(200))
    await service.request(
        "PUT",
        "/path",
        raw_body=b"<xml>data</xml>",
        content_type="application/xml",
        username="admin",
        password="secret",
    )

    request = route.calls.last.request
    assert request.content == b"<xml>data</xml>"


# ── Convenience methods ─────────────────────────────────────────────


@respx.mock
async def test_get_delegates_to_request(service: MapiService):
    route = respx.get(f"{HCP_BASE}/tenants").mock(return_value=httpx.Response(200))
    await service.get("/tenants", username="admin", password="secret")

    assert route.called
    assert route.calls.last.request.method == "GET"


@respx.mock
async def test_post_delegates_to_request(service: MapiService):
    route = respx.post(f"{HCP_BASE}/tenants/t1").mock(return_value=httpx.Response(200))
    await service.post(
        "/tenants/t1", body={"key": "val"}, username="admin", password="secret"
    )

    assert route.called
    assert route.calls.last.request.method == "POST"


@respx.mock
async def test_delete_delegates_to_request(service: MapiService):
    route = respx.delete(f"{HCP_BASE}/tenants/t1").mock(
        return_value=httpx.Response(200)
    )
    await service.delete("/tenants/t1", username="admin", password="secret")

    assert route.called
    assert route.calls.last.request.method == "DELETE"


# ── Client lifecycle ────────────────────────────────────────────────


@respx.mock
async def test_close_closes_client(service: MapiService):
    # Trigger client creation
    respx.get(f"{HCP_BASE}/tenants").mock(return_value=httpx.Response(200))
    await service.get("/tenants", username="admin", password="secret")
    assert service._client is not None
    assert not service._client.is_closed

    await service.close()
    assert service._client.is_closed


async def test_close_noop_when_no_client(service: MapiService):
    await service.close()  # Should not raise


@respx.mock
async def test_close_noop_when_already_closed(service: MapiService):
    # Trigger client creation
    respx.get(f"{HCP_BASE}/tenants").mock(return_value=httpx.Response(200))
    await service.get("/tenants", username="admin", password="secret")
    assert service._client is not None
    await service._client.aclose()
    assert service._client.is_closed

    await service.close()  # Should not raise again
