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
        # `/api/explorer/documents`, NOT `/api/explorer/transcripts`. The viewer serves 33 OpenAPI paths
        # and NONE of them is `/api/transcripts` — there is no transcript route at all. The row was
        # fabricated, and because the MockTransport below answers 200 for any request, the only thing
        # checked was that the rewrite equalled a string this file made up. That is the exact assertion
        # shape the ingest row passed with while every `/api/ingest/*` call 404'd in production.
        # `test_the_media_rewrites_land_on_paths_the_upstreams_ACTUALLY_serve` now pins these against
        # each service's own openapi.
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


# ── The rewrites, checked against the upstreams rather than against this file ──────────────────────
#
# Every parametrized row above compares the rewritten URL to a literal written by hand, and the
# `proxied` fixture's MockTransport answers 200 for ANY request — so a rewrite landing on a path the
# upstream does not serve passes. It did: `/api/explorer/transcripts` was asserted to rewrite to
# `/api/transcripts`, and the viewer serves 33 paths, none of them that one. This is the assertion
# shape the ingest row already passed with while every `/api/ingest/*` call 404'd in production, so it
# is not a hypothetical failure mode — it is a recurrence.
#
# `services/gateway/tests/test_routing.py` already had the right pattern for the flows row ("checked
# against the flows app's OWN openapi — the ingest lesson, applied to the new row rather than trusted
# not to recur"). It simply was never applied to the lance rows. This is that.
_MEDIA_ROWS = [
    ("/api/explorer/documents", "viewer.main"),
    ("/api/explorer/search", "search.main"),
    ("/api/explorer/annotations/{doc_id}/{speech_id}/{chunk_id}", "annotator.main"),
]


def _served_paths(module_name: str) -> set[str]:
    """The upstream's own OpenAPI paths — the only authority on what it answers."""
    import importlib

    module = importlib.import_module(module_name)
    app = getattr(module, "app", None) or module.create_app()
    return set(app.openapi().get("paths", {}))


def _matches_a_served_path(candidate: str, served: set[str]) -> bool:
    """Template-aware: `/api/annotations/a/b/c` satisfies `/api/annotations/{doc}/{speech}/{chunk}`."""
    if candidate in served:
        return True
    parts = candidate.strip("/").split("/")
    for path in served:
        template = path.strip("/").split("/")
        if len(template) != len(parts):
            continue
        if all(t.startswith("{") or t == p for t, p in zip(template, parts, strict=True)):
            return True
    return False


@pytest.mark.parametrize(("public", "module_name"), _MEDIA_ROWS)
def test_the_media_rewrites_land_on_paths_the_upstreams_ACTUALLY_serve(gw, public: str, module_name: str) -> None:
    served = _served_paths(module_name)
    assert served, f"{module_name} reported no OpenAPI paths — the probe is broken, not the service"

    route = gw._pick_route(public, gw._routes())
    assert route is not None, f"no gateway row matches {public}"
    route_prefix, upstream_prefix = route[0], route[1]
    upstream_path = upstream_prefix + public[len(route_prefix) :]

    assert _matches_a_served_path(upstream_path, served), (
        f"the gateway rewrites {public} to {upstream_path}, which {module_name} does not serve. Its "
        f"{len(served)} paths do not include it, so this row 404s in production while the literal "
        f"comparison above passes — the ingest lesson, recurring."
    )
