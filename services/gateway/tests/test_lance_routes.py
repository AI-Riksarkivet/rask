"""The lance-plane gateway rows (lance-ns-merge.md P1 gateway fold, code half).

Proves each new row proxies to the RIGHT upstream with the RIGHT rewritten path —
the lance services serve their own internal prefixes (`/v1/...`, `/api/...`), so a
wrong rewrite silently 404s — and that longest-prefix ordering holds (a
/api/explorer/search request must never hit viewer). Proxy tests swap the gateway's
httpx client for one on a MockTransport: no network, real ASGI path handling.
"""

import importlib

import httpx
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def gw(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RASK_API_PREFIX", "/api")
    import gateway

    return importlib.reload(gateway)


@pytest.fixture
def proxied(gw):
    """(TestClient, captured upstream requests) with every upstream mocked 200."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        # stream=, not json=/content=: the proxy re-streams via aiter_raw(), and a
        # preloaded body is already marked consumed (StreamConsumed).
        return httpx.Response(200, stream=httpx.ByteStream(b'{"ok": true}'), headers={"content-type": "application/json"})

    with TestClient(gw.app) as client:
        gw.app.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        yield client, captured


def test_lance_rows_present_and_ordered(gw) -> None:
    prefixes = [r[0] for r in gw._routes()]
    # the two deeper media rows outrank /api/explorer
    assert prefixes.index("/api/explorer/search") < prefixes.index("/api/explorer")
    assert prefixes.index("/api/explorer/annotations") < prefixes.index("/api/explorer")
    # the /api catch-all died with core-api (R6/R20): every row is an explicit
    # prefix, an unmatched /api/* 404s at the gateway
    assert "/api" not in prefixes
    for row in ("/api/explorer/search", "/api/explorer/annotations", "/api/explorer", "/api/catalog", "/api/lineage", "/api/produce", "/api/train"):
        assert row in prefixes


@pytest.mark.parametrize(
    ("public", "app_id", "upstream"),
    [
        # catalog/lineage serve /v1/... and /runs at ROOT — the /api/<svc> prefix is stripped whole
        ("/api/catalog/v1/namespace", "catalog", "http://127.0.0.1:2333/v1/namespace"),
        ("/api/lineage/runs", "lineage", "http://127.0.0.1:8000/runs"),
        # the medallion producer serves /produce + /train at root (strip /api only)
        ("/api/produce", "medallion-producer", "http://127.0.0.1:8002/produce"),
        ("/api/train", "medallion-producer", "http://127.0.0.1:8002/train"),
        # the media trio serves /api/... internally — /media is dropped, /api kept
        ("/api/explorer/transcripts", "viewer", "http://127.0.0.1:8101/api/transcripts"),
        ("/api/explorer/search", "search", "http://127.0.0.1:8102/api/search"),
        ("/api/explorer/annotations/doc/sp/ch", "annotator", "http://127.0.0.1:8103/api/annotations/doc/sp/ch"),
    ],
)
def test_lance_row_rewrites(gw, proxied, public: str, app_id: str, upstream: str) -> None:
    client, captured = proxied
    picked = gw._pick_route(public, gw.app.state.routes)
    assert picked is not None and picked[2] == app_id
    resp = client.get(public)
    assert resp.status_code == 200
    assert str(captured[-1].url) == upstream


def test_media_search_does_not_hit_viewer(gw, proxied) -> None:
    """Longest-prefix-first: /api/explorer/search goes to the search service, NOT to
    viewer's /api/explorer catch-all (which would 404 it as /api/search-under-viewer)."""
    client, captured = proxied
    resp = client.get("/api/explorer/search", params={"q": "kyrka", "mode": "fts"})
    assert resp.status_code == 200
    url = captured[-1].url
    assert url.port == 8102
    assert url.path == "/api/search"
    assert url.params["q"] == "kyrka"


def test_media_upstreams_env_overridable(gw, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_EXPLORER_VIEWER_URL", "http://viewer.test:9000")
    monkeypatch.setenv("RASK_CATALOG_API_URL", "http://catalog.test:9001")
    routes = gw._routes()
    assert next(r for r in routes if r[0] == "/api/explorer")[3] == "http://viewer.test:9000"
    assert next(r for r in routes if r[0] == "/api/catalog")[3] == "http://catalog.test:9001"


def test_rask_rows_still_forward_unrewritten(gw, proxied) -> None:
    client, captured = proxied
    resp = client.get("/api/ray/jobs")
    assert resp.status_code == 200
    assert str(captured[-1].url) == "http://127.0.0.1:8804/api/ray/jobs"
