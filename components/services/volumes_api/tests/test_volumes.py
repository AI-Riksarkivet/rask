"""volumes-api endpoint smoke tests — FS-backed, no DB/Lance/Ray."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("RASK_API_PREFIX", "/api/v1")
    monkeypatch.setenv("RASK_VIEWER_INPUT", str(tmp_path / "in"))
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", str(tmp_path / "out"))
    monkeypatch.delenv("HCP_ENDPOINT", raising=False)
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()

    from volumes_api import app

    with TestClient(app) as c:
        yield c


def test_list_pages_empty_returns_200(client: TestClient) -> None:
    resp = client.get("/api/v1/volumes/VOL/pages")
    assert resp.status_code == 200
    assert resp.json() == []


def test_image_missing_returns_404(client: TestClient) -> None:
    resp = client.get("/api/v1/volumes/VOL/pages/VOL/missing.jpg/image")
    assert resp.status_code == 404


def test_image_path_outside_volume_returns_400(client: TestClient) -> None:
    resp = client.get("/api/v1/volumes/VOL/pages/OTHER/x.jpg/image")
    assert resp.status_code == 400


def test_health_returns_200(client: TestClient) -> None:
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
