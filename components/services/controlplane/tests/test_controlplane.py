"""controlplane tests — health skeleton + (later) project listing."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # RASK_API_PREFIX=/api mirrors the deployed fleet; the shared Settings also
    # *requires* viewer in/out, so set dummies even though controlplane ignores them.
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    monkeypatch.setenv("RASK_VIEWER_INPUT", "s3://unused")
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", "s3://unused")

    from controlplane import app

    with TestClient(app) as c:
        yield c


def test_health_returns_ok(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
