"""Tests for HCPClient — auth, retry, error mapping."""

import httpx
import pytest
import respx

from rahcp_client import HCPClient
from rahcp_client.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    UpstreamError,
)

pytestmark = pytest.mark.asyncio

BASE = "http://test:8000/api/v1"


@pytest.fixture
def client():
    return HCPClient(
        endpoint=BASE,
        username="user",
        password="pass",
        tenant="test-tenant",
        max_retries=1,
        retry_base_delay=0.01,
    )


# ── Auth ────────────────────────────────────────────────────────────


@respx.mock
async def test_login_sends_tenant_in_form_data(client):
    route = respx.post(f"{BASE}/auth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok123"})
    )
    async with client:
        pass  # __aenter__ triggers login

    assert route.called
    req = route.calls.last.request
    body = req.content.decode()
    assert "username=user" in body
    assert "password=pass" in body
    assert "tenant=test-tenant" in body


@respx.mock
async def test_login_failure_raises_auth_error(client):
    respx.post(f"{BASE}/auth/token").mock(
        return_value=httpx.Response(401, json={"detail": "bad creds"})
    )
    with pytest.raises(AuthenticationError, match="Login failed"):
        async with client:
            pass


@respx.mock
async def test_auto_refresh_on_401(client):
    # First login
    respx.post(f"{BASE}/auth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok123"})
    )
    # First request gets 401, then refresh + retry succeeds
    respx.get(f"{BASE}/buckets").mock(
        side_effect=[
            httpx.Response(401, json={"detail": "expired"}),
            httpx.Response(200, json={"buckets": []}),
        ]
    )
    async with client:
        resp = await client.request("GET", "/buckets")
        assert resp.status_code == 200


# ── Error mapping ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,error_cls",
    [
        (401, AuthenticationError),
        (403, AuthenticationError),
        (404, NotFoundError),
        (409, ConflictError),
        (502, UpstreamError),
    ],
)
@respx.mock
async def test_error_mapping(client, status, error_cls):
    respx.post(f"{BASE}/auth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok"})
    )
    respx.get(f"{BASE}/test").mock(return_value=httpx.Response(status, text="error"))
    async with client:
        with pytest.raises(error_cls):
            await client.request("GET", "/test")


@respx.mock
async def test_502_does_not_retry():
    c = HCPClient(endpoint=BASE, max_retries=3, retry_base_delay=0.01)
    c._token = "pre-authed"  # skip login
    route = respx.get(f"{BASE}/test").mock(
        return_value=httpx.Response(502, text="upstream down")
    )
    async with c:
        with pytest.raises(UpstreamError):
            await c.request("GET", "/test")
    # Should only be called once — no retry on 502
    assert route.call_count == 1


@respx.mock
async def test_retryable_errors_are_retried(client):
    respx.post(f"{BASE}/auth/token").mock(
        return_value=httpx.Response(200, json={"access_token": "tok"})
    )
    route = respx.get(f"{BASE}/test").mock(
        side_effect=[
            httpx.Response(503, text="unavailable"),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    async with client:
        resp = await client.request("GET", "/test")
        assert resp.status_code == 200
    assert route.call_count == 2
