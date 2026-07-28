"""ray-kit token-auth unit proofs (gate 7 / R3) — offline.

The live tokenless-rejection proof runs at the cluster gates. Here we pin the
static wiring: with a token env var set the outgoing requests carry the Bearer
credential (asserted on mocked transports), without it they carry nothing (auth
off == previous behavior), and the dashboard proxy never forwards credentials
inbound (browser -> Ray) nor leaks Ray's auth cookie outbound (Ray -> browser).
"""

from typing import ClassVar

import httpx
import pytest

from ray_kit import auth_headers, dashboard


TOKEN = "test-token-123"
_ENV_VARS = ("RASK_RAY_AUTH_TOKEN", "RAY_AUTH_TOKEN")


@pytest.fixture(autouse=True)
def _no_ambient_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip any ambient token so each test states its own env explicitly."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------- auth_headers


def test_auth_headers_empty_when_no_token_env() -> None:
    assert auth_headers() == {}


def test_auth_headers_empty_when_token_env_is_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_RAY_AUTH_TOKEN", "")
    assert auth_headers() == {}


def test_auth_headers_bearer_from_rask_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_RAY_AUTH_TOKEN", TOKEN)
    assert auth_headers() == {"Authorization": f"Bearer {TOKEN}"}


def test_auth_headers_bearer_from_native_ray_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ray's own RAY_AUTH_TOKEN (what the chart injects) works without the RASK_ alias."""
    monkeypatch.setenv("RAY_AUTH_TOKEN", TOKEN)
    assert auth_headers() == {"Authorization": f"Bearer {TOKEN}"}


def test_rask_env_wins_over_native_ray_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAY_AUTH_TOKEN", "native")
    monkeypatch.setenv("RASK_RAY_AUTH_TOKEN", TOKEN)
    assert auth_headers() == {"Authorization": f"Bearer {TOKEN}"}


# ---------------------------------------------------------------- build_client


class _CapturingClient:
    """Stands in for JobSubmissionClient — records ctor kwargs, no network."""

    captured: ClassVar[dict[str, object]] = {}

    def __init__(self, address: str, headers: dict[str, str] | None = None) -> None:
        type(self).captured = {"address": address, "headers": headers}


def test_build_client_sends_bearer_when_token_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_RAY_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(dashboard, "JobSubmissionClient", _CapturingClient)
    assert dashboard.build_client("http://ray:8265") is not None
    assert _CapturingClient.captured["headers"] == {"Authorization": f"Bearer {TOKEN}"}


def test_build_client_sends_no_headers_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashboard, "JobSubmissionClient", _CapturingClient)
    assert dashboard.build_client("http://ray:8265") is not None
    assert _CapturingClient.captured["headers"] is None


# ------------------------------------------------- raw-httpx dashboard surface


def _client_with_capture(seen: list[httpx.Request], response: httpx.Response | None = None) -> httpx.AsyncClient:
    """AsyncClient carrying auth_headers() as defaults over a capturing transport —
    the exact construction the ray service's lifespan uses."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response or httpx.Response(200, json={})

    return httpx.AsyncClient(headers=auth_headers(), transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_dashboard_http_carries_bearer_when_token_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_RAY_AUTH_TOKEN", TOKEN)
    seen: list[httpx.Request] = []
    async with _client_with_capture(seen) as http:
        await dashboard.overview(http, "http://ray:8265")
    assert seen, "no request reached the transport"
    assert all(r.headers.get("authorization") == f"Bearer {TOKEN}" for r in seen)


@pytest.mark.asyncio
async def test_dashboard_http_carries_no_credential_without_token() -> None:
    seen: list[httpx.Request] = []
    async with _client_with_capture(seen) as http:
        await dashboard.overview(http, "http://ray:8265")
    assert seen, "no request reached the transport"
    assert all("authorization" not in r.headers for r in seen)


# ------------------------------------------------------------- proxy hardening


@pytest.mark.asyncio
async def test_proxy_strips_inbound_credentials_and_applies_server_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Browser junk (Authorization / x-ray-authorization / cookie) never reaches Ray;
    with the incoming Authorization stripped, the httpx client's own default Bearer
    token (pod env) is what authenticates the forwarded request."""
    monkeypatch.setenv("RASK_RAY_AUTH_TOKEN", TOKEN)
    seen: list[httpx.Request] = []
    async with _client_with_capture(seen) as http:
        await dashboard.proxy(
            http,
            "http://ray:8265",
            "api/serve/applications/",
            "GET",
            b"",
            {
                "authorization": "Bearer browser-junk",
                "x-ray-authorization": "browser-junk",
                "cookie": "ray-authentication-token=stolen",
                "accept": "application/json",
            },
            b"",
        )
    (request,) = seen
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert "x-ray-authorization" not in request.headers
    assert "cookie" not in request.headers
    assert request.headers["accept"] == "application/json"  # benign headers still forwarded


@pytest.mark.asyncio
async def test_proxy_never_returns_rays_auth_cookie_to_the_browser() -> None:
    """Ray's /api/authenticate sets a ray-authentication-token cookie; the proxy must
    swallow it (and any Set-Cookie) so the token surface never reaches the browser."""
    seen: list[httpx.Request] = []
    upstream = httpx.Response(
        200,
        json={},
        headers={"set-cookie": "ray-authentication-token=secret; Max-Age=2592000", "x-benign": "kept"},
    )
    async with _client_with_capture(seen, upstream) as http:
        resp = await dashboard.proxy(http, "http://ray:8265", "api/serve/applications/", "GET", b"", {}, b"")
    lowered = {k.lower() for k in resp.headers}
    assert "set-cookie" not in lowered
    assert resp.headers.get("x-benign") == "kept"
    assert resp.status_code == 200
