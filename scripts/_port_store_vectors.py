"""The store vectors of ``gen_port_vectors.py``: what the Python provenance store decides, through its own functions.

Every group runs the real code of ``lineage.services.provenance`` and ``lakehouse.derived_tables``, with only the
catalog replaced:

- ``schemas``: each of the five tables' schemas as the Arrow IPC stream ``ProvenanceStore.ensure`` sends.
- ``commits``: a batch of admitted events put through ``_recording_of`` and ``ProvenanceStore._commit``, with the table's
  run bindings answered from the case and every write captured: the rows each of the three tables is sent, as an IPC
  stream, and which events were refused as another author's run.
- ``first_of_each``: which rows ``_first_of_each`` keeps of a set of keys.
- ``windows``: ``FeedWindow`` over a feed of the case's row ids, answered in the case's order by a reader that records
  each read: the bounds, every take's seqs, and the reads made.
- ``walks``: ``ProvenanceStore._derived`` over an edge list, and ``derived_tables.is_table_uuid``.
- ``delivery_feed_keys``, ``prune_predicates`` and ``parked_filters``: what a parked delivery would write, and the
  predicates the prune and the parked read send.
"""

from __future__ import annotations

import asyncio
import base64
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch


_T = "2026-11-17T10:00:00Z"
_RECEIVED = datetime(2026, 11, 17, 10, 0, tzinfo=UTC)
_NOW = datetime(2026, 11, 17, 12, 30, 15, 123456, tzinfo=UTC)
_UUID_A = "0190a1b2-0000-7000-8000-00000000000a"
_UUID_B = "0190a1b2-0000-7000-8000-00000000000b"
_UUID_C = "0190a1b2-0000-7000-8000-00000000000c"
_UUID_NS = "0190a1b2-0000-7000-8000-0000000000ff"


def _micros(moment: datetime) -> int:
    return (moment - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1)


def _ipc(table: Any) -> str:  # noqa: ANN401 - a pyarrow table
    from lakehouse.arrow_ipc import encode_arrow_stream

    return base64.b64encode(encode_arrow_stream(table)).decode()


def _schemas() -> list[dict[str, Any]]:
    from lineage.services.provenance import _TABLES

    return [{"table": table, "ipc": _ipc(schema.empty_table())} for table, schema in _TABLES]


def _dataset(catalog: str, name: str, uuid: str | None = None, **lance: Any) -> dict[str, Any]:  # noqa: ANN401 - a facet is arbitrary JSON
    facet = {"tableUuid": uuid, **lance} if uuid else lance
    return {"namespace": catalog, "name": name, "facets": {"rask_lance": facet} if facet else {}}


def _run(
    run_id: str,
    event_type: str = "COMPLETE",
    *,
    author: str | None = None,
    time: str = _T,
    inputs: list[dict[str, Any]] | None = None,
    outputs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    facets = {"rask_author": {"sub": author}} if author else {}
    run = {"runId": run_id, "facets": facets}
    return {
        "eventType": event_type,
        "eventTime": time,
        "run": run,
        "job": {"namespace": "jobs", "name": "copy"},
        "inputs": inputs or [],
        "outputs": outputs or [],
    }


def _commit_cases(catalog: str) -> list[tuple[str, bool, dict[str, str | None], list[tuple[dict[str, Any], list[str]]]]]:
    """Each case: a name, whether foreign runs are refused, the bindings the table holds, and the batch's events with their aliases."""
    a, b, c = _dataset(catalog, "wh$raw$pages", _UUID_A), _dataset(catalog, "wh$clean$pages", _UUID_B), _dataset(catalog, "wh$gold$pages", _UUID_C)
    signed = {"eventTime": _T, "producer": "p", "dataset": _dataset(catalog, "wh$raw$pages", _UUID_A, operation="create_table") | {}}
    signed["dataset"]["facets"]["rask_signature"] = {"sig": "x"}
    posted = {"eventTime": _T, "producer": "p", "dataset": _dataset(catalog, "wh$raw$pages", _UUID_A) | {"facets": {"rask_author": {"sub": "ada"}}}}
    return [
        ("a run's first event founds its binding", True, {}, [(_run("r1", author="ada", inputs=[a], outputs=[b]), [])]),
        (
            "distinct runs and changes in one batch",
            True,
            {},
            [
                (_run("r1", "START", author="ada"), []),
                (_run("r2", author="bob", inputs=[a, c], outputs=[b]), [_UUID_C]),
                (signed, []),
                (posted, [_UUID_B, _UUID_A]),
                (_run("r3", "RUNNING", author="cyd", time="2026-11-17T10:00:01Z"), []),
            ],
        ),
        (
            "a run extended in its batch by another author is refused",
            True,
            {},
            [(_run("r1", "START", author="ada"), []), (_run("r1", author="bob", inputs=[a], outputs=[b]), [])],
        ),
        (
            "a run extended in its batch by another author lands when refusal is off",
            False,
            {},
            [(_run("r1", "START", author="ada"), []), (_run("r1", author="bob", inputs=[a], outputs=[b]), [])],
        ),
        ("a run the table binds to another author is refused", True, {"r1": "ada"}, [(_run("r1", author="bob", inputs=[a], outputs=[b]), [])]),
        ("a run the table binds to its author lands", True, {"r1": "ada"}, [(_run("r1", author="ada", inputs=[a], outputs=[b]), [])]),
        ("a run bound to no author takes any", True, {"r1": None}, [(_run("r1", author="bob", inputs=[a], outputs=[b]), [])]),
        ("an unauthored event extends any run", True, {"r1": "ada"}, [(_run("r1", inputs=[a], outputs=[b]), [])]),
        ("a run founded unauthored takes any author", True, {}, [(_run("r1", "START"), []), (_run("r1", author="bob", inputs=[a], outputs=[b]), [])]),
        (
            "the same event twice is sent once",
            True,
            {},
            [(_run("r1", author="ada", inputs=[a], outputs=[b]), []), (_run("r1", author="ada", inputs=[a], outputs=[b]), [])],
        ),
        (
            "a key repeated keeps its first row",
            True,
            {},
            [
                (_run("r1", author="ada", inputs=[a], outputs=[b]), []),
                (_run("r1", author="ada", time="2026-11-17T11:00:00Z", inputs=[a], outputs=[b]), []),
                (_run("r2", author="ada"), []),
            ],
        ),
        (
            "events that assert no edge send none",
            True,
            {},
            [(_run("r1", "START", author="ada"), []), (_run("r2", "FAIL", author="ada", inputs=[a], outputs=[b]), [])],
        ),
        ("a run with no id binds nothing", True, {}, [(_run("", author="ada", inputs=[a], outputs=[b]), [])]),
        (
            "a refused event beside a landing one",
            True,
            {"r1": "ada"},
            [(_run("r1", author="bob", inputs=[a], outputs=[b]), []), (_run("r2", author="bob", inputs=[b], outputs=[c]), [])],
        ),
        ("a dataset change binds no run", True, {}, [(signed, [_UUID_C])]),
    ]


class _Writer:
    """A table writer that keeps what each table is sent, as an IPC stream."""

    def __init__(self, table: str, written: dict[str, Any]) -> None:
        self._table = table
        self._written = written

    def merge_insert_only(self, rows: Any, *, on: str) -> None:  # noqa: ANN401 - a pyarrow table
        self._written[self._table] = {"on": on, "ipc": _ipc(rows)}


def _commits(catalog: str) -> list[dict[str, Any]]:
    from lineage.models import parse_event
    from lineage.services.provenance import EDGES, FEED, RUNS, ProvenanceStore, _recording_of

    vectors: list[dict[str, Any]] = []
    for name, refuse, bound, events in _commit_cases(catalog):
        store = ProvenanceStore(catalog_url="http://catalog.test", namespace=["ns"], token=lambda: None, refuse_foreign_runs=refuse)
        written: dict[str, Any] = {}

        received = [_RECEIVED + timedelta(microseconds=at) for at in range(len(events))]
        batch = [_recording_of(parse_event(data), received_at=moment, aliased=aliased) for (data, aliased), moment in zip(events, received, strict=True)]
        bindings = dict(bound)
        with (
            patch.object(store, "_authors_of", lambda runs, bindings=bindings: {run: bindings[run] for run in runs if run in bindings}),
            patch.object(store, "_writer", lambda table, written=written: _Writer(table, written)),
        ):
            foreign = store._commit(batch)
        vectors.append(
            {
                "name": name,
                "refuse": refuse,
                "bound": bound,
                "events": [
                    {"data": data, "aliased": aliased, "received_at": _micros(moment)} for (data, aliased), moment in zip(events, received, strict=True)
                ],
                "runs": written.get(RUNS),
                "edges": written.get(EDGES),
                "feed": written.get(FEED),
                "foreign": [index for index, recording in enumerate(batch) if any(recording is refused for refused in foreign)],
            }
        )
    return vectors


def _firsts() -> list[dict[str, Any]]:
    import pyarrow as pa

    from lineage.services.provenance import _first_of_each

    corpus = [["a"], ["a", "a"], ["b", "a", "b", "c", "a"], ["z", "y", "x"], [], ["k", "k", "j", "k", "j", "i"]]
    return [
        {
            "keys": keys,
            "kept": _first_of_each(pa.table({"key": pa.array(keys, pa.string()), "at": pa.array(range(len(keys)), pa.int64())}), "key")["at"].to_pylist(),
        }
        for keys in corpus
    ]


class _Feed:
    """A feed of the given row ids at one version, answering reads in ``order`` and recording each."""

    def __init__(self, ids: list[int], order: str) -> None:
        self.ids = sorted(ids)
        self.order = self.ids[::-1] if order == "descending" else self.ids
        self.calls: list[list[Any]] = []

    def count_rows(self, filter: str | None = None, *, version: int | None = None) -> int:  # noqa: A002 - the reader's own keyword
        if filter is None:
            self.calls.append(["count"])
            return len(self.ids)
        match = re.fullmatch(r"_rowid < (-?\d+) OR _rowid > (-?\d+)", filter)
        if match is None:
            raise ValueError(filter)
        lo, hi = int(match[1]), int(match[2])
        self.calls.append(["outside", lo, hi])
        return sum(1 for row in self.ids if row < lo or row > hi)

    def to_table(self, *, columns: list[str], filter: str | None = None, limit: int | None = None, offset: int | None = None, **_: Any) -> Any:  # noqa: A002, ANN401
        import pyarrow as pa

        if filter is None:
            self.calls.append(["ids", limit, offset])
            ids = self.order[offset or 0 :]
        else:
            match = re.fullmatch(r"_rowid >= (-?\d+) AND _rowid <= (-?\d+)", filter)
            if match is None:
                raise ValueError(filter)
            lo, hi = int(match[1]), int(match[2])
            self.calls.append(["range", lo, hi, limit])
            ids = [row for row in self.order if lo <= row <= hi]
        ids = ids[:limit] if limit is not None else ids
        table = {"_rowid": pa.array(ids, pa.uint64())}
        for column in columns:
            if column in {"inputs", "outputs", "table_uuids", "namespace_uuids", "unresolved_tables"}:
                table[column] = pa.array([[f"{column}-{row}"] for row in ids], pa.list_(pa.string()))
            elif column == "event":
                table[column] = pa.array([f'{{"row": {row}}}' for row in ids], pa.large_string())
            elif column == "received_at":
                table[column] = pa.array([_RECEIVED] * len(ids), pa.timestamp("us", tz="UTC"))
            else:
                table[column] = pa.array([f"{column}-{row}" for row in ids], pa.string())
        return pa.table(table)


def _window_cases() -> list[dict[str, Any]]:
    dense, holes = list(range(10)), [0, 1, 2, 5, 6, 9, 10, 11, 15]
    feeds = (("empty", []), ("dense", dense), ("pruned prefix", list(range(4, 12))), ("holes", holes), ("one row", [7]))
    cursors = ({}, {"since": 0}, {"since": 3}, {"since": 12}, {"after": 6}, {"after": 1}, {"after": 30})
    return [
        {"name": name, "ids": ids, "order": order, **cursor, "summary": takes != [100], "takes": takes}
        for name, ids in feeds
        for order in ("ascending", "descending")
        for cursor in cursors
        for takes in ([3, 3, 3, 3], [100])
    ]


async def _window(case: dict[str, Any]) -> dict[str, Any]:
    from lineage.services.provenance import FeedWindow

    async def run(call: Any) -> Any:  # noqa: ANN401 - the runner's own shape
        return call()

    bounded = _Feed(case["ids"], case["order"])
    bounds = await (await _opened(FeedWindow, run, bounded, {})).bounds()
    feed = _Feed(case["ids"], case["order"])
    cursor = {key: case.get(key) for key in ("since", "after")}
    window = await _opened(FeedWindow, run, feed, cursor | {"summary": case["summary"]})
    pages = [[record.model_dump() for record in await window.take(n)] for n in case["takes"]]
    return case | {
        "bounds": list(bounds) if bounds else None,
        "bounds_calls": sorted(bounded.calls, key=repr),
        "pages": pages,
        "calls": sorted(feed.calls, key=repr),
    }


async def _opened(window: Any, run: Any, feed: _Feed, cursor: dict[str, Any]) -> Any:  # noqa: ANN401
    opened = window(run, feed, version=1, after=cursor.get("after"), since=cursor.get("since"), summary=cursor.get("summary", False))
    await opened.open()
    return opened


def _walk_cases() -> list[dict[str, Any]]:
    def edge(source: str, target: str, name: str | None, at: int) -> dict[str, Any]:
        return {"in_table": source, "out_table": target, "out_name": name, "received_at": at}

    chain = [edge("a", "b", "wh$b", 1), edge("b", "c", "wh$c", 2), edge("c", "d", "wh$d", 3)]
    renamed = [edge(_UUID_A, _UUID_B, "wh$old", 5), edge(_UUID_A, _UUID_B, "wh$new", 9), edge(_UUID_A, _UUID_B, "wh$older", 1)]
    return [
        {"name": "a chain", "edges": chain, "sources": ["a"], "max_depth": 20},
        {"name": "a chain cut short", "edges": chain, "sources": ["a"], "max_depth": 2},
        {"name": "a chain cut at its end", "edges": chain, "sources": ["a"], "max_depth": 3},
        {"name": "depth zero", "edges": chain, "sources": ["a"], "max_depth": 0},
        {"name": "a cycle back to the source", "edges": [*chain, edge("d", "a", "wh$a", 4), edge("c", "b", "wh$b2", 7)], "sources": ["a"], "max_depth": 20},
        {"name": "a diamond, nearest depth kept", "edges": [*chain, edge("a", "d", "wh$d-direct", 1)], "sources": ["a"], "max_depth": 20},
        {"name": "the newest name, ties to the later edge", "edges": [*renamed, edge(_UUID_A, _UUID_B, "wh$tie", 9)], "sources": [_UUID_A], "max_depth": 20},
        {"name": "an edge with no name keeps its key", "edges": [edge(_UUID_A, _UUID_C, None, 1)], "sources": [_UUID_A], "max_depth": 20},
        {"name": "two source keys", "edges": [edge(_UUID_A, "x", "wh$x", 1), edge("wh$a", "y", "wh$y", 1)], "sources": [_UUID_A, "wh$a"], "max_depth": 20},
        {"name": "nothing derived", "edges": chain, "sources": ["zz"], "max_depth": 20},
        {"name": "a source derived from itself", "edges": [edge("a", "a", "wh$a", 1)], "sources": ["a"], "max_depth": 20},
    ]


def _walks() -> list[dict[str, Any]]:
    import pyarrow as pa

    from lineage.services.provenance import ProvenanceStore

    vectors: list[dict[str, Any]] = []
    for case in _walk_cases():
        edges = case["edges"]

        class _Edges:
            def table_version(self) -> int:
                return 3

            def to_table(self, *, columns: list[str], filter: str, version: int) -> Any:  # noqa: A002, ANN401 - the reader's own keyword
                named = set(re.findall(r"'((?:[^']|'')*)'", filter.removeprefix("in_table IN ")))
                rows = [row for row in edges if row["in_table"] in named]  # noqa: B023
                received = [datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=row["received_at"]) for row in rows]
                return pa.table(
                    {
                        "out_table": pa.array([row["out_table"] for row in rows], pa.string()),
                        "out_name": pa.array([row["out_name"] for row in rows], pa.string()),
                        "received_at": pa.array(received, pa.timestamp("us", tz="UTC")),
                    }
                ).select(columns)

        store = ProvenanceStore(catalog_url="http://catalog.test", namespace=["ns"], token=lambda: None, refuse_foreign_runs=False)
        with patch.object(store, "_reader", lambda _table, edges=_Edges: edges()):
            found, complete = store._derived(set(case["sources"]), case["max_depth"])
        vectors.append(case | {"derived": [table.model_dump() for table in found], "complete": complete})
    return vectors


def _uuid_keys() -> list[dict[str, Any]]:
    from lakehouse.derived_tables import is_table_uuid

    keys = [_UUID_A, _UUID_A.upper(), _UUID_A.replace("-", ""), "wh$a$b", "", "{" + _UUID_A + "}", "urn:uuid:" + _UUID_A, _UUID_A + " "]
    return [{"key": key, "uuid": is_table_uuid(key)} for key in keys]


def _delivery_feed_keys(catalog: str) -> list[dict[str, Any]]:
    from lineage.services.provenance import delivery_feed_key

    events = [
        {"data": _run("r1", author="ada")},
        {"data": _run("r1", "RUNNING", author="ada")},
        {"data": {"eventTime": _T, "producer": "p", "dataset": _dataset(catalog, "wh$a", _UUID_A)}},
        {"data": None},
        {"data": "text"},
        {},
        {"data": {"eventType": "COMPLETE"}},
    ]
    return [{"cloud_event": event, "feed_key": delivery_feed_key(event)} for event in events]


def _prune_predicates() -> list[dict[str, Any]]:
    from lineage.services import provenance
    from lineage.services.provenance import ProvenanceStore

    class _Fixed(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> _Fixed:  # noqa: ANN401 - datetime's own signature
            return cls.fromtimestamp(_NOW.timestamp(), tz)

    captured: list[str] = []
    store = ProvenanceStore(catalog_url="http://catalog.test", namespace=["ns"], token=lambda: None, refuse_foreign_runs=False)
    vectors: list[dict[str, Any]] = []
    with patch.object(store, "_prune", lambda predicate: captured.append(predicate) or 0), patch.object(provenance, "datetime", _Fixed):
        for days, through in ((30, None), (0, None), (7, 42), (1, 0), (400, 1)):
            asyncio.run(store.prune_events(days, delivered_through=through))
            vectors.append({"now": _micros(_NOW), "retention_days": days, "delivered_through": through, "predicate": captured[-1]})
    return vectors


def _parked_filters() -> list[dict[str, Any]]:
    import pyarrow as pa

    from lineage.services.provenance import ProvenanceStore

    vectors: list[dict[str, Any]] = []
    for run_id in (None, "r1", "it's"):
        filters: list[str] = []

        class _Refusals:
            def to_table(self, *, columns: list[str], filter: str) -> Any:  # noqa: A002, ANN401 - the reader's own keyword
                filters.append(filter)  # noqa: B023
                return pa.table({column: pa.array([], pa.string()) for column in columns})

        store = ProvenanceStore(catalog_url="http://catalog.test", namespace=["ns"], token=lambda: None, refuse_foreign_runs=False)
        with patch.object(store, "_reader", lambda _table, refusals=_Refusals: refusals()):
            asyncio.run(store.parked(run_id))
        vectors.append({"run_id": run_id, "filter": filters[0]})
    return vectors


def store_vectors() -> dict[str, list[Any]]:
    import logging

    from lineage.core.config import get_settings

    # A feed with holes or answered out of order is read whole, which the store warns of: expected here, every time.
    logging.getLogger("lineage.services.provenance").setLevel(logging.ERROR)
    catalog = get_settings().catalog_dataset_namespace
    return {
        "catalog": [catalog],
        "schemas": _schemas(),
        "commits": _commits(catalog),
        "first_of_each": _firsts(),
        "windows": [asyncio.run(_window(case)) for case in _window_cases()],
        "walks": _walks(),
        "uuid_keys": _uuid_keys(),
        "delivery_feed_keys": _delivery_feed_keys(catalog),
        "prune_predicates": _prune_predicates(),
        "parked_filters": _parked_filters(),
    }
