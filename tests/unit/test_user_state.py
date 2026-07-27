"""Per-subject user state on the Dapr state store (``service_kit/governed/user_state.py``, ``/v1/user-state/*``).

Three things are being pinned, and only the first is ordinary:

1. **A real round trip.** The store client talks HTTP to a stand-in sidecar that models the parts of
   Dapr's state API which actually constrain us — the ``<appId>||<key>`` prefix it adds, the ``204`` it
   answers a missing key with, and the ``400`` it answers a key containing ``||`` with. Requests go over
   ``httpx.ASGITransport``, so the URL building, JSON shaping and status handling under test are the real
   ones; only the socket is not.

2. **User A cannot read user B's state.** This is the test that decides whether the design is right. It
   is driven through the full catalog app with OIDC on, two different bearer tokens, and NO other
   difference between the two calls — no header, no path segment, no body field names either user. A
   store keyed on anything the caller supplies passes a naive "write then read" test and fails this one.

3. **The key can never carry ``||``.** Dapr reserves it as the app-id separator, and the naive fix
   (percent-encoding) does not work because the key rides in the sidecar's URL path and is decoded on
   arrival. The stand-in decodes exactly like the real one, so the test would catch that regression.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from catalog.api.v1.endpoints import user_state as ep
from catalog.core.config import Settings, get_settings
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient
from lance_namespace import ServiceUnavailableError

from service_kit.governed.oidc import IDToken
from service_kit.governed.user_state import (
    DAPR_APP_ID_SEPARATOR,
    StoredState,
    UserStateDocument,
    UserStateStore,
    UserStateUnreadable,
    decode_subject,
    encode_subject,
    state_key,
)
from service_kit.schemas.workflow import SavedView, SearchSpec, WorkflowGraph, WorkflowSearchMode


# ── a stand-in for the local Dapr sidecar's state API ────────────────────────────────────────────────


class FakeSidecar:
    """The subset of ``/v1.0/state/{store}`` our client depends on, with Dapr's real constraints.

    Deliberately models three behaviours rather than being a dict with a URL: the app-id key prefix (so a
    test can assert what the STORE actually sees), ``204`` for a missing key (not ``404`` — reading that
    wrong would turn a first visit into an error), and ``400`` for a key containing ``||``. Starlette
    decodes the path param exactly as the sidecar's router does, which is what makes the ``||`` case a
    real test and not a decorative one.
    """

    def __init__(self, *, app_id: str = "catalog", store: str = "lance-statestore") -> None:
        self.app_id = app_id
        self.store = store
        self.rows: dict[str, Any] = {}
        self.fail_next_with: int | None = None
        self.app = FastAPI()
        self.app.post("/v1.0/state/{store}")(self._save)
        self.app.get("/v1.0/state/{store}/{key:path}")(self._get)
        self.app.delete("/v1.0/state/{store}/{key:path}")(self._delete)

    def stored_key(self, key: str) -> str:
        """The key as the state store sees it — Dapr prefixes the app-id with its reserved separator."""
        return f"{self.app_id}{DAPR_APP_ID_SEPARATOR}{key}"

    def _guard(self, store: str, key: str) -> Response | None:
        if self.fail_next_with is not None:
            status, self.fail_next_with = self.fail_next_with, None
            return Response(status_code=status, content=b'{"errorCode":"ERR_STATE_GET"}')
        if store != self.store:
            return Response(status_code=400, content=b'{"errorCode":"ERR_STATE_STORE_NOT_FOUND"}')
        if DAPR_APP_ID_SEPARATOR in key:
            return Response(status_code=400, content=b'{"errorCode":"ERR_MALFORMED_REQUEST"}')
        return None

    async def _save(self, store: str, request: Request) -> Response:
        items = await request.json()
        for item in items:
            bad = self._guard(store, item["key"])
            if bad is not None:
                return bad
            self.rows[self.stored_key(item["key"])] = item["value"]
        return Response(status_code=204)

    async def _get(self, store: str, key: str) -> Response:
        bad = self._guard(store, key)
        if bad is not None:
            return bad
        row = self.rows.get(self.stored_key(key))
        if row is None:
            return Response(status_code=204)
        # Serve the stored bytes VERBATIM, the way a real sidecar does. Re-validating here made the fake
        # stricter than the thing it stands in for, so a row that had drifted out of schema could not be
        # expressed at all — which is exactly the case that turned out to lose data in production.
        return Response(status_code=200, content=json.dumps(row))

    async def _delete(self, store: str, key: str) -> Response:
        bad = self._guard(store, key)
        if bad is not None:
            return bad
        self.rows.pop(self.stored_key(key), None)
        return Response(status_code=204)


@pytest.fixture
def sidecar() -> FakeSidecar:
    return FakeSidecar()


@pytest.fixture
def store(sidecar: FakeSidecar) -> UserStateStore:
    """A real ``UserStateStore`` whose transport reaches the stand-in in-process."""
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=sidecar.app))
    return UserStateStore(store_name=sidecar.store, client=client)


def _graph(label: str = "alice's canvas") -> WorkflowGraph:
    """A small but structurally complete canvas — two nodes, an edge on the Search image port."""
    return WorkflowGraph.model_validate(
        {
            "nodes": [
                {"id": "q1", "type": "query", "position": {"x": 0, "y": 0}},
                {"id": "s1", "type": "search", "position": {"x": 240, "y": 0}},
            ],
            "edges": [{"id": "q1-s1", "source": "q1", "target": "s1", "targetHandle": "in"}],
            "config": {"q1": {"q": label, "mode": "hybrid", "n": 12}},
            "tags": {"s1": ["review"]},
        }
    )


# ── key rules: Dapr's reserved separator ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "subject",
    ["alice", "CgVhbGljZRIFbG9jYWw", "a||b", "|||", "alice@example.com", "sub/with/slashes", "üñî"],
)
def test_key_never_carries_the_reserved_separator(subject: str) -> None:
    key = state_key(subject, UserStateDocument.WORKFLOW_GRAPH)
    assert DAPR_APP_ID_SEPARATOR not in key
    # And nothing that a URL decode could TURN INTO one: base64url has no '%' escapes at all.
    assert "%" not in key and "/" not in key
    assert decode_subject(key.split(":")[1]) == subject


def test_key_is_stable_and_distinct_per_subject_and_document() -> None:
    a_graph = state_key("alice", UserStateDocument.WORKFLOW_GRAPH)
    assert a_graph == state_key("alice", UserStateDocument.WORKFLOW_GRAPH)
    assert a_graph != state_key("bob", UserStateDocument.WORKFLOW_GRAPH)
    assert a_graph != state_key("alice", UserStateDocument.SAVED_VIEWS)


def test_empty_subject_is_refused() -> None:
    # There is no anonymous user state; a blank subject must never compose a key that someone else could
    # also compose.
    with pytest.raises(ValueError, match="non-empty verified subject"):
        state_key("   ", UserStateDocument.WORKFLOW_GRAPH)


def test_percent_encoding_would_not_have_worked(sidecar: FakeSidecar) -> None:
    """The reason base64url is used, pinned: a percent-encoded '|' decodes back to '|' at the sidecar.

    Not a test of our code — a test of the constraint our code is shaped by. If this ever stops holding,
    the comment in ``service_kit/governed/user_state.py`` explaining the encoding choice is wrong.
    """
    from urllib.parse import quote

    percent_encoded = f"user-state:{quote('a||b', safe='')}:workflow-graph"
    assert DAPR_APP_ID_SEPARATOR not in percent_encoded  # looks safe...
    client = TestClient(sidecar.app)
    # ...but the sidecar decodes the path, so the store would see the separator and 400.
    assert client.get(f"/v1.0/state/{sidecar.store}/{percent_encoded}").status_code == 400
    assert client.get(f"/v1.0/state/{sidecar.store}/{state_key('a||b', UserStateDocument.WORKFLOW_GRAPH)}").status_code == 204


# ── the round trip, against a real HTTP transport ────────────────────────────────────────────────────


def test_round_trip_through_the_sidecar(store: UserStateStore, sidecar: FakeSidecar) -> None:
    graph = _graph()
    payload = graph.model_dump(mode="json", by_alias=True, exclude_none=True)

    written = asyncio.run(store.put(subject="alice", document=UserStateDocument.WORKFLOW_GRAPH, value=payload))
    read = asyncio.run(store.get(subject="alice", document=UserStateDocument.WORKFLOW_GRAPH))

    assert read is not None
    assert read.subject == "alice"
    assert read.value == payload
    assert read.updated_at == written.updated_at
    # What the STORE holds: Dapr's own app-id prefix, and our key intact under it.
    assert sidecar.stored_key(state_key("alice", UserStateDocument.WORKFLOW_GRAPH)) in sidecar.rows


def test_absent_document_reads_as_none_not_an_error(store: UserStateStore) -> None:
    # Dapr answers a missing key with 204; a first visit is the common case, never an error.
    assert asyncio.run(store.get(subject="alice", document=UserStateDocument.SAVED_VIEWS)) is None


def test_delete_is_idempotent(store: UserStateStore) -> None:
    asyncio.run(store.put(subject="alice", document=UserStateDocument.SAVED_VIEWS, value=[]))
    asyncio.run(store.delete(subject="alice", document=UserStateDocument.SAVED_VIEWS))
    asyncio.run(store.delete(subject="alice", document=UserStateDocument.SAVED_VIEWS))
    assert asyncio.run(store.get(subject="alice", document=UserStateDocument.SAVED_VIEWS)) is None


def test_store_failure_is_fail_closed_not_empty(store: UserStateStore, sidecar: FakeSidecar) -> None:
    """A wedged store must look UNAVAILABLE, never like an empty document set.

    If a 500 read as "you have no saved work", the client would seed a fresh canvas and the next autosave
    would overwrite the user's real graph with it — data loss caused by a transient blip.
    """
    sidecar.fail_next_with = 500
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(store.get(subject="alice", document=UserStateDocument.WORKFLOW_GRAPH))


def test_unreachable_store_is_fail_closed(sidecar: FakeSidecar) -> None:
    def _refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    unreachable = UserStateStore(store_name=sidecar.store, client=httpx.AsyncClient(transport=httpx.MockTransport(_refuse)))
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(unreachable.get(subject="alice", document=UserStateDocument.WORKFLOW_GRAPH))


def test_record_owned_by_another_subject_is_refused(store: UserStateStore, sidecar: FakeSidecar) -> None:
    """Defence in depth: the record carries its owner, so a mis-keyed row still cannot be served.

    Simulates the only way this can happen — a row hand-written (or migrated) under bob's key while
    carrying alice's subject. Reading it must not yield alice's canvas, and must NOT read as absent
    either: absent tells bob's client to seed, and its next autosave would destroy alice's work under
    bob's key. Unreadable is the honest answer, and it is the answer that keeps the row intact.
    """
    bob_key = state_key("bob", UserStateDocument.WORKFLOW_GRAPH)
    forged = StoredState(
        subject="alice",
        document=UserStateDocument.WORKFLOW_GRAPH,
        updated_at="2026-07-26T00:00:00Z",  # type: ignore[arg-type]
        value={"nodes": []},
    )
    sidecar.rows[sidecar.stored_key(bob_key)] = forged.model_dump(mode="json")
    with pytest.raises(UserStateUnreadable, match="another subject"):
        asyncio.run(store.get(subject="bob", document=UserStateDocument.WORKFLOW_GRAPH))


def test_an_unparseable_record_is_unreadable_not_absent(store: UserStateStore, sidecar: FakeSidecar) -> None:
    """The data-loss path: schema drift must never read as "you have no saved work".

    Reproduced live before this raised — one field of a real row changed from ``str`` to ``int`` (an
    ordinary schema evolution), the row entirely intact, and ``GET /v1/user-state/saved-views`` answered
    ``exists: false``. A client told the document is empty seeds a fresh one, and its next autosave
    overwrites the record that was still there. The module's own docstring already forbids this shape for
    an outage ("must look unavailable, never empty, or the next autosave would overwrite the real
    document"); it arrived through drift instead.
    """
    key = state_key("alice", UserStateDocument.SAVED_VIEWS)
    sidecar.rows[sidecar.stored_key(key)] = {"subject": "alice", "value": [], "updated_at": "not-a-date"}

    with pytest.raises(UserStateUnreadable, match="schema"):
        asyncio.run(store.get(subject="alice", document=UserStateDocument.SAVED_VIEWS))

    # And the row is still there — the point of refusing is that nothing destroyed it.
    assert sidecar.stored_key(key) in sidecar.rows


# ── the mirrored schemas ─────────────────────────────────────────────────────────────────────────────


def test_search_modes_match_the_search_service() -> None:
    """The mirror in ``service_kit.schemas.workflow`` cannot drift from the real ``SearchMode``.

    ``service_kit`` may not import a service package (it is the shared kernel every service imports), so the
    seven modes are restated there — and this is what keeps the restatement honest.
    """
    from search.services.spec import SearchMode

    assert {m.value for m in WorkflowSearchMode} == {m.value for m in SearchMode}


def test_graph_rejects_out_of_range_n() -> None:
    # valibot CLAMPS n to [1, 100] because localStorage is a cache that must self-heal. A server is a
    # boundary: it says what is wrong instead of quietly storing something else.
    with pytest.raises(ValueError, match="less than or equal to 100"):
        WorkflowGraph.model_validate(
            {
                "nodes": [{"id": "q1", "type": "query", "position": {"x": 0, "y": 0}}],
                "config": {"q1": {"n": 999}},
            }
        )


def test_graph_rejects_an_unknown_node_kind() -> None:
    with pytest.raises(ValueError, match="type"):
        WorkflowGraph.model_validate({"nodes": [{"id": "x", "type": "teleport", "position": {"x": 0, "y": 0}}]})


def test_graph_rejects_an_empty_canvas() -> None:
    # Mirrors v.minLength(1): an empty canvas is what the client seeds, not something worth storing.
    with pytest.raises(ValueError, match="at least 1 item"):
        WorkflowGraph.model_validate({"nodes": []})


def test_unknown_keys_are_dropped_like_valibot_strips_them() -> None:
    graph = WorkflowGraph.model_validate({"nodes": [{"id": "q1", "type": "query", "position": {"x": 1, "y": 2}, "width": 180}]})
    assert not hasattr(graph.nodes[0], "width")


def test_camel_case_survives_the_round_trip() -> None:
    view = SavedView(
        name="low confidence",
        dataset="",
        spec=SearchSpec(q="htr", mode=WorkflowSearchMode.HYBRID, q_vec="cached", rerank_n=20),
    )
    wire = view.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert wire["spec"]["qVec"] == "cached" and wire["spec"]["rerankN"] == 20
    assert SavedView.model_validate(wire) == view


# ── the full app: identity comes from the token, and only from the token ─────────────────────────────


def _oidc_settings() -> Settings:
    return Settings.model_validate(
        {
            "oidc_enabled": True,
            "oidc_issuer": "https://idp.example",
            "oidc_audience": "lance",
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
            "impl": "dir",
            "root": "/tmp/lance-user-state-test",
        }
    )


class _SubjectIsTheToken:
    """A verifier where the bearer string IS the subject — so a test can be two people trivially."""

    def verify(self, token: str) -> IDToken:
        return IDToken(iss="https://idp.example", sub=token, aud="lance", exp=1, iat=1)


@pytest.fixture
def app_client(monkeypatch: pytest.MonkeyPatch, store: UserStateStore) -> Iterator[TestClient]:
    """The real catalog app with OIDC on, its user-state store pointed at the stand-in sidecar."""
    monkeypatch.setenv("LANCE_REST_IMPL", "dir")
    monkeypatch.setenv("LANCE_REST_ROOT", "/tmp/lance-user-state-test")
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("LANCE_S3_SECRET_ACCESS_KEY", "test")
    get_settings.cache_clear()

    from catalog.main import app

    app.dependency_overrides[get_settings] = _oidc_settings
    app.dependency_overrides[ep.get_user_state_store] = lambda: store
    with TestClient(app) as client:
        client.app.state.oidc = _SubjectIsTheToken()
        yield client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _as(client: TestClient, who: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {who}"}


def test_alice_writes_and_reads_her_own_canvas(app_client: TestClient) -> None:
    graph = _graph().model_dump(mode="json", by_alias=True, exclude_none=True)
    put = app_client.put("/v1/user-state/workflow-graph", json=graph, headers=_as(app_client, "alice"))
    assert put.status_code == 200, put.text
    assert put.json()["subject"] == "alice"

    got = app_client.get("/v1/user-state/workflow-graph", headers=_as(app_client, "alice"))
    assert got.status_code == 200
    body = got.json()
    assert body["exists"] is True and body["subject"] == "alice"
    assert body["value"] == graph
    assert body["value"]["config"]["q1"]["q"] == "alice's canvas"


def test_bob_cannot_read_alices_state(app_client: TestClient) -> None:
    """THE test. Two callers, one URL, no difference but the token — and no leak.

    A store keyed on a path segment, a body field or an ``X-User`` header would pass every other test in
    this file and fail here. Note what is NOT in the request: there is nothing bob could have changed to
    address alice's document, because the endpoint offers no place to put an identity.
    """
    alice_graph = _graph("alice's canvas").model_dump(mode="json", by_alias=True, exclude_none=True)
    assert app_client.put("/v1/user-state/workflow-graph", json=alice_graph, headers=_as(app_client, "alice")).status_code == 200

    bob = app_client.get("/v1/user-state/workflow-graph", headers=_as(app_client, "bob"))
    assert bob.status_code == 200
    body = bob.json()
    assert body["subject"] == "bob"
    assert body["exists"] is False
    assert "value" not in body  # not alice's canvas, and not a null pretending to be one

    # And bob writing his own must not touch hers.
    bob_graph = _graph("bob's canvas").model_dump(mode="json", by_alias=True, exclude_none=True)
    app_client.put("/v1/user-state/workflow-graph", json=bob_graph, headers=_as(app_client, "bob"))
    still_hers = app_client.get("/v1/user-state/workflow-graph", headers=_as(app_client, "alice"))
    assert still_hers.json()["value"]["config"]["q1"]["q"] == "alice's canvas"
    assert app_client.get("/v1/user-state/workflow-graph", headers=_as(app_client, "bob")).json()["value"]["config"]["q1"]["q"] == "bob's canvas"


def test_bob_cannot_delete_alices_state(app_client: TestClient) -> None:
    graph = _graph().model_dump(mode="json", by_alias=True, exclude_none=True)
    app_client.put("/v1/user-state/workflow-graph", json=graph, headers=_as(app_client, "alice"))
    assert app_client.delete("/v1/user-state/workflow-graph", headers=_as(app_client, "bob")).status_code == 204
    assert app_client.get("/v1/user-state/workflow-graph", headers=_as(app_client, "alice")).json()["exists"]


def test_a_fresh_client_reads_what_the_first_one_wrote(app_client: TestClient, store: UserStateStore) -> None:
    """Condition 5's actual claim: the work survives leaving the browser it was made in.

    The second client shares nothing with the first except the store — new app instance, new cookie jar,
    new connection pool. That is the server-side half of "write in one browser context, read it in a
    fresh one".
    """
    views = [SavedView(name="ambiguous speakers", dataset="", spec=SearchSpec(q="speaker")).model_dump(mode="json", by_alias=True, exclude_none=True)]
    app_client.put("/v1/user-state/saved-views", json=views, headers=_as(app_client, "alice"))

    from catalog.main import app

    with TestClient(app) as fresh:
        fresh.app.state.oidc = _SubjectIsTheToken()
        body = fresh.get("/v1/user-state/saved-views", headers=_as(fresh, "alice")).json()
    assert body["exists"] is True
    assert body["value"] == views


def test_anonymous_is_401(app_client: TestClient) -> None:
    for method in ("get", "delete"):
        assert getattr(app_client, method)("/v1/user-state/saved-views").status_code == 401
    assert app_client.put("/v1/user-state/saved-views", json=[]).status_code == 401


def test_unwired_store_is_503_not_an_empty_document(app_client: TestClient) -> None:
    from catalog.main import app

    app.dependency_overrides[ep.get_user_state_store] = lambda: None
    resp = app_client.get("/v1/user-state/workflow-graph", headers=_as(app_client, "alice"))
    assert resp.status_code == 503
    assert resp.headers["content-type"].startswith("application/problem+json")


def test_a_document_past_the_ceiling_is_refused(app_client: TestClient) -> None:
    # An unmetered per-user write surface onto a shared Postgres gets its own bound; max_body_bytes is
    # sized for Arrow-IPC (256 MiB) and would wave this through.
    fat = _graph().model_dump(mode="json", by_alias=True, exclude_none=True)
    fat["tags"] = {"s1": ["x" * 600_000]}
    resp = app_client.put("/v1/user-state/workflow-graph", json=fat, headers=_as(app_client, "alice"))
    assert resp.status_code == 400
    assert "per-document limit" in resp.json()["detail"]


def test_nulls_are_omitted_so_the_zone_can_still_parse_it(app_client: TestClient) -> None:
    """The client's valibot edge schema is ``v.optional(v.string())``: absent is fine, ``null`` FAILS.

    A null here would fail the whole graph parse in the browser and silently reseed the canvas — the
    user's work would look lost even though the server held it.
    """
    graph = _graph().model_dump(mode="json", by_alias=True, exclude_none=True)
    graph["edges"][0].pop("targetHandle", None)
    body = app_client.put("/v1/user-state/workflow-graph", json=graph, headers=_as(app_client, "alice")).json()
    edge = body["value"]["edges"][0]
    assert "targetHandle" not in edge and "label" not in edge
    config = body["value"]["config"]["q1"]
    assert "minScore" not in config and "capturedAtlasSelection" not in config


def test_a_malformed_document_is_422_not_a_stored_surprise(app_client: TestClient) -> None:
    resp = app_client.put(
        "/v1/user-state/workflow-graph",
        json={"nodes": [{"id": "q1", "type": "teleport", "position": {"x": 0, "y": 0}}]},
        headers=_as(app_client, "alice"),
    )
    assert resp.status_code == 422


def test_the_document_set_is_closed(app_client: TestClient) -> None:
    # The key space is ours, not the caller's: there is no route that turns caller input into a key.
    assert app_client.get("/v1/user-state/anything-else", headers=_as(app_client, "alice")).status_code == 404


def test_subject_encoding_round_trips() -> None:
    for subject in ("alice", "CgVhbGljZRIFbG9jYWw", "a||b", "üñî"):
        assert decode_subject(encode_subject(subject)) == subject
    assert base64.urlsafe_b64decode(encode_subject("alice") + "===").decode() == "alice"


def test_defaults_are_the_zones_own_fallbacks() -> None:
    """A partial config comes back COMPLETE — so every default must be what valibot would have healed to.

    Found live: the round trip is faithful, not byte-identical, because the server fills fields the client
    omitted. That is only safe while each default equals the corresponding ``v.fallback`` in
    ``frontend/components/frontends/media/src/lib/workflow/persistence.ts``. Change one without changing
    the other and a reload silently rewrites the user's node config; this table is what notices.
    """
    from service_kit.schemas.workflow import DEFAULT_N, WorkflowNodeConfig

    filled = WorkflowNodeConfig().model_dump(mode="json", by_alias=True)
    assert filled == {
        "q": "",  # v.fallback(v.string(), '')
        "imageName": "",  # v.fallback(v.string(), '')
        "where": "",  # v.fallback(v.string(), '')
        "filters": {},  # v.fallback(v.record(...), () => ({}))
        "mode": "fts",  # v.fallback(SearchModeSchema, 'fts')
        "n": DEFAULT_N,  # v.fallback(v.number(), DEFAULT_N)
        "rerank": False,  # v.fallback(v.boolean(), false)
        "minScore": None,  # v.fallback(v.nullable(v.number()), null) — omitted on the wire
        "refineScope": "video",  # v.fallback(v.picklist(['video','chunk']), 'video')
        "combineMode": "union",  # v.fallback(v.picklist(['union','intersect']), 'union')
        "tags": [],  # v.fallback(v.array(v.string()), () => [])
        "exportFormat": "csv",  # v.fallback(v.picklist(['json','csv']), 'csv')
        "exportColumns": None,  # v.fallback(v.nullable(v.array(v.string())), null)
        "capturedAtlasSelection": None,  # v.fallback(v.null_(), null) — never persisted
        "label": "",  # v.fallback(v.string(), '')
        "enabled": True,  # v.fallback(v.boolean(), true)
    }
