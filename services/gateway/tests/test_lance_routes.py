"""The lance-plane gateway rows (lance-ns-merge.md P1 gateway fold, code half).

Proves each new row proxies to the RIGHT upstream with the RIGHT rewritten path —
the lance services serve their own internal prefixes (`/v1/...`, `/api/...`), so a
wrong rewrite silently 404s — and that longest-prefix ordering holds (a
/api/explorer/search request must never hit viewer). Proxy tests swap the gateway's
httpx client for one on a MockTransport: no network, real ASGI path handling.
"""

import importlib
import importlib.util
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi.testclient import TestClient


if TYPE_CHECKING:
    pass


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


@pytest.mark.parametrize(
    ("public", "app_id", "upstream"),
    [
        # catalog/lineage serve /v1/... and /runs at ROOT — the /api/<svc> prefix is stripped whole
        ("/api/catalog/v1/namespace", "catalog", "http://127.0.0.1:2333/v1/namespace"),
        ("/api/lineage/runs", "lineage", "http://127.0.0.1:8000/runs"),
        # the media trio serves /api/... internally — /media is dropped, /api kept
        # `/api/explorer/documents`, NOT `/api/explorer/transcripts`. The viewer serves 33 OpenAPI paths
        # and NONE of them is `/api/transcripts` — there is no transcript route at all. The row was
        # fabricated, and because the MockTransport below answers 200 for any request, the only thing
        # checked was that the rewrite equalled a string this file made up. A rewrite checked
        # only against a string this file wrote cannot tell a route that exists from one that does not.
        ("/api/explorer/documents", "viewer", "http://127.0.0.1:8101/api/documents"),
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
