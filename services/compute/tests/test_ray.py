"""compute service smoke tests — offline (Ray dashboard unreachable per conftest)."""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from service_kit import build_settings


@pytest.fixture
def client() -> Iterator[TestClient]:
    from compute import app

    with TestClient(app) as c:
        yield c


def test_health_returns_200(client: TestClient) -> None:
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_ray_health_offline_is_ok_false(client: TestClient) -> None:
    resp = client.get("/api/v1/ray/health")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_ray_jobs_offline_is_ok_false(client: TestClient) -> None:
    resp = client.get("/api/v1/ray/jobs")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_ray_cluster_offline_is_ok_false(client: TestClient) -> None:
    resp = client.get("/api/v1/ray/cluster")
    assert resp.status_code == 200
    assert resp.json()["ok"] is False


def test_serve_proxy_unreachable_returns_502(client: TestClient) -> None:
    resp = client.get("/api/serve/applications/")
    assert resp.status_code == 502


def test_serve_proxy_restores_trailing_slash(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ray's Serve REST API is trailing-slash-strict (`/api/serve/applications/`).
    Intermediaries (the dapr sidecar's service-invoke) normalize the trailing slash
    away, so the proxy must restore it before forwarding to the dashboard — otherwise
    Ray returns 404 and the SPA's /serve view breaks."""
    from ray_kit import dashboard
    from ray_kit.schemas import ProxyResponse

    captured: dict[str, str] = {}

    async def fake_proxy(http, dashboard_url, path, method, query, headers, body):
        captured["path"] = path
        return ProxyResponse(content=b"{}", status_code=200, headers={"content-type": "application/json"})

    monkeypatch.setattr(dashboard, "proxy", fake_proxy)

    # Slash-less path, as it arrives after dapr strips the trailing slash.
    resp = client.get("/api/serve/applications")
    assert resp.status_code == 200
    assert captured["path"] == "api/serve/applications/"


def test_serve_proxy_never_relays_stale_body_headers(client: TestClient) -> None:
    """A gzip-encoded Ray response reaches the browser with headers describing the DECODED body.

    httpx decompresses the dashboard's body but keeps its `content-encoding`/`content-length` (which
    describe the compressed bytes); forwarding them makes the browser re-inflate plaintext or hit a
    length mismatch. The strip lives in ray_kit's `_RESPONSE_STRIP` — the seam that decoded — so this
    fakes at the HTTP layer and drives the REAL `dashboard.proxy`, pinning the whole relay chain."""
    import gzip

    import httpx

    payload = b'{"applications": {}}'
    compressed = gzip.compress(payload)

    def _gzipped(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "content-encoding": "gzip"},
            content=compressed,
        )

    from compute import app

    real_http = app.state.http
    app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(_gzipped))
    try:
        resp = client.get("/api/serve/applications/")
    finally:
        app.state.http = real_http

    assert resp.status_code == 200
    assert resp.content == payload
    assert "content-encoding" not in resp.headers
    assert resp.headers["content-length"] == str(len(payload))


def test_get_ray_client_rebuilds_lazily_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A None client (Ray not yet up at boot) is rebuilt on first use and cached."""
    from compute import dependencies

    # A real app + Request, so app.state and the scope behave exactly as in prod.
    app = FastAPI()
    app.state.settings = build_settings().model_copy(update={"ray_dashboard_url": "http://ray:8265"})
    app.state.ray_client = None
    request = Request({"type": "http", "app": app})

    sentinel = object()
    monkeypatch.setattr(dependencies, "build_client", lambda url: sentinel)
    assert dependencies.get_ray_client(request) is sentinel
    assert app.state.ray_client is sentinel  # cached on app.state

    def _boom(url: str) -> object:
        raise AssertionError("should not rebuild once cached")

    monkeypatch.setattr(dependencies, "build_client", _boom)
    assert dependencies.get_ray_client(request) is sentinel  # served from cache


def test_the_documented_cooldown_env_name_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recorded knob is RASK_COMPUTE_RAY_CLIENT_RETRY_COOLDOWN_S — it must actually bind.

    The field shipped with no alias, so under `Settings`' `env_prefix="RASK_"` the only name that
    bound was the undocumented RASK_RAY_CLIENT_RETRY_COOLDOWN_S; the documented name was a no-op and
    an operator turning the knob changed nothing."""
    from compute.config import ComputeSettings

    monkeypatch.setenv("RASK_COMPUTE_RAY_CLIENT_RETRY_COOLDOWN_S", "3.5")
    assert ComputeSettings().ray_client_retry_cooldown_s == 3.5


def test_get_ray_client_retries_after_the_cooldown_elapses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Self-heal: once the cooldown has elapsed, the next request tries the build again.

    The cooldown is monkeypatched to zero rather than slept through — wall-clock waits are what make
    a suite flaky, and the elapsed/not-elapsed branch is the same either way."""
    from compute import dependencies
    from compute.config import ComputeSettings

    app = FastAPI()
    app.state.settings = build_settings().model_copy(update={"ray_dashboard_url": "http://ray:8265"})
    app.state.ray_client = None
    request = Request({"type": "http", "app": app})

    monkeypatch.setattr(dependencies, "get_compute_settings", lambda: ComputeSettings(ray_client_retry_cooldown_s=0.0))

    calls = 0
    sentinel = object()

    def _flaky(url: str) -> object | None:
        nonlocal calls
        calls += 1
        return None if calls == 1 else sentinel  # down on the first try, up on the second

    monkeypatch.setattr(dependencies, "build_client", _flaky)

    assert dependencies.get_ray_client(request) is None  # Ray down; attempt recorded
    assert dependencies.get_ray_client(request) is sentinel  # cooldown (0s) elapsed -> retried, healed
    assert calls == 2
    assert app.state.ray_client is sentinel  # cached; later requests stop rebuilding


def test_get_ray_client_negative_caches_while_ray_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """A None result (Ray down) must not re-run build_client on every request: each construction
    issues blocking HTTP version-check calls to the dashboard, so a burst of requests while Ray is
    down would storm it. A cooldown collapses the burst to one attempt per interval."""
    from compute import dependencies

    app = FastAPI()
    app.state.settings = build_settings().model_copy(update={"ray_dashboard_url": "http://ray:8265"})
    app.state.ray_client = None
    request = Request({"type": "http", "app": app})

    calls = 0

    def _counting(url: str) -> None:
        nonlocal calls
        calls += 1

    monkeypatch.setattr(dependencies, "build_client", _counting)

    for _ in range(3):
        assert dependencies.get_ray_client(request) is None
    assert calls == 1  # only the first rapid request rebuilt; the cooldown suppressed the rest
