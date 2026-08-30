"""Unit tests for the lineage service's auth gate (audit ``w8u4rc2tg``, P0 #1/#2).

Infra-free, like ``test_lineage.py``: no database, no network. Drives the async gate with
stdlib ``asyncio.run`` (the project convention — see ``test_fga_resilience.py``) and fakes
the OpenFGA check + the OIDC verifier.

Covers:
* fail-closed config (a half-configured auth layer is a startup error),
* authn (off → open; on → token required / verified / fail-closed when unwired),
* the read authz gate (off → allow; unwired → 503; unauthenticated → 401; deny → 403; allow),
* author-forgery prevention (the verified subject overrides any body-claimed author),
* route wiring (the gate is actually attached to the endpoints).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import Request
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials
from lance_namespace import PermissionDeniedError, ServiceUnavailableError, UnauthenticatedError
from openfga_sdk import OpenFgaClient
from pydantic import ValidationError

from lineage.api import fga_deps, security
from lineage.core.config import LineageSettings
from lineage.models import RunEvent
from lineage.schemas import (
    ColumnEdge,
    ColumnGraph,
    ColumnNeighbors,
    ColumnNode,
    ColumnRef,
    DatasetRef,
    DatasetSchema,
    EstateGraph,
    EventRecord,
    GraphEdge,
    GraphNode,
    LineageGraph,
    Neighbors,
    RunInput,
    RunInputs,
    Runs,
    RunStatus,
    SchemaField,
)
from lineage.services.repository import LineageRepository
from service_kit.governed import fga
from service_kit.governed.oidc import IDToken


_ISSUER = "https://idp.example.com"


def _settings(**values: Any) -> LineageSettings:
    """Build settings from field names (``model_validate`` runs the fail-closed validator)."""
    return LineageSettings.model_validate(values)


def _token(sub: str = "alice") -> IDToken:
    return IDToken(iss=_ISSUER, sub=sub, aud="lance", exp=0, iat=0)


def _request(**state: object) -> Request:
    """A fake request exposing only ``request.app.state`` (the gate reads ``oidc``/``fga``)."""
    return cast(Request, SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(**state))))


def _creds(value: str = "tok") -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=value)


_FULL_AUTH = {
    "oidc_enabled": True,
    "oidc_issuer": _ISSUER,
    "oidc_audience": "lance",
    "fga_enabled": True,
    "fga_store_id": "store",
    "fga_model_id": "model",
}


# --------------------------------------------------------------------------- #
# Fail-closed config
# --------------------------------------------------------------------------- #


def test_oidc_enabled_requires_issuer_and_audience() -> None:
    with pytest.raises(ValidationError):
        _settings(oidc_enabled=True)


def test_fga_enabled_requires_oidc() -> None:
    with pytest.raises(ValidationError):
        _settings(fga_enabled=True, oidc_enabled=False)


def test_fga_enabled_without_store_and_model_is_valid() -> None:
    # store_id/model_id are OPTIONAL — main.py provisions the store by NAME at boot when they are absent
    # (idempotent convergence on the catalog's store). Requiring them here would (and did) crash the lineage
    # pod in governed mode, since the chart can't know the runtime-provisioned ids at template time.
    settings = _settings(oidc_enabled=True, oidc_issuer=_ISSUER, oidc_audience="lance", fga_enabled=True)
    assert settings.fga_enabled and settings.fga_store_id is None and settings.fga_model_id is None


def test_full_auth_config_is_valid() -> None:
    assert _settings(**_FULL_AUTH).fga_object_type == "table"


# --------------------------------------------------------------------------- #
# authenticate (authn)
# --------------------------------------------------------------------------- #


def test_authenticate_disabled_returns_none() -> None:
    assert security.authenticate(_request(), _settings(), None) is None


def test_authenticate_enabled_missing_token_raises() -> None:
    settings = _settings(oidc_enabled=True, oidc_issuer=_ISSUER, oidc_audience="lance")
    verifier = SimpleNamespace(verify=lambda _t: _token())
    with pytest.raises(UnauthenticatedError):
        security.authenticate(_request(oidc=verifier), settings, None)


def test_authenticate_enabled_unwired_verifier_fails_closed() -> None:
    settings = _settings(oidc_enabled=True, oidc_issuer=_ISSUER, oidc_audience="lance")
    with pytest.raises(ServiceUnavailableError):
        security.authenticate(_request(), settings, _creds())


def test_authenticate_enabled_verifies_token() -> None:
    settings = _settings(oidc_enabled=True, oidc_issuer=_ISSUER, oidc_audience="lance")
    verifier = SimpleNamespace(verify=lambda _t: _token("dee"))
    token = security.authenticate(_request(oidc=verifier), settings, _creds())
    assert token is not None and token.sub == "dee"


# --------------------------------------------------------------------------- #
# require_metadata_access (read authz gate)
# --------------------------------------------------------------------------- #


def test_gate_disabled_allows() -> None:
    # FGA off → no check, no raise (dev/test default, like the catalog).
    asyncio.run(fga_deps.require_metadata_access("a$b", _request(), _settings(), None))


def test_gate_unwired_client_fails_closed() -> None:
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(fga_deps.require_metadata_access("a$b", _request(), _settings(**_FULL_AUTH), _token()))


def test_gate_unauthenticated_raises() -> None:
    with pytest.raises(UnauthenticatedError):
        asyncio.run(fga_deps.require_metadata_access("a$b", _request(fga=object()), _settings(**_FULL_AUTH), None))


def test_gate_denies_without_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def _deny(_client: object, *, user: str, relation: str, obj: str) -> bool:
        captured.update(user=user, relation=relation, obj=obj)
        return False

    monkeypatch.setattr(fga, "check", _deny)
    client = cast(OpenFgaClient, object())
    with pytest.raises(PermissionDeniedError):
        asyncio.run(fga_deps.require_metadata_access("a$b", _request(fga=client), _settings(**_FULL_AUTH), _token()))
    # The denial must be on the right user/object/relation, not just "some" denial.
    assert captured == {"user": "alice", "relation": "can_get_metadata", "obj": "table:a$b"}


def test_gate_allows_with_permission(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def _allow(_client: object, *, user: str, relation: str, obj: str) -> bool:
        captured.update(user=user, relation=relation, obj=obj)
        return True

    monkeypatch.setattr(fga, "check", _allow)
    client = cast(OpenFgaClient, object())
    asyncio.run(fga_deps.require_metadata_access("a$b", _request(fga=client), _settings(**_FULL_AUTH), _token("dee")))
    # The dataset name is gated as table:<name> with the catalog's metadata-read relation.
    assert captured == {"user": "dee", "relation": "can_get_metadata", "obj": "table:a$b"}


# --------------------------------------------------------------------------- #
# enforce_author (provenance forgery prevention)
# --------------------------------------------------------------------------- #


def _event(claimed_author: str = "anon", outputs: list[str] | None = None, inputs: list[str] | None = None) -> RunEvent:
    return RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-06-24T00:00:00Z",
            "run": {"runId": "r1", "facets": {"author": {"name": claimed_author}}},
            "job": {"namespace": "jobs", "name": "promote"},
            "inputs": [{"namespace": "silver", "name": n} for n in (inputs or [])],
            "outputs": [{"namespace": "silver", "name": n} for n in (outputs or [])],
        }
    )


def test_enforce_author_overrides_body_claim() -> None:
    event = _event(claimed_author="attacker")
    fga_deps.enforce_author(event, _token("real-user"))
    assert event.author == "real-user"  # body claim is overwritten by the verified subject


def test_enforce_author_keeps_body_when_unauthenticated() -> None:
    event = _event(claimed_author="claimed")
    fga_deps.enforce_author(event, None)  # OIDC off (dev) → body author preserved
    assert event.author == "claimed"


# --------------------------------------------------------------------------- #
# Route wiring — the gate is actually attached to the endpoints
# --------------------------------------------------------------------------- #


def _api_routes(app: Any) -> list[Any]:
    """Flatten the app's routes, resolving starlette 1.3's lazy ``_IncludedRouter`` wrappers — the lineage
    routes now live under ``include_router``, not directly on ``app.routes`` as when main.py was flat.

    Wrappers are resolved via ``effective_route_contexts()``, NOT by walking ``original_router.routes``:
    only the effective view carries include-time prefixes (the ingest router mounts under ``/api/v1`` at
    the COMPOSITION layer — F-LIN-14 — so the raw walk would report the unprefixed ``/lineage``). The
    contexts duck-type what these tests read: ``.path``, ``.methods``, ``.dependant``."""
    out: list[Any] = []
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        if isinstance(route, APIRoute):
            out.append(route)
        elif hasattr(route, "effective_route_contexts"):
            out.extend(route.effective_route_contexts())
        elif hasattr(route, "routes"):
            stack.extend(route.routes)
    return out


def test_read_routes_wire_the_metadata_gate() -> None:
    from lineage.main import app

    gated = {
        "/datasets/{name}/upstream",
        "/datasets/{name}/downstream",
        "/datasets/{name}/producers",
        "/datasets/{name}/graph",
        "/datasets/{name}/creator",
        "/datasets/{name}/reconcile",
        "/datasets/{name}/schema",
        "/datasets/{name}/columns/{field}/upstream",
        "/datasets/{name}/columns/{field}/downstream",
        "/datasets/{name}/columns",
        "/datasets/{name}/governance",
    }
    seen = set()
    for route in _api_routes(app):
        if route.path in gated:
            calls = [d.call for d in route.dependant.dependencies]
            assert fga_deps.require_metadata_access in calls, route.path
            assert fga_deps.audit_read in calls, route.path  # #6: every gated read is also audited
            seen.add(route.path)
    assert seen == gated  # every per-dataset read is present and gated


def test_governance_write_routes_wire_the_writer_gate() -> None:
    """The #49 write routes must carry require_write_access as a route dependency — the handler tests
    drive the gate separately, so without this pin deleting the router-level dependency would un-gate
    governance writes while every test stays green (the false-confidence-authz class this file exists
    to close)."""
    from lineage.main import app

    write_gated = {
        ("PUT", "/datasets/{name}/tags/{tag}"),
        ("DELETE", "/datasets/{name}/tags/{tag}"),
        ("PUT", "/datasets/{name}/description"),
    }
    seen = set()
    for route in _api_routes(app):
        for method in getattr(route, "methods", set()) or set():
            if (method, route.path) in write_gated:
                calls = [d.call for d in route.dependant.dependencies]
                assert fga_deps.require_write_access in calls, (method, route.path)
                seen.add((method, route.path))
    assert seen == write_gated, f"write routes missing from the app: {write_gated - seen}"


def test_readers_route_wires_the_owner_gate() -> None:
    """The read-audit query ``GET /datasets/{name}/readers`` exposes WHO accessed a dataset — an access
    log more sensitive than the dataset's own schema — so it must carry ``require_write_access``
    (owner/writer) ON TOP of the router-level ``can_get_metadata`` gate. Pin the structure: a casual
    reader must not be able to enumerate who else viewed a dataset, and dropping this route dependency
    would silently un-gate that while every handler test stays green (the false-confidence-authz class)."""
    from lineage.main import app

    route = next(r for r in _api_routes(app) if r.path == "/datasets/{name}/readers" and "GET" in (getattr(r, "methods", set()) or set()))
    calls = [d.call for d in route.dependant.dependencies]
    assert fga_deps.require_write_access in calls


def test_ingest_route_requires_authentication() -> None:
    from lineage.main import app

    ingest = next(r for r in _api_routes(app) if r.path == "/api/v1/lineage")
    calls = [d.call for d in ingest.dependant.dependencies]
    assert security.authenticate in calls


def test_graph_route_wires_the_dataset_filter() -> None:
    """§7a: the handler-level /graph filter tests below construct the filter EXPLICITLY, so they stay
    green even if the route loses ``FilterDep`` — bind the structure: the real /graph route's dependant
    tree must carry ``get_dataset_filter`` (the transitive-disclosure filter's provider)."""
    from lineage.main import app

    route = next(r for r in _api_routes(app) if r.path == "/datasets/{name}/graph")
    calls = [d.call for d in route.dependant.dependencies]
    assert fga_deps.get_dataset_filter in calls


# --------------------------------------------------------------------------- #
# DatasetFilter — transitive-disclosure filtering (audit w8u4rc2tg, security medium)
# --------------------------------------------------------------------------- #


def test_filter_passthrough_when_fga_off() -> None:
    flt = fga_deps.DatasetFilter(_request(), _settings(), None)
    assert asyncio.run(flt.visible(["a", "b"])) == {"a", "b"}


def test_filter_empty_names_skips_check() -> None:
    flt = fga_deps.DatasetFilter(_request(fga=object()), _settings(**_FULL_AUTH), _token())
    assert asyncio.run(flt.visible([])) == set()


async def _batch_allow_a(_client: object, *, objects: list[str], **_kw: object) -> dict[str, bool]:
    """Fake batch_check: only ``table:a`` is visible."""
    return {o: o == "table:a" for o in objects}


def test_filter_drops_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fga, "batch_check", _batch_allow_a)
    flt = fga_deps.DatasetFilter(_request(fga=cast(OpenFgaClient, object())), _settings(**_FULL_AUTH), _token())
    assert asyncio.run(flt.visible(["a", "b"])) == {"a"}


# --------------------------------------------------------------------------- #
# Handler-body behavior the route-dependency introspection can't see:
# the read handler must APPLY the filter, and ingest must bind the verified author.
# --------------------------------------------------------------------------- #


class _FakeRepo:
    """Minimal repository: captures the ingested event / returns two canned neighbors."""

    def __init__(self) -> None:
        self.ingested: RunEvent | None = None
        self.events: list[EventRecord] = []
        self.list_events_calls: list[dict[str, object]] = []
        # The depth each rooted read was asked for, so a test can assert the route THREADS it rather
        # than merely tolerating it. A double that silently swallows a new argument is how "the
        # endpoint takes a depth" and "the endpoint uses the depth" drift apart — which is exactly
        # what happened here: these two fakes kept the old two-argument signature after the real
        # methods grew a depth, and the mismatch was invisible until the endpoint called them.
        self.seen_depths: dict[str, object] = {}
        self.runs: list[RunStatus] = []
        self.inputs: list[RunInput] = []
        self.write_version: int | None = None
        self.uri: str | None = None
        self.col_related: list[ColumnRef] = []
        self.col_graph: ColumnGraph | None = None
        self.lineage_graph: LineageGraph | None = None
        self.oldest: int | None = None
        self.estate: EstateGraph | None = None

    async def ingest_event(self, event: RunEvent) -> None:
        self.ingested = event

    async def record_event(self, **_kwargs: object) -> None:
        return None

    async def list_events(self, limit: int = 500, *, after: int | None = None, summary: bool = False) -> list[EventRecord]:
        self.list_events_calls.append({"limit": limit, "after": after, "summary": summary})
        return self.events

    async def oldest_event_seq(self) -> int | None:
        return self.oldest

    async def list_runs(self) -> Runs:
        return Runs(runs=self.runs)

    async def run_inputs(self, run_id: str) -> RunInputs:
        return RunInputs(run_id=run_id, inputs=self.inputs)

    async def latest_write_version(self, name: str) -> int | None:
        return self.write_version

    async def source_uri(self, name: str) -> str | None:
        return self.uri

    async def dataset_schema(self, name: str, version: int | None = None) -> DatasetSchema:
        return DatasetSchema(dataset=name, version=version or 2, fields=[SchemaField(name="id", type="int")])

    async def estate_graph(self) -> EstateGraph:
        assert self.estate is not None
        return self.estate

    async def column_upstream(self, dataset: str, field: str) -> ColumnNeighbors:
        return ColumnNeighbors(dataset=dataset, field=field, related=self.col_related)

    async def column_downstream(self, dataset: str, field: str) -> ColumnNeighbors:
        return ColumnNeighbors(dataset=dataset, field=field, related=self.col_related)

    async def dataset_column_graph(self, name: str, depth: int = 1) -> ColumnGraph:
        self.seen_depths["columns"] = depth
        return self.col_graph or ColumnGraph(root=name)

    async def graph(self, name: str, depth: int | None = None) -> LineageGraph:
        self.seen_depths["graph"] = depth
        return self.lineage_graph or LineageGraph(root=name, nodes=[], edges=[])

    async def upstream(self, name: str) -> Neighbors:
        return Neighbors(dataset=name, related=[DatasetRef(name="a"), DatasetRef(name="b")])


def test_get_upstream_drops_unauthorized_related(monkeypatch: pytest.MonkeyPatch) -> None:
    from lineage.api.v1.endpoints.datasets import get_upstream

    monkeypatch.setattr(fga, "batch_check", _batch_allow_a)
    flt = fga_deps.DatasetFilter(_request(fga=cast(OpenFgaClient, object())), _settings(**_FULL_AUTH), _token())
    result = asyncio.run(get_upstream("root", cast(LineageRepository, _FakeRepo()), flt))
    assert [ref.name for ref in result.related] == ["a"]  # "b" is filtered out


def test_run_inputs_surfaces_pinned_versions_and_is_governed(monkeypatch: pytest.MonkeyPatch) -> None:
    """GET /runs/{id}/inputs exposes each input's PINNED version (#115 D1) and drops what the caller
    can't see — the API surface for what was Cypher-only, and the "which feature versions made this
    model" answer."""
    from lineage.api.v1.endpoints.runs import get_run_inputs

    monkeypatch.setattr(fga, "batch_check", _batch_allow_a)  # only "a" visible
    settings = _settings(**_FULL_AUTH)
    repo = _FakeRepo()
    repo.inputs = [
        RunInput(name="a", version="28"),  # visible + pinned → surfaced with its version
        RunInput(name="b", version="3"),  # not visible → dropped (no cross-grant disclosure)
    ]
    flt = fga_deps.DatasetFilter(_request(fga=cast(OpenFgaClient, object())), settings, _token())
    result = asyncio.run(get_run_inputs("train-run", cast(LineageRepository, repo), flt, settings))
    assert result.run_id == "train-run"
    assert [(i.name, i.version) for i in result.inputs] == [("a", "28")]  # b filtered; a keeps its pin


def test_run_inputs_pass_through_when_auth_off() -> None:
    """Auth off (the dev default) → no filtering, versions intact, unpinned reads carry version None."""
    from lineage.api.v1.endpoints.runs import get_run_inputs

    settings = _settings()  # oidc/fga off
    repo = _FakeRepo()
    repo.inputs = [
        RunInput(name="silver$features", version="28"),
        RunInput(name="gold$catalog", version=None),
    ]
    flt = fga_deps.DatasetFilter(_request(), settings, None)
    result = asyncio.run(get_run_inputs("r", cast(LineageRepository, repo), flt, settings))
    assert [(i.name, i.version) for i in result.inputs] == [("silver$features", "28"), ("gold$catalog", None)]


def test_get_events_filters_to_visible_datasets(monkeypatch: pytest.MonkeyPatch) -> None:
    # #22: the durable events feed is governed — drop events referencing a dataset the caller can't see.
    from lineage.api.v1.endpoints.runs import get_events

    monkeypatch.setattr(fga, "batch_check", _batch_allow_a)  # only "a" visible
    settings = _settings(**_FULL_AUTH)
    repo = _FakeRepo()
    repo.events = [
        EventRecord(seq=2, outputs=["a"], inputs=[], event={}),
        EventRecord(seq=1, outputs=["b"], inputs=[], event={}),  # "b" not visible → dropped
    ]
    flt = fga_deps.DatasetFilter(_request(fga=cast(OpenFgaClient, object())), settings, _token())
    result = asyncio.run(get_events(cast(LineageRepository, repo), flt, settings))
    assert [e.outputs for e in result.events] == [["a"]]  # the "b" event is filtered out


def test_get_events_hides_dataset_less_events_when_governed(monkeypatch: pytest.MonkeyPatch) -> None:
    # #22 audit: a dataset-less event (empty inputs+outputs) must NOT pass the gate vacuously when FGA is
    # on — it would otherwise leak the run/author/full event JSON to a caller with zero grants.
    from lineage.api.v1.endpoints.runs import get_events

    monkeypatch.setattr(fga, "batch_check", _batch_allow_a)
    settings = _settings(**_FULL_AUTH)
    repo = _FakeRepo()
    repo.events = [EventRecord(seq=1, outputs=[], inputs=[], event={"run": {"runId": "secret"}})]
    flt = fga_deps.DatasetFilter(_request(fga=cast(OpenFgaClient, object())), settings, _token())
    result = asyncio.run(get_events(cast(LineageRepository, repo), flt, settings))
    assert result.events == []  # dataset-less event is hidden under governance


def test_get_events_pagination_never_leaks_past_governance(monkeypatch: pytest.MonkeyPatch) -> None:
    # §2 perf (2026-07-11) guardrail pin: the governance filter runs BEFORE the slice on EVERY page —
    # a small `limit` must page through VISIBLE rows only; a hidden row never appears on any page.
    from lineage.api.v1.endpoints.runs import get_events

    monkeypatch.setattr(fga, "batch_check", _batch_allow_a)  # only "a" visible
    settings = _settings(**_FULL_AUTH)
    repo = _FakeRepo()
    repo.events = [
        EventRecord(seq=3, outputs=["a"], inputs=[], event={}),
        EventRecord(seq=2, outputs=["b"], inputs=[], event={}),  # hidden — must never surface
        EventRecord(seq=1, outputs=["a"], inputs=[], event={}),
    ]
    flt = fga_deps.DatasetFilter(_request(fga=cast(OpenFgaClient, object())), settings, _token())
    result = asyncio.run(get_events(cast(LineageRepository, repo), flt, settings, limit=1))
    assert [e.seq for e in result.events] == [3]
    assert result.next_cursor == 3  # continue below the last VISIBLE row — not below the hidden one
    # auth ON keeps the wide fetch window so filtered-out rows don't starve the page
    assert repo.list_events_calls[-1]["limit"] == 2000


def test_get_events_cursor_and_fetch_width(monkeypatch: pytest.MonkeyPatch) -> None:
    # Auth OFF → governed() is pass-through, so the endpoint reads EXACTLY `limit` rows (the 2s
    # poll no longer pays a 2000-row window), and the keyset cursor advances / terminates correctly.
    from lineage.api.v1.endpoints.runs import get_events

    settings = _settings()  # auth off
    repo = _FakeRepo()
    flt = fga_deps.DatasetFilter(_request(), settings, None)
    repo.events = [
        EventRecord(seq=5, outputs=["a"], inputs=[], event={}),
        EventRecord(seq=4, outputs=["a"], inputs=[], event={}),
    ]
    page = asyncio.run(get_events(cast(LineageRepository, repo), flt, settings, limit=2))
    assert repo.list_events_calls[-1] == {"limit": 2, "after": None, "summary": False}
    assert [e.seq for e in page.events] == [5, 4] and page.next_cursor == 4  # full page → continue

    repo.events = [EventRecord(seq=3, outputs=["a"], inputs=[], event={})]
    page = asyncio.run(get_events(cast(LineageRepository, repo), flt, settings, after=4, limit=2))
    assert repo.list_events_calls[-1] == {"limit": 2, "after": 4, "summary": False}
    assert [e.seq for e in page.events] == [3] and page.next_cursor is None  # short page → exhausted

    # summary rides through to the SQL projection layer untouched
    asyncio.run(get_events(cast(LineageRepository, repo), flt, settings, summary=True))
    assert repo.list_events_calls[-1]["summary"] is True


def test_get_column_upstream_filters_to_visible_datasets(monkeypatch: pytest.MonkeyPatch) -> None:
    # #24: column provenance is governed — a related column in a dataset the caller can't see is dropped
    # (a column has no ACL of its own; it inherits its owning table's visibility).
    from lineage.api.v1.endpoints.columns import get_column_upstream

    monkeypatch.setattr(fga, "batch_check", _batch_allow_a)  # only "a" visible
    settings = _settings(**_FULL_AUTH)
    repo = _FakeRepo()
    repo.col_related = [ColumnRef(dataset="a", field="x"), ColumnRef(dataset="b", field="y")]
    flt = fga_deps.DatasetFilter(_request(fga=cast(OpenFgaClient, object())), settings, _token())
    result = asyncio.run(get_column_upstream("a", "root", cast(LineageRepository, repo), flt, settings))
    assert [(r.dataset, r.field) for r in result.related] == [("a", "x")]  # b's column is hidden


def test_get_column_downstream_filters_to_visible_datasets(monkeypatch: pytest.MonkeyPatch) -> None:
    # #24: column IMPACT is governed identically to provenance — a related column in a hidden dataset drops.
    from lineage.api.v1.endpoints.columns import get_column_downstream

    monkeypatch.setattr(fga, "batch_check", _batch_allow_a)  # only "a" visible
    settings = _settings(**_FULL_AUTH)
    repo = _FakeRepo()
    repo.col_related = [ColumnRef(dataset="a", field="x"), ColumnRef(dataset="b", field="y")]
    flt = fga_deps.DatasetFilter(_request(fga=cast(OpenFgaClient, object())), settings, _token())
    result = asyncio.run(get_column_downstream("a", "root", cast(LineageRepository, repo), flt, settings))
    assert [(r.dataset, r.field) for r in result.related] == [("a", "x")]  # b's column is hidden


def test_get_graph_drops_hidden_nodes_and_edges_both_directions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The dataset-level /graph transitive-disclosure guarantee (the contract the column tests cite as
    # "same as /graph"): a node the caller can't see is dropped, and an edge needs BOTH endpoints visible
    # — proven in both leak directions (source-hidden AND target-hidden). The requested root rides on the
    # route gate's authorization, so the filter must keep it WITHOUT re-checking it (visible() is called
    # with the other nodes only).
    from lineage.api.v1.endpoints.datasets import get_graph

    checked: list[str] = []

    async def _allow_c(_client: object, *, objects: list[str], **_kw: object) -> dict[str, bool]:
        checked.extend(objects)
        return {o: o == "table:c" for o in objects}

    monkeypatch.setattr(fga, "batch_check", _allow_c)
    settings = _settings(**_FULL_AUTH)
    repo = _FakeRepo()
    repo.lineage_graph = LineageGraph(
        root="a",
        nodes=[GraphNode(id="a"), GraphNode(id="b"), GraphNode(id="c")],
        edges=[
            GraphEdge(source="a", target="c"),  # KEEP: both endpoints visible
            GraphEdge(source="b", target="a"),  # source hidden
            GraphEdge(source="a", target="b"),  # target hidden
        ],
    )
    flt = fga_deps.DatasetFilter(_request(fga=cast(OpenFgaClient, object())), settings, _token())
    result = asyncio.run(get_graph("a", cast(LineageRepository, repo), flt, 2))
    assert {n.id for n in result.nodes} == {"a", "c"}  # b dropped; the root kept without an FGA check
    # THE DEPTH MUST REACH THE REPOSITORY. Governance runs over whatever the walk returned, so a
    # depth the route accepts and then drops would quietly serve a different neighbourhood than the
    # caller asked for — and every assertion above would still pass, because the canned graph does
    # not depend on it.
    assert repo.seen_depths["graph"] == 2, f"get_graph was asked for depth 2 and passed {repo.seen_depths.get('graph')!r} to the repository"
    assert "table:a" not in checked  # the root was NOT re-checked (the route gate already authorized it)
    assert [(e.source, e.target) for e in result.edges] == [("a", "c")]  # both leak directions dropped


def test_estate_graph_governed_like_datasets(monkeypatch: pytest.MonkeyPatch) -> None:
    # The bulk /graph read carries the SAME transitive-disclosure guarantee as the per-dataset
    # graph: a non-visible node drops, and an edge needs BOTH endpoints visible (proven in both
    # leak directions). There is no route-gated root here — every node is checked.
    from lineage.api.v1.endpoints.discovery import estate_graph

    async def _allow_a_c(_client: object, *, objects: list[str], **_kw: object) -> dict[str, bool]:
        return {o: o in ("table:a", "table:c") for o in objects}

    monkeypatch.setattr(fga, "batch_check", _allow_a_c)
    settings = _settings(**_FULL_AUTH)
    repo = _FakeRepo()
    repo.estate = EstateGraph(
        nodes=[GraphNode(id="a"), GraphNode(id="b"), GraphNode(id="c")],
        edges=[
            GraphEdge(source="a", target="c"),  # KEEP: both endpoints visible
            GraphEdge(source="b", target="a"),  # source hidden
            GraphEdge(source="a", target="b"),  # target hidden
        ],
        total=3,
    )
    flt = fga_deps.DatasetFilter(_request(fga=cast(OpenFgaClient, object())), settings, _token())
    result = asyncio.run(estate_graph(cast(LineageRepository, repo), flt, settings))
    assert {n.id for n in result.nodes} == {"a", "c"}
    assert [(e.source, e.target) for e in result.edges] == [("a", "c")]
    assert result.total == 2  # the VISIBLE count — a hidden node is not even counted
    assert result.capped is False


def test_fold_writes_rolls_up_versions_and_failure() -> None:
    # The estate read's per-node badge rollup: distinct versions sorted, failed sticky once any
    # producing run FAILed/ABORTed (case-insensitive), datasets with no writes simply absent.
    from lineage.services.repository import _fold_writes

    rows = [
        ["a", "2", "COMPLETE"],
        ["a", "1", "fail"],
        ["a", "2", "COMPLETE"],  # duplicate version collapses
        ["b", None, "COMPLETE"],  # null version → no version recorded
    ]
    folded = _fold_writes(rows)
    assert folded["a"] == (["1", "2"], True)
    assert folded["b"] == ([], False)
    assert "c" not in folded


def test_estate_graph_caps_honestly_and_deterministically() -> None:
    # Auth off: the cap truncates a name-sorted node list (stable across refreshes), drops edges
    # that leave the window, and reports total/capped truthfully ("N of M", never "the estate").
    from lineage.api.v1.endpoints.discovery import estate_graph

    settings = _settings()
    repo = _FakeRepo()
    repo.estate = EstateGraph(
        nodes=[GraphNode(id="c"), GraphNode(id="a"), GraphNode(id="b")],
        edges=[GraphEdge(source="a", target="b"), GraphEdge(source="a", target="c")],
        total=3,
    )
    flt = fga_deps.DatasetFilter(_request(), settings, None)
    result = asyncio.run(estate_graph(cast(LineageRepository, repo), flt, settings, limit=2))
    assert [n.id for n in result.nodes] == ["a", "b"]  # sorted, then capped
    assert [(e.source, e.target) for e in result.edges] == [("a", "b")]  # a→c left the window
    assert result.total == 3
    assert result.capped is True


def test_get_dataset_columns_drops_edges_touching_hidden_datasets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #24: the column-graph view drops nodes/edges touching a dataset the caller can't see. An edge needs
    # BOTH endpoints visible — a 3-edge fixture proves selectivity in BOTH leak directions (source-hidden
    # AND target-hidden), not just one — same transitive-disclosure guarantee as /graph, at column res.
    from lineage.api.v1.endpoints.columns import get_dataset_columns

    monkeypatch.setattr(fga, "batch_check", _batch_allow_a)  # only "a" visible
    settings = _settings(**_FULL_AUTH)
    repo = _FakeRepo()
    repo.col_graph = ColumnGraph(
        root="a",
        columns=[
            ColumnNode(dataset="a", field="x"),
            ColumnNode(dataset="a", field="z"),
            ColumnNode(dataset="b", field="y"),
        ],
        edges=[
            ColumnEdge(source_dataset="a", source_field="x", target_dataset="a", target_field="z"),  # KEEP
            ColumnEdge(source_dataset="b", source_field="y", target_dataset="a", target_field="x"),  # source hidden
            ColumnEdge(source_dataset="a", source_field="x", target_dataset="b", target_field="y"),  # target hidden
        ],
    )
    flt = fga_deps.DatasetFilter(_request(fga=cast(OpenFgaClient, object())), settings, _token())
    result = asyncio.run(get_dataset_columns("a", cast(LineageRepository, repo), flt, 3))
    assert [n.dataset for n in result.columns] == ["a", "a"]  # b's column dropped, a's two kept
    # only the both-endpoints-visible edge survives; both leak directions are dropped.
    assert [(e.source_field, e.target_field) for e in result.edges] == [("x", "z")]
    # And the depth reached the repository — a walk the route widened but never asked for would
    # govern the right rows of the wrong neighbourhood.
    assert repo.seen_depths["columns"] == 3, f"get_dataset_columns was asked for depth 3 and passed {repo.seen_depths.get('columns')!r}"


def test_get_reconcile_flags_storage_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    # #23: the endpoint wires graph version + on-disk version → drift status. Here the graph says v1 but
    # storage is at v2 (a write that bypassed lineage) → storage_ahead, not in sync.
    from lineage.api.v1.endpoints.reconcile import get_reconcile
    from lineage.schemas import ReconcileState

    settings = _settings()  # gating is route-level; the body just compares the two versions
    repo = _FakeRepo()
    repo.write_version = 1
    repo.uri = "s3://lakehouse/silver/features"
    monkeypatch.setattr("lineage.api.v1.endpoints.reconcile.read_storage_version", lambda _uri, _opts: 2)
    # §9 P1 lifecycle: when storage IS readable, the endpoint also runs the blob-pointer probe and
    # its findings ride the response — the axis the version comparison can't see.
    monkeypatch.setattr("lineage.api.v1.endpoints.reconcile.read_dangling_blob_columns", lambda _uri, _opts: ["thumb"])
    result = asyncio.run(get_reconcile("silver$features", cast(LineageRepository, repo), settings))
    assert result.status is ReconcileState.STORAGE_AHEAD
    assert result.in_sync is False
    assert (result.graph_version, result.storage_version) == (1, 2)
    assert result.dangling_blob_columns == ["thumb"]


def test_get_reconcile_skips_storage_read_without_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    # No recorded source_uri → we can't read storage, so storage_version is None (graph claims a write
    # with nothing readable behind it) without ever touching the object store.
    from lineage.api.v1.endpoints.reconcile import get_reconcile
    from lineage.schemas import ReconcileState

    def _boom(_uri: str, _opts: dict[str, str]) -> int:
        raise AssertionError("storage must not be read when no source_uri is recorded")

    monkeypatch.setattr("lineage.api.v1.endpoints.reconcile.read_storage_version", _boom)
    repo = _FakeRepo()
    repo.write_version = 3
    repo.uri = None
    result = asyncio.run(get_reconcile("g$x", cast(LineageRepository, repo), _settings()))
    assert result.status is ReconcileState.MISSING_ON_STORAGE
    assert result.storage_version is None


def test_get_schema_returns_persisted_fields() -> None:
    # #24: the gated /schema endpoint returns the per-version column schema captured at ingest.
    from lineage.api.v1.endpoints.datasets import get_schema

    result = asyncio.run(get_schema("silver$features", cast(LineageRepository, _FakeRepo()), version=2))
    assert result.dataset == "silver$features"
    assert result.version == 2
    assert [f.name for f in result.fields] == ["id"]


def test_get_runs_filters_to_visible_datasets(monkeypatch: pytest.MonkeyPatch) -> None:
    # #22 audit: /runs is governed like /events — a run is shown only if its output datasets are visible.
    from lineage.api.v1.endpoints.runs import get_runs

    monkeypatch.setattr(fga, "batch_check", _batch_allow_a)
    settings = _settings(**_FULL_AUTH)
    repo = _FakeRepo()
    repo.runs = [RunStatus(run_id="r-a", outputs=["a"]), RunStatus(run_id="r-b", outputs=["b"])]
    flt = fga_deps.DatasetFilter(_request(fga=cast(OpenFgaClient, object())), settings, _token())
    result = asyncio.run(get_runs(cast(LineageRepository, repo), flt, settings))
    assert [r.run_id for r in result.runs] == ["r-a"]  # the run that wrote unseen "b" is dropped


def test_ingest_handler_binds_verified_author() -> None:
    from lineage.api.v1.endpoints.ingest import ingest_event

    repo = _FakeRepo()
    event = _event(claimed_author="attacker")
    asyncio.run(ingest_event(event, _request(), cast(LineageRepository, repo), _settings(), _token("real-user")))
    assert repo.ingested is not None and repo.ingested.author == "real-user"  # body claim overridden


def test_ingest_handler_keeps_body_author_when_oidc_off() -> None:
    from lineage.api.v1.endpoints.ingest import ingest_event

    repo = _FakeRepo()
    event = _event(claimed_author="claimed")
    asyncio.run(ingest_event(event, _request(), cast(LineageRepository, repo), _settings(), None))
    assert repo.ingested is not None and repo.ingested.author == "claimed"


# --------------------------------------------------------------------------- #
# #2 — output-scoped ingest authz: a producer may only record provenance for
# outputs it can WRITE (can_write_data), not just that it's authenticated.
# --------------------------------------------------------------------------- #


def test_output_authz_disabled_is_noop() -> None:
    # FGA off → no check (dev/test default).
    asyncio.run(fga_deps.enforce_output_authz(_event(outputs=["a$b"]), _request(), _settings(), None))


def test_output_authz_no_outputs_is_noop() -> None:
    # An event with no outputs makes no write claim → nothing to authorize.
    asyncio.run(fga_deps.enforce_output_authz(_event(outputs=[]), _request(fga=object()), _settings(**_FULL_AUTH), _token()))


def test_output_authz_unwired_client_fails_closed() -> None:
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(fga_deps.enforce_output_authz(_event(outputs=["a$b"]), _request(), _settings(**_FULL_AUTH), _token()))


def test_output_authz_unauthenticated_raises() -> None:
    with pytest.raises(UnauthenticatedError):
        asyncio.run(fga_deps.enforce_output_authz(_event(outputs=["a$b"]), _request(fga=object()), _settings(**_FULL_AUTH), None))


def test_output_authz_output_less_still_requires_auth() -> None:
    # REGRESSION (bug hunt 2026-07-13): an event with NO outputs must STILL require a valid principal.
    # The pre-fix code short-circuited (`if not outputs: return`) BEFORE the auth checks, so an
    # unauthenticated caller could ingest a forged inputs-only run into the governed audit graph.
    with pytest.raises(UnauthenticatedError):
        asyncio.run(fga_deps.enforce_output_authz(_event(inputs=["a$b"]), _request(fga=object()), _settings(**_FULL_AUTH), None))


def test_output_authz_denies_unreadable_input(monkeypatch: pytest.MonkeyPatch) -> None:
    # REGRESSION (bug hunt 2026-07-13): you may only RECORD reading a dataset you can SEE. An authenticated
    # reader claiming an unreadable input (forging READ-edge provenance like "service-web read gold$catalog")
    # is refused. Output-less + an unreadable input → 403, checked via can_get_metadata.
    async def _batch(_client: object, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        assert relation == "can_get_metadata"
        return dict.fromkeys(objects, False)  # nothing is visible to this caller

    monkeypatch.setattr(fga, "batch_check", _batch)
    with pytest.raises(PermissionDeniedError):
        asyncio.run(
            fga_deps.enforce_output_authz(
                _event(inputs=["gold$catalog"]),
                _request(fga=cast(OpenFgaClient, object())),
                _settings(**_FULL_AUTH),
                _token(),
            )
        )


def test_output_authz_denies_non_writable_output(monkeypatch: pytest.MonkeyPatch) -> None:
    # alice may write a$b but NOT c$d → the whole ingest is denied (403).
    async def _batch(_client: object, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        return {o: (o == "table:a$b") for o in objects}

    monkeypatch.setattr(fga, "batch_check", _batch)
    with pytest.raises(PermissionDeniedError):
        asyncio.run(
            fga_deps.enforce_output_authz(
                _event(outputs=["a$b", "c$d"]),
                _request(fga=cast(OpenFgaClient, object())),
                _settings(**_FULL_AUTH),
                _token(),
            )
        )


def test_output_authz_allows_when_all_outputs_writable(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def _batch(_client: object, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        captured.update(user=user, relation=relation, objects=sorted(objects))
        return dict.fromkeys(objects, True)

    monkeypatch.setattr(fga, "batch_check", _batch)
    asyncio.run(
        fga_deps.enforce_output_authz(
            _event(outputs=["a$b"]),
            _request(fga=cast(OpenFgaClient, object())),
            _settings(**_FULL_AUTH),
            _token(),
        )
    )
    # The write check is on the right subject/relation/objects, not just "some" allow.
    assert captured == {"user": "alice", "relation": "can_write_data", "objects": ["table:a$b"]}


# --------------------------------------------------------------------------- #
# #5 — durable /events feed retention (prune older rows past the cap on ingest).
# --------------------------------------------------------------------------- #


class _FakeConn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def execute(self, sql: str, params: object = None) -> None:
        self.calls.append((sql, params))


class _FakePool:
    """Minimal async pool whose ``connection()`` yields a recording fake conn (no DB)."""

    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def connection(self) -> Any:
        conn = self._conn

        class _Ctx:
            async def __aenter__(self) -> _FakeConn:
                return conn

            async def __aexit__(self, *_a: object) -> bool:
                return False

        return _Ctx()


def _record(repo: LineageRepository) -> None:
    asyncio.run(
        repo.record_event(
            run_id="r",
            event_type="COMPLETE",
            event_time="t",
            job="j",
            author="a",
            inputs=[],
            outputs=["x"],
            event={},
        )
    )


def test_events_retention_prunes_when_set() -> None:
    conn = _FakeConn()
    repo = LineageRepository(cast(Any, _FakePool(conn)), "g", events_retention=5)
    _record(repo)
    assert any("INSERT INTO public.lineage_events" in s for s, _ in conn.calls)
    prune = [p for s, p in conn.calls if "DELETE FROM public.lineage_events" in s]
    assert prune == [(5,)]  # exactly one prune, parameterized with the retention cap


def test_events_retention_unbounded_does_not_prune() -> None:
    conn = _FakeConn()
    repo = LineageRepository(cast(Any, _FakePool(conn)), "g", events_retention=0)
    _record(repo)
    assert not any("DELETE FROM public.lineage_events" in s for s, _ in conn.calls)


# --------------------------------------------------------------------------- #
# #6 — read-audit: log WHO read which dataset on a gated read. Off by default; needs a verified
# subject; best-effort (an audit-write failure must never fail the read it is auditing).
# --------------------------------------------------------------------------- #


class _AuditRepo:
    """Repository stub capturing record_read calls (and able to raise, to prove best-effort)."""

    def __init__(self, *, fail: bool = False) -> None:
        self.reads: list[tuple[str, str]] = []
        self._fail = fail

    async def record_read(self, *, reader: str, dataset: str) -> None:
        if self._fail:
            raise RuntimeError("audit store down")
        self.reads.append((reader, dataset))


def test_audit_read_disabled_does_not_record() -> None:
    # Off by default → not even an authenticated read writes an audit row.
    repo = _AuditRepo()
    asyncio.run(fga_deps.audit_read("a$b", _settings(), _token(), cast(LineageRepository, repo)))
    assert repo.reads == []


def test_audit_read_unauthenticated_does_not_record() -> None:
    # Enabled but no verified subject (OIDC off) → nothing to attribute, so no row.
    repo = _AuditRepo()
    asyncio.run(fga_deps.audit_read("a$b", _settings(read_audit_enabled=True), None, cast(LineageRepository, repo)))
    assert repo.reads == []


def test_audit_read_records_reader_and_dataset_when_enabled() -> None:
    # The audit row is keyed by the VERIFIED subject + the dataset name — who read what.
    repo = _AuditRepo()
    asyncio.run(
        fga_deps.audit_read(
            "silver$features",
            _settings(read_audit_enabled=True),
            _token("dee"),
            cast(LineageRepository, repo),
        )
    )
    assert repo.reads == [("dee", "silver$features")]


def test_audit_read_best_effort_swallows_store_failure() -> None:
    # An audit-write failure must NEVER fail the read it is auditing — the call returns, does not raise.
    repo = _AuditRepo(fail=True)
    asyncio.run(fga_deps.audit_read("a$b", _settings(read_audit_enabled=True), _token(), cast(LineageRepository, repo)))


# --- The SERVICE door on the HTTP ingest (lineage/api/security.py ServicePrincipal) ------------------
# The Ray TRAIN job is a sidecar-less IN-CLUSTER producer (RAY-TRAIN.md D2): it POSTs the HTTP ingest,
# which under auth.enabled 401'd every RunEvent, silently losing ALL training provenance (live
# 2026-07-13). It now authenticates with the shared app token + its bare FGA subject. These tests pin the
# security properties that make that safe — above all: it must NOT become an impersonation primitive.


def _svc_settings(subjects: str = "service-trainer") -> LineageSettings:
    return _settings(**_FULL_AUTH, service_subjects=subjects)


def test_service_principal_authenticates_an_allowlisted_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid app token + an ALLOWLISTED subject → a ServicePrincipal carrying that bare FGA name."""
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    principal = security.authenticate(
        _request(),
        _svc_settings(),
        None,
        dapr_api_token="s3cret",
        x_lance_service_identity="service-trainer",
    )
    assert isinstance(principal, security.ServicePrincipal)
    # The subject is the SAME bare name the rest of the estate uses (D5) — not a second identity axis.
    assert principal.sub == "service-trainer"


def test_service_token_cannot_impersonate_a_human(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE load-bearing guard: a token holder may only speak as an ALLOWLISTED SERVICE, never as a user.

    Without the allowlist this door would let anything holding the app token record provenance as alice
    (and then write whatever alice may write) — a provenance-forgery + privilege-escalation primitive.
    """
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    with pytest.raises(PermissionDeniedError):
        security.authenticate(
            _request(),
            _svc_settings(),
            None,
            dapr_api_token="s3cret",
            x_lance_service_identity="alice",  # a human — not on the allowlist
        )


def test_service_door_rejects_a_bad_or_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wrong token (or a named subject with no token configured) is 401 — never a silent pass."""
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    with pytest.raises(UnauthenticatedError):
        security.authenticate(
            _request(),
            _svc_settings(),
            None,
            dapr_api_token="wrong",
            x_lance_service_identity="service-trainer",
        )
    # No app token configured → the door does not exist; naming a subject must not open it.
    monkeypatch.delenv("APP_API_TOKEN", raising=False)
    with pytest.raises(UnauthenticatedError):
        security.authenticate(
            _request(),
            _svc_settings(),
            None,
            dapr_api_token="s3cret",
            x_lance_service_identity="service-trainer",
        )


def test_service_door_is_shut_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty LINEAGE_SERVICE_SUBJECTS (the default) = OIDC-only ingest: no subject is allowlisted."""
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    with pytest.raises(PermissionDeniedError):
        security.authenticate(
            _request(),
            _settings(**_FULL_AUTH),  # service_subjects defaults to ""
            None,
            dapr_api_token="s3cret",
            x_lance_service_identity="service-trainer",
        )


def test_dapr_stamped_token_without_identity_falls_through_to_oidc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `dapr-api-token` WITHOUT the identity header is a sidecar-stamped invocation, not a service call.

    With ``dapr.io/app-token-secret`` set, the sidecar adds `dapr-api-token` to EVERY request it delivers —
    including a gateway-proxied human carrying a valid OIDC bearer. Gating the service door on the token
    alone diverted those into a guaranteed 403 on the missing identity (audit 2026-07-15). The door must
    open only on a deliberate service call (token + identity); a token-only request takes the OIDC path,
    which here (no bearer supplied) is its usual 401 — never the door's 403, never a silent pass.
    """
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")
    with pytest.raises(UnauthenticatedError, match="Missing bearer token"):
        security.authenticate(
            _request(oidc=SimpleNamespace(verify=lambda _t: _token())),
            _svc_settings(),
            None,
            dapr_api_token="s3cret",
            x_lance_service_identity=None,
        )


def test_service_principal_is_attributed_and_fga_bounded() -> None:
    """The ServicePrincipal flows through BOTH ingest gates: it is stamped as author AND FGA-checked.

    This is what keeps D5 intact end-to-end — the trainer records provenance only for what its rung
    (`writer` on `namespace:models`) actually permits, and is *stricter* than the Dapr subscription route
    it mirrors (which trusts the producer-stamped author outright).
    """
    principal = security.ServicePrincipal("service-trainer")
    event = RunEvent.model_validate(
        {
            "eventType": "COMPLETE",
            "eventTime": "2026-07-13T09:00:00+00:00",
            "producer": "https://github.com/Borg93/lance-ns",
            "run": {"runId": "11111111-1111-5111-8111-111111111111", "facets": {}},
            "job": {"namespace": "ray-jobs", "name": "train.churn"},
            "inputs": [],
            "outputs": [{"namespace": "models", "name": "models$churn", "facets": {}}],
        }
    )
    # (1) attributed — the run is no longer authored by "" (which is what the graph held before)
    fga_deps.enforce_author(event, principal)
    assert event.run.facets["author"] == {"name": "service-trainer", "sub": "service-trainer"}

    # (2) authorized — the FGA check runs as the SERVICE subject, and a denial is a 403
    checked: dict[str, Any] = {}

    async def _batch_check(_client: Any, *, user: str, relation: str, objects: list[str]) -> dict[str, bool]:
        checked.update(user=user, relation=relation, objects=objects)
        return dict.fromkeys(objects, False)  # the trainer may NOT write this output

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fga, "batch_check", _batch_check)
        with pytest.raises(PermissionDeniedError):
            asyncio.run(
                fga_deps.enforce_output_authz(
                    event,
                    _request(fga=cast(OpenFgaClient, object())),
                    _svc_settings(),
                    principal,
                )
            )
    assert checked["user"] == "service-trainer"  # NOT an opaque Dex sub
    assert checked["relation"] == "can_write_data"
    assert checked["objects"] == ["table:models$churn"]


def test_demo_datasets_requires_auth_when_fga_on(monkeypatch: pytest.MonkeyPatch) -> None:
    # AUDIT: /demo/datasets read real medallion schemas/row-counts + gold's lineage JSONB from S3 with the
    # SERVICE root creds and had NO auth gate — any in-cluster caller could exfiltrate them unauthenticated.
    # It now authenticates + governs; with FGA on an unauthenticated request must 401 BEFORE any S3 read.
    from lineage.api.v1.endpoints import demo

    monkeypatch.setattr(demo, "_storage_options", lambda: {})  # isolate the gate from S3
    unauthenticated = fga_deps.DatasetFilter(_request(fga=object()), _settings(**_FULL_AUTH), None)
    with pytest.raises(UnauthenticatedError):
        asyncio.run(demo.demo_datasets(_settings(**_FULL_AUTH), unauthenticated, demo.PeekCache()))


def test_events_governs_on_columnlineage_source_datasets() -> None:
    # AUDIT: /events returned the full raw payload but governed only on top-level inputs/outputs. The
    # columnLineage facet names SOURCE datasets NOT in inputs, so a caller who can see the output would get
    # input datasets' column schemas. The ref-extractor now unions those in (event hidden unless the caller
    # can read every dataset the payload discloses).
    from lineage.api.v1.endpoints.runs import _column_lineage_datasets

    event = {
        "outputs": [
            {
                "name": "gold$t",
                "facets": {"columnLineage": {"fields": {"col": {"inputFields": [{"namespace": "bronze", "name": "bronze$secret"}]}}}},
            }
        ]
    }
    assert _column_lineage_datasets(event) == {"bronze$secret"}
    assert _column_lineage_datasets({}) == set()  # summary=True → no payload → no extra refs, nothing hidden


# ── the public front door must not be able to mint a service principal ────────


def test_the_service_door_REFUSES_the_public_front_door(monkeypatch: pytest.MonkeyPatch) -> None:
    """The laundering path, at the door that guards the authoritative lineage graph.

    The gateway forwards `/api/lineage/**` through Dapr service invocation, and the callee's daprd
    stamps a valid `dapr-api-token` on the way in. `x-lance-service-identity` is caller-supplied. So
    an anonymous public request arrives holding the estate's service credential AND naming an
    allowlisted subject — which `_service_principal` would accept, yielding forged, author-stamped
    RunEvents in the graph, governance tag/description writes, and DLQ replay.

    The gateway strips both headers at the edge; this refuses the door even if one ever gets through,
    because a service principal is never something the public front door should be able to mint.
    """
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")

    with pytest.raises(PermissionDeniedError, match="public front door"):
        security.authenticate(
            _request(),
            _svc_settings(),
            None,
            dapr_api_token="s3cret",
            x_lance_service_identity="service-trainer",
            dapr_caller_app_id="gateway",
        )


def test_a_REAL_service_caller_still_gets_its_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must not sever service-to-service lineage ingest, which is the door's whole job."""
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")

    principal = security.authenticate(
        _request(),
        _svc_settings(),
        None,
        dapr_api_token="s3cret",
        x_lance_service_identity="service-trainer",
        dapr_caller_app_id="medallion",
    )

    assert isinstance(principal, security.ServicePrincipal)
    assert principal.sub == "service-trainer"


def test_the_EMITTER_path_carries_no_caller_id_and_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """`lineage_kit.emitter` posts to RASK_LINEAGE_ENDPOINT over plain Service DNS.

    No Dapr invocation hop means no `dapr-caller-app-id`, so absence must keep working — it is how
    the medallion producer, all three movers and the Ray train job emit. Treating absence as public
    would stop every provenance record in the estate.
    """
    monkeypatch.setenv("APP_API_TOKEN", "s3cret")

    principal = security.authenticate(
        _request(),
        _svc_settings(),
        None,
        dapr_api_token="s3cret",
        x_lance_service_identity="service-trainer",
    )

    assert isinstance(principal, security.ServicePrincipal)


def test_get_events_reports_the_feed_FLOOR_so_a_consumer_can_see_what_it_lost() -> None:
    """`oldest_seq` — the oldest row the feed still holds.

    Lineage prunes its durable feed inline on every ingest, keeping the newest N rows by seq. That
    prune cannot consult any consumer's cursor: the notifications reconciler's mark lives in ITS Dapr
    state store, which lineage is not scoped to and must never be — reaching across would couple the
    producer of the feed to one of its readers.

    So lineage reports its FLOOR and the consumer draws its own conclusion. Without it a reader whose
    mark fell below the floor walks to the end of the feed, sees `next_cursor: None`, and concludes it
    is caught up — the rows in between were deleted before it read them, and nothing anywhere says so.

    UNGOVERNED on purpose: a seq number discloses no dataset, and it is exactly the number a caller
    needs in order to know a page it CANNOT see is missing.
    """
    from lineage.api.v1.endpoints.runs import get_events

    settings = _settings()  # auth off
    repo = _FakeRepo()
    repo.oldest = 8999
    flt = fga_deps.DatasetFilter(_request(), settings, None)
    repo.events = [EventRecord(seq=9000, outputs=["a"], inputs=[], event={})]

    page = asyncio.run(get_events(cast(LineageRepository, repo), flt, settings, limit=2))

    assert page.oldest_seq == 8999


def test_get_events_on_an_EMPTY_feed_reports_no_floor() -> None:
    """`None`, not `0`. A zero would read as "the feed still holds everything from the beginning",
    which is the opposite of what an empty feed means, and would suppress the very gap report a
    consumer needs after a wipe."""
    from lineage.api.v1.endpoints.runs import get_events

    settings = _settings()
    repo = _FakeRepo()
    repo.oldest = None
    flt = fga_deps.DatasetFilter(_request(), settings, None)

    page = asyncio.run(get_events(cast(LineageRepository, repo), flt, settings, limit=2))

    assert page.oldest_seq is None
