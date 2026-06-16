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
