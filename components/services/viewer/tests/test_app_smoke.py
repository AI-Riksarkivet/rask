"""Smoke tests for the viewer FastAPI app — catches signature mismatches at import time."""


def test_create_app_imports_and_constructs(monkeypatch, tmp_path):
    monkeypatch.setenv("RASK_VIEWER_INPUT", str(tmp_path / "in"))
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", str(tmp_path / "out"))
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    from viewer.app import create_app

    app = create_app()
    assert app.title == "viewer"


def test_list_pages_endpoint_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("RASK_VIEWER_INPUT", str(tmp_path / "in"))
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", str(tmp_path / "out"))
    (tmp_path / "in" / "VOL").mkdir(parents=True)
    (tmp_path / "out").mkdir()

    from fastapi.testclient import TestClient

    from viewer.app import create_app

    client = TestClient(create_app())
    resp = client.get("/api/volumes/VOL/pages")
    assert resp.status_code == 200
    assert resp.json() == []
