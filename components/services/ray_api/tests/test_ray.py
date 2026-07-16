"""ray-api smoke tests — offline (Ray dashboard unreachable per conftest)."""

from collections.abc import Iterator
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request


@pytest.fixture
def client() -> Iterator[TestClient]:
    from ray_api import app

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


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_serve_proxy_rejects_write_methods(client: TestClient, method: str) -> None:
    """The Serve proxy is READ-ONLY. Ray's dashboard exposes write verbs on
    `/api/serve/applications/` (e.g. PUT deploys an app with an attacker-chosen
    import_path/runtime_env = RCE on the cluster), and the proxy is unauthenticated,
    so it must forward only GET/HEAD. Write verbs must never reach the dashboard —
    FastAPI answers 405 for a method the route does not declare."""
    resp = client.request(method, "/api/serve/applications/")
    assert resp.status_code == 405


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


def test_get_ray_client_rebuilds_lazily_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A None client (Ray not yet up at boot) is rebuilt on first use and cached."""
    from ray_api import dependencies

    # Ad-hoc stand-ins for starlette's Request/app/State; cast at the get_ray_client
    # boundary where the fake stands in for the real Request[State].
    state = SimpleNamespace(ray_client=None, settings=SimpleNamespace(ray_dashboard_url="http://ray:8265"))
    request = cast(Request, SimpleNamespace(app=SimpleNamespace(state=state)))

    sentinel = object()
    monkeypatch.setattr(dependencies, "build_client", lambda url: sentinel)
    assert dependencies.get_ray_client(request) is sentinel
    assert state.ray_client is sentinel  # cached on app.state

    def _boom(url: str) -> object:
        raise AssertionError("should not rebuild once cached")

    monkeypatch.setattr(dependencies, "build_client", _boom)
    assert dependencies.get_ray_client(request) is sentinel  # served from cache
