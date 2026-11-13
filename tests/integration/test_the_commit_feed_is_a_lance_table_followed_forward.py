"""The commit feed is a Lance table the catalog governs, and a follower reads it FORWARD (LH-468, ADR 0158).

Lineage keeps no database: every event it admits is a row of `provenance$events`, written through the
catalog's own `merge_insert` door and read back through its `query` door, and `GET /events?since=` pages that
table oldest first. Driven over real sockets against the real catalog app on a real `dir` namespace, because
the claim is that lineage's store IS the catalog: a live uvicorn catalog announcing each commit over HTTP to
lineage's own app, served beside it, which records it back into that catalog. Recording a commit is itself a
commit, so a catalog that announced its provenance namespace's writes would feed lineage its own rows for ever.

What stands in is `lineage_app`'s: OpenFGA answering from a granted set, and the verified caller named in a
header. The visibility filter is the claim of the second test, and neither stand-in is a claim about the store.

A compaction of a feed table is no recorded event's commit, so the catalog announces it as it announces any table's
([[LH-529]]): lineage records it once, and recording it is a row write the catalog keeps to itself, so it ends there.

A page reads only its own rows ([[LH-590]]): the catalog's traffic for a page is counted at the store's transport, the
one seam every feed read goes through, and a store answering its queries in another order stands in at the same seam.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import lance
import lance_namespace
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient
from lance.optimize import Compaction

from lakehouse.arrow_ipc import decode_arrow_response
from lakehouse.lancekit.reader import CatalogTableReader, RestCatalogTransport
from lakehouse.lancekit.writer import RestCatalogWriteTransport
from lakehouse.lineage_events import DatasetUuids, with_dataset_uuids
from lineage.models import parse_event
from lineage.services.provenance import FEED, ProvenanceStore
from tests.integration.lineage_app import SUBJECT_HEADER, ensure_tables, governed_lineage, live_catalog, live_lineage, run_event
from tests.loopback import free_port


if TYPE_CHECKING:
    from lance_namespace_urllib3_client import QueryTableRequest


#: The pipeline reading the feed, under its own service account.
_PIPELINE = "kubernetes~system:serviceaccount:etl:etl-runner"
_ARROW = {"content-type": "application/vnd.apache.arrow.stream"}

#: The tables the pipeline may see. `acme-silver$hidden` and `globex-silver$orders` are someone else's.
_VISIBLE = ("acme-silver$pages", "acme-gold$catalog", "acme-gold$index")
_TABLES = (*_VISIBLE, "acme-silver$hidden", "globex-silver$orders")


def _rows(start: int, stop: int) -> bytes:
    table = pa.table({"id": pa.array(range(start, stop), pa.int64())})
    sink = pa.BufferOutputStream()
    with ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _follow(client: httpx.Client, headers: Mapping[str, str] | None = None, **filters: str) -> list[dict[str, Any]]:
    """Walk the feed the way a pipeline does: poll from the last cursor until a poll brings nothing new."""
    seen: list[dict[str, Any]] = []
    cursor = 0
    for _ in range(20):
        response = client.get("/events", params={"since": cursor, "limit": 2, **filters}, headers=dict(headers or {}))
        assert response.status_code == 200, response.text
        page = response.json()
        seen += page["events"]
        if not page["events"] and page["next_cursor"] == cursor:
            return seen
        assert page["next_cursor"] > cursor, f"the cursor must move forward or the walk is over: {page}"
        cursor = page["next_cursor"]
    raise AssertionError(f"the walk did not settle: {seen}")


def _operation(event: dict[str, Any]) -> str:
    """The catalog operation an announcement names: on its run, or on the dataset of a DDL change, which has no run."""
    facets = event["run"]["facets"] if "run" in event else event["dataset"]["facets"]
    return facets["rask_lance"]["operation"]


def _compact_the_feed(catalog_url: str) -> None:
    """Compact ``provenance$events`` as a bring-your-own job does, through the catalog's doors with Lance's own protocol."""
    feed = lance.dataset(namespace_client=lance_namespace.connect("rest", {"uri": catalog_url}), table_id=["provenance", "events"])
    plan = Compaction.plan(feed, {"target_rows_per_fragment": 1024})
    Compaction.commit(feed, [task.execute(feed) for task in plan.tasks])


def test_each_catalog_commit_is_followed_forward_once_and_in_commit_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    port = free_port()
    announced = {"LANCE_LINEAGE_EMIT_ENABLED": "true", "LANCE_LINEAGE_TRANSPORT": "http", "LANCE_LINEAGE_URL": f"http://127.0.0.1:{port}/api/v1/lineage"}
    with (
        live_catalog(tmp_path / "catalog", monkeypatch, **announced) as catalog_url,
        live_lineage(catalog_url, monkeypatch, port=port) as lineage_url,
        httpx.Client(base_url=catalog_url, timeout=60) as catalog,
        httpx.Client(base_url=lineage_url, timeout=60) as lineage,
    ):
        assert catalog.post("/v1/namespace/acme-silver/create", json={}).status_code == 200
        created = catalog.post("/v1/table/acme-silver$pages/create", content=_rows(0, 3), headers=_ARROW)
        assert created.status_code == 200, created.text
        inserted = catalog.post("/v1/table/acme-silver$pages/insert", content=_rows(3, 5), headers=_ARROW)
        assert inserted.status_code == 200, inserted.text
        # What the outbox relay sends when it recovers an event whose publish was never acknowledged.
        redelivered = lineage.post("/api/v1/lineage", json=_follow(lineage)[-1]["event"])
        assert redelivered.status_code == 201, redelivered.text
        _compact_the_feed(catalog_url)

        seen = _follow(lineage)
        # A cursor this feed never issued, as a follower holds one after the store is moved or restored.
        ahead = lineage.get("/events", params={"since": 121_000}).json()

    assert [(row["outputs"], _operation(row["event"])) for row in seen] == [
        (["acme-silver$pages"], "create_table"),
        (["acme-silver$pages"], "insert"),
        # The feed's own compaction is announced as any table's commit is, once per commit, and recording it is a row
        # write the catalog keeps to itself, so the feed ends there.
        (["provenance$events"], "compact_table"),
        (["provenance$events"], "compact_table"),
    ]
    seqs = [row["seq"] for row in seen]
    assert seqs == sorted(set(seqs)), seqs
    assert (ahead["events"], ahead["next_cursor"]) == ([], seqs[-1]), "a cursor this feed never issued must be answered with its newest seq"


def _declared(catalog_url: str, tables: tuple[str, ...]) -> dict[str, DatasetUuids]:
    """Declare each of ``tables`` (and its namespace) in the catalog, and answer the uuids the catalog minted for each and
    its namespace: the object its grants are held by, and what a feed row names it by ([[LH-524]])."""
    with httpx.Client(base_url=catalog_url, timeout=60) as catalog:

        def minted(kind: str, name: str) -> str:
            return str(catalog.post(f"/v1/{kind}/{name}/describe", json={}).json()["properties"]["rask.uuid"])

        namespaces = list(dict.fromkeys(table.split("$")[0] for table in tables))
        for namespace in namespaces:
            assert catalog.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
        for table in tables:
            assert catalog.post(f"/v1/table/{table}/declare", json={}).status_code == 200
        namespace_uuid = {namespace: minted("namespace", namespace) for namespace in namespaces}
        return {table: DatasetUuids(table_uuid=minted("table", table), namespace_uuid=namespace_uuid[table.split("$")[0]]) for table in tables}


def _stamped(event: dict[str, Any], uuid_of: Mapping[str, DatasetUuids]) -> dict[str, Any]:
    """``event`` with each dataset it names carrying its table's and namespace's uuids, as the catalog's own events carry
    them (ADR 0173 §10)."""

    def stamp(datasets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [with_dataset_uuids(d, uuid_of[d["name"]], producer="https://example.invalid/etl") for d in datasets]

    return {**event, "inputs": stamp(event["inputs"]), "outputs": stamp(event["outputs"])}


@pytest.fixture
def governed_feed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Lineage's router behind its real authentication-then-FGA gate, over a feed the store wrote to the catalog."""
    feed = [
        run_event(str(uuid.uuid4()), "COMPLETE", "2026-10-15T10:00:01+00:00", outputs=["acme-silver$pages"]),
        run_event(str(uuid.uuid4()), "COMPLETE", "2026-10-15T10:00:02+00:00", outputs=["acme-silver$hidden"]),
        run_event(str(uuid.uuid4()), "COMPLETE", "2026-10-15T10:00:03+00:00", outputs=["acme-gold$catalog"], inputs=("acme-silver$pages",)),
        run_event(str(uuid.uuid4()), "COMPLETE", "2026-10-15T10:00:04+00:00", outputs=["globex-silver$orders"]),
        run_event(str(uuid.uuid4()), "COMPLETE", "2026-10-15T10:00:05+00:00", outputs=["acme-gold$index"]),
        run_event(str(uuid.uuid4()), "COMPLETE", "2026-10-15T10:00:06+00:00", outputs=["acme-gold$catalog"], inputs=("acme-silver$hidden",)),
    ]
    with live_catalog(tmp_path / "catalog", monkeypatch) as url:
        uuid_of = _declared(url, _TABLES)
        granted = {(_PIPELINE, "can_get_metadata", f"table:{uuid_of[name].table_uuid}") for name in _VISIBLE}
        with governed_lineage(url, monkeypatch, granted) as client:
            store = client.app.state.provenance

            async def _seed() -> None:
                for event in feed:
                    await store.record(parse_event(_stamped(event, uuid_of)))

            asyncio.run(_seed())
            yield client


def test_a_table_or_namespace_filter_narrows_the_feed_and_hidden_events_stay_hidden(governed_feed: TestClient) -> None:
    def outputs(rows: list[dict[str, Any]]) -> list[list[str]]:
        return [row["outputs"] for row in rows]

    caller = {SUBJECT_HEADER: _PIPELINE}
    assert outputs(_follow(governed_feed, caller)) == [["acme-silver$pages"], ["acme-gold$catalog"], ["acme-gold$index"]]
    assert outputs(_follow(governed_feed, caller, table="acme-silver$pages")) == [["acme-silver$pages"], ["acme-gold$catalog"]]
    assert outputs(_follow(governed_feed, caller, namespace="acme-gold")) == [["acme-gold$catalog"], ["acme-gold$index"]]
    assert _follow(governed_feed, caller, table="acme-silver$hidden") == []


def _recorded(store: ProvenanceStore, events: list[dict[str, Any]]) -> None:
    """Record ``events`` concurrently, as the bus delivers them, so they land in a few group commits."""

    async def burst() -> None:
        await asyncio.gather(*(store.record(parse_event(event)) for event in events))

    asyncio.run(burst())


class _CatalogTraffic:
    """What the store's transport asks of the catalog: one entry per call, its kind and the rows a query answered."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[tuple[str, int]] = []
        query, count, version = RestCatalogTransport.query, RestCatalogTransport.count, RestCatalogTransport.table_version

        def counted_query(transport: RestCatalogTransport, request: QueryTableRequest) -> bytes:
            answer = query(transport, request)
            self.calls.append(("query", decode_arrow_response(answer).num_rows))
            return answer

        def counted_count(transport: RestCatalogTransport, table_id: list[str], filter: str | None, *, version: int | None = None) -> int:
            self.calls.append(("count", 0))
            return count(transport, table_id, filter, version=version)

        def counted_version(transport: RestCatalogTransport, table_id: list[str]) -> int:
            self.calls.append(("describe", 0))
            return version(transport, table_id)

        monkeypatch.setattr(RestCatalogTransport, "query", counted_query)
        monkeypatch.setattr(RestCatalogTransport, "count", counted_count)
        monkeypatch.setattr(RestCatalogTransport, "table_version", counted_version)

    def of(self, page: Callable[[], object]) -> list[tuple[str, int]]:
        """The calls ``page`` made and the rows each answered, sorted, since a window's first two calls run concurrently and
        land in either order: a page's cost."""
        self.calls.clear()
        page()
        return sorted(self.calls)


def test_a_page_costs_the_same_whatever_the_feeds_length(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A page reads a bounded number of catalog queries, none of them a scan of every row, newest first, after a cursor
    and forward from one, under the governance filter ([[LH-590]])."""

    def visible(at: str) -> dict[str, Any]:
        return run_event(str(uuid.uuid4()), "COMPLETE", at, outputs=["acme-silver$pages"])

    def hidden(at: str) -> dict[str, Any]:
        return run_event(str(uuid.uuid4()), "COMPLETE", at, outputs=["globex-silver$orders"])

    with live_catalog(tmp_path / "catalog", monkeypatch) as url:
        uuid_of = _declared(url, _TABLES)
        granted = {(_PIPELINE, "can_get_metadata", f"table:{uuid_of[name].table_uuid}") for name in _VISIBLE}
        with governed_lineage(url, monkeypatch, granted) as client:
            store = client.app.state.provenance
            caller = {SUBJECT_HEADER: _PIPELINE}
            traffic = _CatalogTraffic(monkeypatch)

            def costs() -> list[list[tuple[str, int]]]:
                newest = client.get("/events", params={"limit": 1, "summary": "true"}, headers=caller).json()["events"][0]["seq"]
                return [
                    traffic.of(lambda: client.get("/events", params={"limit": 1, "summary": "true"}, headers=caller)),
                    traffic.of(lambda: client.get("/events", params={"limit": 1, "after": newest}, headers=caller)),
                    traffic.of(lambda: client.get("/events", params={"limit": 1, "since": 0}, headers=caller)),
                ]

            def stamped(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
                return [_stamped(event, uuid_of) for event in events]

            _recorded(store, stamped([visible("2026-10-15T10:00:01+00:00"), hidden("2026-10-15T10:00:02+00:00")]))
            _recorded(
                store,
                stamped([hidden(f"2026-10-15T10:01:{n:02d}+00:00") if n % 3 else visible(f"2026-10-15T10:01:{n:02d}+00:00") for n in range(10)]),
            )
            _recorded(store, stamped([visible("2026-10-15T10:00:03+00:00")]))
            _recorded(store, stamped([visible("2026-10-15T10:00:04+00:00")]))
            _recorded(store, stamped([hidden("2026-10-15T10:00:05+00:00")]))
            short = costs()
            _recorded(store, stamped([hidden(f"2026-10-15T11:00:{n:02d}+00:00") if n % 3 else visible(f"2026-10-15T11:00:{n:02d}+00:00") for n in range(60)]))
            _recorded(store, stamped([visible("2026-10-15T12:00:01+00:00")]))
            _recorded(store, stamped([visible("2026-10-15T12:00:02+00:00")]))
            _recorded(store, stamped([hidden("2026-10-15T12:00:03+00:00")]))
            long = costs()

    assert long == short, f"a page of a 78-row feed cost {long}, of a 17-row feed {short}"


def _newest_first(query: Callable[[RestCatalogTransport, QueryTableRequest], bytes]) -> Callable[[RestCatalogTransport, QueryTableRequest], bytes]:
    """A catalog whose query answers its rows newest first: what the spec leaves a store free to do (`QueryTableRequest`
    names no order)."""

    def answered(transport: RestCatalogTransport, request: QueryTableRequest) -> bytes:
        whole = decode_arrow_response(query(transport, request.model_copy(update={"k": 10_000, "offset": None})))
        ordered = whole.sort_by([("_rowid", "descending")]) if "_rowid" in whole.column_names else whole
        page = ordered.slice(request.offset or 0, request.k)
        sink = pa.BufferOutputStream()
        with ipc.new_file(sink, page.schema) as writer:
            writer.write_table(page)
        return sink.getvalue().to_pybytes()

    return answered


@pytest.mark.parametrize("newest_first", [pytest.param(False, id="as-the-catalog-answers"), pytest.param(True, id="a-store-answering-newest-first")])
def test_every_cursor_pages_the_rows_a_pruned_feed_with_holes_holds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, newest_first: bool) -> None:
    """Every page answers the rows, in the order, its cursor selects: newest first, older than ``after`` and forward from
    ``since``, over a feed a prune cut and deletes left holes in, whatever order the store answers a query in."""
    with live_catalog(tmp_path / "catalog", monkeypatch) as url:
        store = ProvenanceStore(catalog_url=url, namespace=["provenance"], token=lambda: None, refuse_foreign_runs=False)
        ensure_tables(store)
        for burst in range(3):
            _recorded(
                store, [run_event(str(uuid.uuid4()), "COMPLETE", f"2026-10-15T1{burst}:00:{n:02d}+00:00", outputs=["acme-silver$pages"]) for n in range(8)]
            )
        RestCatalogWriteTransport(url, ["provenance", FEED]).delete("_rowid < 3 OR _rowid = 4 OR (_rowid >= 10 AND _rowid <= 15) OR _rowid = 20")
        held = CatalogTableReader(RestCatalogTransport(url), ["provenance", FEED]).to_table(columns=["received_at"], with_row_id=True)
        seqs = sorted(int(row_id) + 1 for row_id in held.column("_rowid").to_pylist())
        if newest_first:
            monkeypatch.setattr(RestCatalogTransport, "query", _newest_first(RestCatalogTransport.query))

        def page(*, after: int | None = None, since: int | None = None) -> list[int]:
            return [record.seq for record in asyncio.run(store.list_events(3, after=after, since=since))]

        answered = {
            "newest": page(),
            "bounds": asyncio.run(store.feed_bounds()),
            "since": {cursor: page(since=cursor) for cursor in range(seqs[-1] + 2)},
            "after": {cursor: page(after=cursor) for cursor in range(1, seqs[-1] + 3)},
        }

    assert answered == {
        "newest": seqs[::-1][:3],
        "bounds": (seqs[0], seqs[-1]),
        "since": {cursor: [seq for seq in seqs if seq > cursor][:3] for cursor in range(seqs[-1] + 2)},
        "after": {cursor: [seq for seq in seqs[::-1] if seq < cursor][:3] for cursor in range(1, seqs[-1] + 3)},
    }
