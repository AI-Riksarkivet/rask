"""ray-api smoke tests — offline (Ray dashboard unreachable per conftest)."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


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


def test_get_ray_client_rebuilds_lazily_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A None client (Ray not yet up at boot) is rebuilt on first use and cached."""
    from ray_api import dependencies

    state = type("State", (), {})()
    state.ray_client = None
    state.settings = type("Settings", (), {"ray_dashboard_url": "http://ray:8265"})()
    request = type("Request", (), {"app": type("App", (), {"state": state})()})()

    sentinel = object()
    monkeypatch.setattr(dependencies, "build_client", lambda url: sentinel)
    assert dependencies.get_ray_client(request) is sentinel
    assert state.ray_client is sentinel  # cached on app.state

    def _boom(url: str) -> object:
        raise AssertionError("should not rebuild once cached")

    monkeypatch.setattr(dependencies, "build_client", _boom)
    assert dependencies.get_ray_client(request) is sentinel  # served from cache
