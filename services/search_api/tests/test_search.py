"""search-api smoke tests — offline (HCP unset → lines_tbl is None), no DB/Ray."""

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient
from lancedb.table import AsyncTable


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("RASK_API_PREFIX", "/api/v1")
    monkeypatch.setenv("RASK_VIEWER_INPUT", str(tmp_path / "in"))
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", str(tmp_path / "out"))
    monkeypatch.delenv("HCP_ENDPOINT", raising=False)
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()

    from search_api import app

    with TestClient(app) as c:
        yield c


def test_search_offline_returns_200_empty(client: TestClient) -> None:
    resp = client.get("/api/v1/search/?q=hej&limit=5")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["count"] == 0
    assert body["hits"] == []


def test_stats_offline_returns_unavailable(client: TestClient) -> None:
    resp = client.get("/api/v1/search/stats")
    assert resp.status_code == 200
    assert resp.json() == {"available": False, "rows": 0}


def test_thumb_without_s3_returns_503(client: TestClient) -> None:
    resp = client.get("/api/v1/search/thumb/thumbs/VOL/0001.jpg")
    assert resp.status_code == 503


def test_health_returns_200(client: TestClient) -> None:
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


class _FakeLinesTbl:
    """Async LanceDB table stand-in: the query() chain returns the seeded rows."""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def query(self) -> "_FakeLinesTbl":
        return self

    def nearest_to_text(self, query: str, columns: str) -> "_FakeLinesTbl":
        return self

    def select(self, cols: list[str]) -> "_FakeLinesTbl":
        return self

    def limit(self, n: int) -> "_FakeLinesTbl":
        return self

    async def to_list(self, timeout: timedelta) -> list[dict[str, object]]:
        return self._rows


@pytest.mark.asyncio
async def test_search_thumb_url_includes_api_prefix() -> None:
    """thumb_url must be built from settings.api_prefix (regression: it once
    emitted /api/search/thumb/... with no /v1 so every line thumbnail 404'd)."""
    from search_api import service as search_service

    tbl = _FakeLinesTbl([{"batch_id": "VOL_A", "text": "hej", "thumb_key": "thumbs/VOL_A/0001.jpg"}])
    resp = await search_service.search_lines(cast(AsyncTable, tbl), "hej", 10, timedelta(seconds=5), api_prefix="/api/v1")
    assert resp.hits[0].thumb_url == "/api/v1/search/thumb/thumbs/VOL_A/0001.jpg"
