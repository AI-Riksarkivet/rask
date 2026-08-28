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


def test_serve_proxy_strips_stale_body_headers(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """`ray_kit.dashboard.proxy` returns the httpx-DECODED body but relays Ray's original
    `content-encoding`/`content-length` headers, which describe the compressed body. Forwarding
    them makes the browser re-inflate plaintext or hit a length mismatch — the proxy must drop
    both and let Starlette recompute the length for the decoded bytes."""
    from ray_kit import dashboard
    from ray_kit.schemas import ProxyResponse

    payload = b'{"applications": {}}'

    async def fake_proxy(http, dashboard_url, path, method, query, headers, body):
        return ProxyResponse(
            content=payload,  # already decoded by httpx
            status_code=200,
            headers={
                "content-type": "application/json",
                "content-encoding": "gzip",  # describes the ORIGINAL compressed body, not `payload`
                "content-length": "11",  # the compressed length, not len(payload)
            },
        )

    monkeypatch.setattr(dashboard, "proxy", fake_proxy)

    resp = client.get("/api/serve/applications/")
    assert resp.status_code == 200
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
