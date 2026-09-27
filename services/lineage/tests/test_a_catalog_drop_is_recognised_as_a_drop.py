"""LH-144: a catalog drop is recognised as a drop, whatever order its events arrive in.

A catalog drop is a `DatasetEvent` and leaves no run, so run history cannot see it; a table dropped that
way read as live with its grants gone, and the reconcile named it `ungoverned` on every tick. Whether a
dataset is dropped is the newer of its lifecycle stamp (every CREATE or DROP fact) and its newest
data-writing run, a tie going to existence. The graph below applies each lifecycle statement as the
statement reads, and only the guards the statement carries; AGE's own semantics for them were measured
against a scratch graph on AGE 1.5.0.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

import lineage.services.repository as repo_mod
from catalog.core.lineage_emit import InputRef, build_write_event
from lineage.models import MAINTENANCE_OPERATIONS, DatasetEvent, RunEvent, parse_event
from lineage.services import cypher as cy


_TABLE = "acme-bronze$scans"
_AUTHOR = "CgVhbGljZRIFbG9jYWw"


class _Graph:
    """The lifecycle statements applied in memory; every other statement answers empty."""

    def __init__(self) -> None:
        self.lifecycle: dict[str, tuple[str, str]] = {}
        self.source_uri: dict[str, str] = {}
        #: (operation, event_type, event_time) of each run that wrote a dataset, by dataset.
        self.runs: dict[str, list[tuple[str, str, str]]] = {}

    def _newer(self, query: str, name: str, tm: str, state: str) -> bool:
        current = self.lifecycle.get(name)
        if "(d.lifecycle_at IS NULL OR d.lifecycle_at < $tm" not in query or current is None:
            return True
        tie = "(d.lifecycle_at = $tm AND $state = 'CREATE')" in query and current[1] == tm and state == "CREATE"
        return current[1] < tm or tie

    async def run_cypher(self, _conn: object, _graph: str, query: str, params: dict[str, Any], *, columns: int = 1) -> list[list[object]]:
        del columns
        if query == cy.SET_DATASET_LIFECYCLE and self._newer(query, params["name"], params["tm"], params["state"]):
            self.lifecycle[params["name"]] = (params["state"], params["tm"])
            return [[1]]
        if query == cy.SET_OBSERVED_DROP:
            uri_held = "d.source_uri = $uri" not in query or self.source_uri.get(params["name"]) == params["uri"]
            if uri_held and self._newer(query, params["name"], params["tm"], "DROP"):
                self.lifecycle[params["name"]] = ("DROP", params["tm"])
                return [[1]]
        return []

    async def fetch(self, _pool: object, _graph: str, query: str, params: dict[str, Any], *, columns: int = 1) -> list[list[object]]:
        del columns
        if query == cy.DATASET_LIFECYCLE:
            found = self.lifecycle.get(params["name"])
            return [[found[0], found[1]]] if found else []
        if query == cy.DATASET_LAST_EXISTENCE_RUN:
            runs = [r for r in self.runs.get(params["name"], []) if r[1] == "COMPLETE" and r[0] not in MAINTENANCE_OPERATIONS]
            newest = max(runs, key=lambda r: r[2], default=None)
            return [[newest[0], newest[2]]] if newest else []
        return []


class _Conn:
    def __init__(self) -> None:
        self.executed: list[tuple[object, object]] = []

    def transaction(self) -> _Conn:
        return self

    async def __aenter__(self) -> _Conn:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False

    async def execute(self, statement: object, params: object = None) -> None:
        self.executed.append((statement, params))


class _Pool:
    def __init__(self) -> None:
        self.conn = _Conn()

    def connection(self) -> _Conn:
        return self.conn


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> _Graph:
    graph = _Graph()
    monkeypatch.setattr(repo_mod, "run_cypher", graph.run_cypher)
    monkeypatch.setattr(repo_mod, "fetch", graph.fetch)
    return graph


@pytest.fixture
def pool() -> _Pool:
    return _Pool()


@pytest.fixture
def repository(graph: _Graph, pool: _Pool) -> repo_mod.LineageRepository:
    del graph
    return repo_mod.LineageRepository(cast("Any", pool), "g")


def _ingest(repository: repo_mod.LineageRepository, event: RunEvent | DatasetEvent) -> None:
    if isinstance(event, DatasetEvent):
        asyncio.run(repository.ingest_dataset_event(event))
    else:
        asyncio.run(repository.ingest_event(event))


def _catalog(operation: str, at: str, *, inputs: list[InputRef] | None = None) -> RunEvent | DatasetEvent:
    """The catalog's own event for ``operation`` at ``at``: a DatasetEvent for DDL, a RunEvent when it has inputs."""
    return parse_event(
        build_write_event(
            table_id=_TABLE,
            namespace="lance",
            author=_AUTHOR,
            version=None,
            operation=operation,
            run_id=f"{operation}@{at}",
            event_time=at,
            job_namespace="lance",
            inputs=inputs,
        )
    )


def _run(outputs: list[dict[str, Any]], at: str, *, operation: str | None = None, event_type: str = "COMPLETE") -> RunEvent:
    """A producer's run, as any OpenLineage producer writes one."""
    facets = {"lance": {"operation": operation}} if operation else {}
    return RunEvent.model_validate(
        {
            "eventType": event_type,
            "eventTime": at,
            "producer": "https://example.invalid/producer",
            "run": {"runId": "7c3a8d2e-2f0b-4a57-9d5e-2c1f3b9a0e11", "facets": facets},
            "job": {"namespace": "lance", "name": "producer"},
            "outputs": outputs,
        }
    )


def _declaring(state: str, namespace: str = "lance", name: str = _TABLE) -> dict[str, Any]:
    """An output carrying the spec's own lifecycle facet, the way a non-catalog producer says it."""
    return {"namespace": namespace, "name": name, "facets": {"lifecycleStateChange": {"_producer": "p", "_schemaURL": "s", "lifecycleStateChange": state}}}


_T = "2026-09-27T10:0{}:00+00:00"


@pytest.mark.parametrize(
    ("history", "dropped"),
    [
        pytest.param([_catalog("create_table", _T.format(0)), _catalog("drop_table", _T.format(5))], True, id="a-catalog-drop"),
        pytest.param([_catalog("create_table", _T.format(0)), _catalog("deregister_table", _T.format(5))], True, id="a-deregister-leaves-the-catalog"),
        pytest.param([_catalog("drop_table", _T.format(5)), _catalog("create_table", _T.format(6))], False, id="a-recreate"),
        pytest.param([_catalog("drop_table", _T.format(5)), _catalog("register_table", _T.format(6))], False, id="an-undrop"),
        pytest.param(
            [_catalog("drop_table", _T.format(5)), _catalog("create_table", _T.format(6)), _catalog("drop_table", _T.format(5))],
            False,
            id="a-stale-drop-after-the-recreate",
        ),
        pytest.param(
            [
                _catalog("drop_table", _T.format(5)),
                _catalog("register_table", _T.format(6), inputs=[InputRef(namespace="lance", name="acme-bronze$old", version=None)]),
            ],
            False,
            id="a-rename-destination-is-a-run",
        ),
        pytest.param([_catalog("drop_table", _T.format(5)), _catalog("add_columns", _T.format(6))], True, id="a-later-alter-does-not-undrop"),
        pytest.param(
            [_catalog("drop_table", "2026-09-27T10:00:00.900000+00:00"), _catalog("create_table", "2026-09-27T10:00:00Z")],
            True,
            id="an-earlier-create-spelled-otherwise",
        ),
        pytest.param([_catalog("drop_table", _T.format(5)), _catalog("create_table", _T.format(5))], False, id="a-tie-goes-to-existence"),
        pytest.param([_catalog("create_table", _T.format(5)), _catalog("drop_table", _T.format(5))], False, id="a-tie-goes-to-existence-in-either-order"),
        pytest.param([_run([{"namespace": "lance", "name": _TABLE}], _T.format(5), operation="drop_table")], True, id="a-producers-run-shaped-drop"),
        pytest.param(
            [_run([{"namespace": "lance", "name": _TABLE}], _T.format(5), operation="drop_table", event_type="FAIL")], False, id="a-failed-drop-asserts-nothing"
        ),
        pytest.param([_catalog("drop_table", _T.format(5)), _run([_declaring("OVERWRITE")], _T.format(6))], False, id="the-spec-facet-says-it-exists"),
        pytest.param([_catalog("create_table", _T.format(5)), _run([_declaring("DROP")], _T.format(6))], True, id="the-spec-facet-says-it-is-gone"),
    ],
)
def test_the_newest_existence_fact_decides_whether_a_table_is_dropped(
    repository: repo_mod.LineageRepository, history: list[RunEvent | DatasetEvent], dropped: bool
) -> None:
    for event in history:
        _ingest(repository, event)

    assert (asyncio.run(repository.dropped_at(_TABLE)) is not None) is dropped


@pytest.mark.parametrize(
    ("runs", "stamped", "dropped"),
    [
        pytest.param([("drop_table", "COMPLETE", _T.format(5))], [], True, id="a-drop-only-run-history-holds"),
        pytest.param([("insert", "COMPLETE", _T.format(6))], [_catalog("drop_table", _T.format(5))], False, id="a-write-after-the-drop-proves-it-is-back"),
        pytest.param([("compaction", "COMPLETE", _T.format(6))], [_catalog("drop_table", _T.format(5))], True, id="a-compaction-after-the-drop-does-not"),
        pytest.param([("drop_table", "COMPLETE", _T.format(6))], [_catalog("create_table", _T.format(5))], True, id="a-drop-run-after-the-create"),
        pytest.param(
            [("insert", "COMPLETE", _T.format(5))], [_catalog("drop_table", _T.format(5))], False, id="a-tie-between-the-stamp-and-a-run-goes-to-existence"
        ),
    ],
)
def test_run_history_is_the_second_source(
    repository: repo_mod.LineageRepository, graph: _Graph, runs: list[tuple[str, str, str]], stamped: list[RunEvent | DatasetEvent], dropped: bool
) -> None:
    """Run history holds every drop recorded before the stamp existed, and a later write proves a table is back."""
    graph.runs[_TABLE] = runs
    for event in stamped:
        _ingest(repository, event)

    assert (asyncio.run(repository.dropped_at(_TABLE)) is not None) is dropped


def test_the_run_query_excludes_exactly_the_maintenance_operations() -> None:
    """The Cypher literal and the Python set are one list in two places; this keeps them one list."""
    listed = cy.DATASET_LAST_EXISTENCE_RUN.split("NOT r.operation IN [", 1)[1].split("]", 1)[0]

    assert {item.strip().strip("'") for item in listed.split(",")} == MAINTENANCE_OPERATIONS


@pytest.mark.parametrize(
    "at",
    [
        pytest.param("2026-09-27T10:05:00", id="zone-less"),
        pytest.param("yesterday", id="unparseable"),
        pytest.param((datetime.now(UTC) + timedelta(days=1)).isoformat(), id="dated-a-day-ahead"),
    ],
)
def test_a_fact_whose_time_cannot_be_ordered_is_not_stamped(repository: repo_mod.LineageRepository, graph: _Graph, at: str) -> None:
    """Refused, not raised: the rest of the ingest (the graph write and the feed row) still lands."""
    _ingest(repository, _catalog("drop_table", at))

    assert graph.lifecycle == {}


def test_the_stamp_lands_on_the_vertex_every_other_statement_writes(repository: repo_mod.LineageRepository, graph: _Graph) -> None:
    """An output in a URI namespace is a namespace-qualified vertex; the stamp must key on that same name."""
    event = _run([_declaring("DROP", namespace="s3://bucket", name="x")], _T.format(5))

    _ingest(repository, event)

    assert list(graph.lifecycle) == [event.outputs[0].vertex_name]
    assert event.outputs[0].vertex_name != "x", "this output is not namespace-qualified, so the case tests nothing"


def test_an_observed_drop_is_recorded_at_the_location_it_read(repository: repo_mod.LineageRepository, graph: _Graph, pool: _Pool) -> None:
    graph.source_uri[_TABLE] = "s3://bucket/scans"

    recorded = asyncio.run(repository.record_observed_drop(_TABLE, "s3://bucket/scans", "2026-09-27T10:05:00.000000+00:00"))

    assert recorded
    assert asyncio.run(repository.dropped_at(_TABLE)) is not None
    [(_sql, params)] = pool.conn.executed
    assert "absent on storage" in json.dumps(params, default=str), "the feed row does not carry the observation"


@pytest.mark.parametrize(
    ("uri", "newer"),
    [
        pytest.param("s3://bucket/moved", None, id="the-source-moved-during-the-read"),
        pytest.param("s3://bucket/scans", _T.format(6), id="a-recreate-landed-during-the-read"),
    ],
)
def test_an_observed_drop_the_graph_overtook_is_not_recorded(
    repository: repo_mod.LineageRepository, graph: _Graph, pool: _Pool, uri: str, newer: str | None
) -> None:
    """Nothing stamped and no feed row: a blocked observation must not append a DROP on every tick."""
    graph.source_uri[_TABLE] = uri
    if newer:
        _ingest(repository, _catalog("create_table", newer))
        pool.conn.executed.clear()

    recorded = asyncio.run(repository.record_observed_drop(_TABLE, "s3://bucket/scans", "2026-09-27T10:05:00.000000+00:00"))

    assert not recorded
    assert asyncio.run(repository.dropped_at(_TABLE)) is None
    assert pool.conn.executed == []
