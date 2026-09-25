"""The lance-plane gateway rows (lance-ns-merge.md P1 gateway fold, code half).

Proves each new row proxies to the RIGHT upstream with the RIGHT rewritten path —
the lance services serve their own internal prefixes (`/v1/...`, `/api/...`), so a
wrong rewrite silently 404s — and that longest-prefix ordering holds (a
/api/explorer/search request must never hit viewer). Proxy tests swap the gateway's
httpx client for one on a MockTransport: no network, real ASGI path handling.
"""

import importlib
from types import ModuleType
from typing import TYPE_CHECKING

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Receive, Scope, Send


if TYPE_CHECKING:
    from gateway import Route


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


# ── The producer's doors, derived from the producer's own OpenAPI ────────────────────────────────────
#
# Every operation the producer serves is either reachable through a gateway row or named below with the
# reason it has none. A new producer route fails here until it gets one or the other, instead of
# answering `404 no upstream` at the edge with nothing saying so.
_PRODUCER_APP_ID = "medallion-producer"

#: Producer operations with no gateway row, and why each has none.
_NOT_PUBLISHED_AT_THE_EDGE: dict[tuple[str, str], str] = {
    ("get", "/livez"): "kubelet probe at the pod root",
    ("get", "/readyz"): "kubelet probe at the pod root",
    ("get", "/dapr/subscribe"): "read by the sidecar to discover the three subscriptions below",
    ("post", "/bronze-arrival"): "pub/sub delivery of the lineage topic, behind require_dapr_token",
    ("post", "/train-trigger"): "pub/sub delivery of the training topic, behind require_dapr_token",
    ("post", "/promotion-held"): "pub/sub delivery of a held promotion, behind require_dapr_token",
    ("post", "/ingest-media"): "synchronous and capped; docs/DECISIONS.md keeps it a service seam, and /api/ingest is the edge's ingest door",
    ("get", "/authorize"): "an admin probe nothing calls; owner ruling 2026-09-25 gives it no row",
}


def _producer_operations() -> set[tuple[str, str]]:
    """(method, path) for every operation the producer's OpenAPI declares."""
    from medallion.core.config import get_settings
    from medallion.producer import app as producer

    operations = {(method, path) for path, item in producer.openapi()["paths"].items() for method in item}
    # The lag cron is an input binding the sidecar delivers to `POST /<binding name>`, mounted only when a
    # deployment names one, so it is excluded by the setting that mounts it.
    binding = get_settings().cascade_lag_binding_name
    return operations - {("post", f"/{binding}")} if binding else operations


def _public_operations() -> set[tuple[str, str]]:
    return {operation for operation in _producer_operations() if operation not in _NOT_PUBLISHED_AT_THE_EDGE}


def _row_reaching(gw: ModuleType, upstream_path: str, routes: list["Route"]) -> "Route | None":
    """The producer row that forwards some public path to ``upstream_path``.

    Decided by the proxy's own `_pick_route`, so a row shadowed by an earlier one reaches nothing, and so
    does a sibling prefix: `/train` + `/` does not start `/trains/...`.
    """
    for route in routes:
        upstream = route.upstream_prefix
        if route.app_id != _PRODUCER_APP_ID or (upstream_path != upstream and not upstream_path.startswith(upstream + "/")):
            continue
        if gw._pick_route(route.public_prefix + upstream_path[len(upstream) :], routes) is route:
            return route
    return None


def test_every_public_producer_operation_has_a_gateway_row(gw) -> None:
    routes = gw._routes()
    unrouted = sorted((method.upper(), path) for method, path in _public_operations() if _row_reaching(gw, path, routes) is None)
    assert not unrouted, (
        f"the producer serves {unrouted} and no gateway row forwards to them, so each answers 404 at the edge; "
        f"give each a row, or name it in _NOT_PUBLISHED_AT_THE_EDGE with the reason it has none"
    )


def test_every_producer_row_reaches_a_producer_operation(gw) -> None:
    routes = gw._routes()
    reached = {_row_reaching(gw, path, routes) for _method, path in _public_operations()}
    dangling = [route.public_prefix for route in routes if route.app_id == _PRODUCER_APP_ID and route not in reached]
    assert not dangling, f"the gateway rows {dangling} reach no producer operation outside _NOT_PUBLISHED_AT_THE_EDGE"


def test_every_exclusion_is_still_a_producer_operation() -> None:
    served = _producer_operations()
    stale = sorted(f"{method.upper()} {path}" for method, path in _NOT_PUBLISHED_AT_THE_EDGE if (method, path) not in served)
    assert not stale, f"the producer no longer serves {stale}; delete those exclusions"


def test_no_gateway_row_publishes_an_exclusion(gw) -> None:
    routes = gw._routes()
    published = sorted(f"{method.upper()} {path}" for method, path in _NOT_PUBLISHED_AT_THE_EDGE if _row_reaching(gw, path, routes) is not None)
    assert not published, f"{published} are named as not published at the edge, and a gateway row publishes them"


def _as_daprd_delivers(app: ASGIApp, *, app_token: str) -> ASGIApp:
    """``app`` behind the stamp daprd adds to an invocation from the gateway: the estate's app token and the
    caller's app-id. The gateway strips both from the client, so these are the only copies that arrive."""

    async def stamped(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope = {**scope, "headers": [*scope["headers"], (b"dapr-api-token", app_token.encode()), (b"dapr-caller-app-id", b"gateway")]}
        await app(scope, receive, send)

    return stamped


@pytest.mark.parametrize(
    ("method", "public"),
    [
        ("get", "/api/trains/train-ray-train-tok-1"),
        ("post", "/api/trains/train-ray-train-tok-1/terminate"),
        ("get", "/api/cascade/stalled"),
    ],
)
def test_a_public_caller_is_refused_by_the_door_behind_the_row(gw, monkeypatch: pytest.MonkeyPatch, method: str, public: str) -> None:
    """The whole forward, as Dapr delivers it: the gateway's own proxy into the producer's own app.

    The stamp carries a VALID app token, so a door that trusted it would serve an anonymous caller from the
    internet. The refusal must come from the door, in its words; a row that reaches no route answers 404.
    """
    from medallion.producer import app as producer

    monkeypatch.setenv("APP_API_TOKEN", "the-estate-app-token")
    with TestClient(gw.app) as client:
        gw.app.state.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=_as_daprd_delivers(producer, app_token="the-estate-app-token")))
        response = client.request(method, public)

    assert response.status_code == 403, response.text
    assert "is a public front door" in response.text, response.text


def test_a_rerun_sent_to_the_gateway_is_answered_by_the_rerun_verb(gw) -> None:
    """The whole forward: the gateway's own proxy carrying the request into the producer's own app.

    Anonymous on purpose. `rerun_stage` refuses an unsigned caller in its body, after routing, in words
    no other producer route uses — so this 403 can only come from the verb. A forward that reached no
    route answers 404, and one that reached a sibling answers in the sibling's words.
    """
    from medallion.producer import app as producer

    with TestClient(gw.app) as client:
        gw.app.state.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=producer))
        response = client.post("/api/stage-runners/stages/rerun", json={"object_id": "table:acme-silver$features", "project": "acme", "to_version": 7})

    assert response.status_code == 403, response.text
    assert "re-running a cascade edge needs a signed-in caller" in response.text, response.text
