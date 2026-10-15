"""The commit feed and the table-level edge list, kept as Lance tables the catalog governs (ADR 0158, LH-468).

Lineage keeps no database and holds no storage credential for these tables. They live in the catalog's
provenance namespace, which the catalog keeps for them and never announces a write to (an announcement of a
feed write would be ingested as a feed write, without end). Every row lands through the catalog's own
``merge_insert`` door and is read back through its ``query`` door, as this service's own principal, so a
write is authorized, judged and audited like any other writer's.

THE FEED'S SEQUENCE IS LANCE'S OWN. Its tables are created with stable row ids, and Lance assigns a row's id
from the manifest's ``next_row_id`` when its commit lands, re-assigning it when a racing commit wins first
(`lance_docs/file_format.md`, "Row ID Assignment"). So ``_rowid`` order is commit order: measured on pylance
13.0.0, four writers racing 40 keyed appends left the row-id order equal to the created-at-version order.
A follower reading ``seq > since`` therefore never misses a row that commits after it read. ``seq`` is
``_rowid + 1``, so the first row is 1 and ``since=0`` reads from the start.

A ROW IS WRITTEN ONCE. Each table declares an unenforced primary key and is written with an insert-only
merge on it, which the catalog runs on Lance's own enforcement: a redelivered event lands no second row, and
two racing deliveries of one event land it once (``catalog.services.primary_keys``). The feed keeps one row
per terminal state of a run, one per arrival of a non-terminal state, and one per static change.

A READ IS TWO QUERIES, pinned to one version: the keys of the rows the cursor selects, then the rows between
the first and last of them. Lance evaluates a ``_rowid`` filter on the manifest's row-id sequences without
reading a data file (measured on pylance 13.0.0: the plan's first read projects no column), so a follower at
the head of the feed reads only the rows after its cursor. A read with no cursor, and ``feed_bounds``, read
every retained row's key, which the feed's retention bounds.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final, TypeVar

import pyarrow as pa

from lineage.core.config import LineageSettings
from lineage.models import Dataset, DatasetEvent, RunEvent, author_sub_from_payload
from lineage.schemas import DerivedTable, EventRecord
from service_kit.governed.machine_identity import read_identity_token
from service_kit.lakehouse.naming import CATALOG_DELIMITER
from service_kit.lancekit.arrow_ipc import encode_arrow_stream
from service_kit.lancekit.catalog_client import catalog_api_client, request_headers
from service_kit.lancekit.reader import CatalogTableReader, RestCatalogTransport, translate_catalog_errors
from service_kit.lancekit.writer import RestCatalogWriteTransport


log = logging.getLogger(__name__)

_T = TypeVar("_T")

#: The field metadata Lance reads a table's unenforced primary key from (`lance_docs/file_format.md`, "Unenforced Primary Key").
_PRIMARY_KEY: Final = {"lance-schema:unenforced-primary-key": "true"}

#: The states a run ends in. The feed keeps the FIRST row of each, whatever its event time, so a terminal event a
#: producer re-sends with a fresh time is one row; a non-terminal state keeps every arrival, its progress trail.
TERMINAL_STATES: Final = frozenset({"COMPLETE", "FAIL", "ABORT"})

FEED: Final = "events"
EDGES: Final = "edges"
REFUSALS: Final = "refusals"

FEED_SCHEMA: Final = pa.schema(
    [
        pa.field("event_key", pa.string(), nullable=False, metadata=_PRIMARY_KEY),
        pa.field("run_id", pa.string()),
        pa.field("event_type", pa.string()),
        pa.field("event_time", pa.string()),
        pa.field("job", pa.string()),
        pa.field("author", pa.string()),
        pa.field("inputs", pa.list_(pa.string())),
        pa.field("outputs", pa.list_(pa.string())),
        pa.field("event", pa.large_string()),
        # The estate's clock, never the producer's `eventTime`: retention reads it, and a skewed producer stamp
        # would strand a row for ever or delete a fresh one.
        pa.field("received_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)

#: One row per (run, input, output) a successful run asserts. `in_table` and `out_table` are the dataset's
#: identity under `Dataset.lineage_name`: a governed table's bare catalog id, an external source qualified by its
#: namespace, so an external input can never name a governed table's edges ([[LIN-3]]).
EDGE_SCHEMA: Final = pa.schema(
    [
        pa.field("edge_key", pa.string(), nullable=False, metadata=_PRIMARY_KEY),
        pa.field("run_id", pa.string()),
        pa.field("writer_run_id", pa.string()),
        pa.field("author", pa.string()),
        pa.field("in_table", pa.string(), nullable=False),
        pa.field("in_version", pa.int64()),
        pa.field("in_branch", pa.string()),
        pa.field("out_table", pa.string(), nullable=False),
        pa.field("out_version", pa.int64()),
        pa.field("out_branch", pa.string()),
        pa.field("event_time", pa.string()),
        pa.field("received_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)

#: The outbox's terminal state for a governance refusal ([[LH-182]]): the staged object is retired only once its
#: verdict and payload are recorded here, because the object is the event's only durable copy.
REFUSAL_SCHEMA: Final = pa.schema(
    [
        pa.field("outbox_key", pa.string(), nullable=False, metadata=_PRIMARY_KEY),
        pa.field("run_id", pa.string()),
        pa.field("author", pa.string()),
        pa.field("reason", pa.string(), nullable=False),
        pa.field("event", pa.large_string(), nullable=False),
        pa.field("refused_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)

#: Where each follower of the feed this service drives left it: the forwarder's cursor, keyed by the endpoint it posts to.
CURSORS: Final = "cursors"
CURSOR_SCHEMA: Final = pa.schema(
    [
        pa.field("follower", pa.string(), nullable=False, metadata=_PRIMARY_KEY),
        pa.field("seq", pa.int64(), nullable=False),
        pa.field("updated_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)

#: Who each run belongs to: the verified author of its first recorded event, the one subject who may extend it ([[LIN-5]]).
#: NEVER PRUNED, unlike the feed. A run id stays public in every graph backend the feed was forwarded to, so a binding that
#: left with the feed's retention would hand an old run to whoever posted its id next. One row per run.
RUNS: Final = "runs"
RUN_SCHEMA: Final = pa.schema(
    [
        pa.field("run_id", pa.string(), nullable=False, metadata=_PRIMARY_KEY),
        pa.field("author", pa.string()),
        pa.field("received_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)

_TABLES: Final = ((FEED, FEED_SCHEMA), (EDGES, EDGE_SCHEMA), (REFUSALS, REFUSAL_SCHEMA), (CURSORS, CURSOR_SCHEMA), (RUNS, RUN_SCHEMA))

#: How many derivation hops `derived` follows. A medallion is three tiers, so a longer chain is a cycle the walk's
#: visited set already stops or an estate whose answer says it stopped short (`complete`).
MAX_DERIVED_DEPTH: Final = 20
#: Input names per edge query, so a wide frontier is read in bounded filters.
_IN_LIST: Final = 500
_FEED_COLUMNS: Final = ["event_type", "event_time", "job", "author", "inputs", "outputs", "event"]
_SUMMARY_COLUMNS: Final = ["event_type", "event_time", "job", "author", "inputs", "outputs"]


def feed_key(event: RunEvent | DatasetEvent) -> str:
    """The feed row's identity: the run and its terminal state, a non-terminal state and when it arrived, or a static change's derived id."""
    if isinstance(event, DatasetEvent):
        return f"{event.feed_id}@{event.feed_event_type}"
    if event.event_type in TERMINAL_STATES:
        return f"{event.run.run_id}@{event.event_type}"
    return f"{event.run.run_id}@{event.event_type}@{event.event_time}"


def _version_of(dataset: Dataset) -> int | None:
    """The Lance version a dataset facet names, or ``None`` when it names none or one that is no version."""
    facet = dataset.facet("version")
    raw = facet.get("datasetVersion") if facet else None
    try:
        version = int(str(raw)) if raw is not None else None
    except ValueError:
        return None
    return version if version is not None and version >= 0 else None


def _branch_of(dataset: Dataset) -> str | None:
    """The ref an input was read on, from the catalog's ``lance`` dataset facet; ``None`` is main."""
    facet = dataset.facet("lance")
    ref = facet.get("ref") if facet else None
    return ref if isinstance(ref, str) and ref else None


def _edge_key(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


def edges_of(event: RunEvent | DatasetEvent, *, received_at: datetime) -> list[dict[str, Any]]:
    """The table-level edges a successful run asserts: every input to every output it wrote, at their versions.

    A failed run derived nothing and a static change reads nothing, so neither asserts an edge. An in-place
    refinement (a run that reads and writes one table) bumps that table's version and is no edge. Every value is
    read off the dataset that carries it, never looked up by its bare name, so an external input sharing a
    governed table's name keeps its own identity and version ([[LIN-3]]).
    """
    if not isinstance(event, RunEvent) or not event.is_success:
        return []
    lance_facet = event.run.facets.get("lance")
    writer_run = lance_facet.get("runId") if isinstance(lance_facet, dict) else None
    rows: list[dict[str, Any]] = []
    for output in event.outputs:
        out_table, out_version = output.lineage_name, _version_of(output)
        for source in event.inputs:
            in_table = source.lineage_name
            if in_table == out_table:
                continue
            in_version, in_branch = _version_of(source), _branch_of(source)
            rows.append(
                {
                    "edge_key": _edge_key(event.run.run_id, in_table, in_version, in_branch, out_table, out_version, event.write_ref),
                    "run_id": event.run.run_id,
                    "writer_run_id": writer_run if isinstance(writer_run, str) and writer_run else None,
                    "author": event.author,
                    "in_table": in_table,
                    "in_version": in_version,
                    "in_branch": in_branch,
                    "out_table": out_table,
                    "out_version": out_version,
                    "out_branch": event.write_ref,
                    "event_time": event.event_time,
                    "received_at": received_at,
                }
            )
    return rows


def feed_row(event: RunEvent | DatasetEvent, *, received_at: datetime) -> dict[str, Any]:
    """The feed row one admitted event becomes. The payload is the event as the parsed model dumps it, the JSON its bound weighs."""
    if isinstance(event, DatasetEvent):
        return {
            "event_key": feed_key(event),
            "run_id": event.feed_id,
            "event_type": event.feed_event_type,
            "event_time": event.event_time,
            "job": None,
            "author": event.author,
            "inputs": [],
            "outputs": [event.dataset.name],
            "event": event.model_dump_json(by_alias=True),
            "received_at": received_at,
        }
    return {
        "event_key": feed_key(event),
        "run_id": event.run.run_id,
        "event_type": event.event_type,
        "event_time": event.event_time,
        "job": f"{event.job.namespace}/{event.job.name}",
        "author": event.author,
        "inputs": [d.name for d in event.inputs],
        "outputs": [d.name for d in event.outputs],
        "event": event.model_dump_json(by_alias=True),
        "received_at": received_at,
    }


def sql_literal(value: str) -> str:
    """``value`` as a SQL string literal for a Lance filter: quoted, with every quote doubled."""
    return "'" + value.replace("'", "''") + "'"


class ProvenanceStore:
    """The feed, the edge list and the outbox refusals, read and written through the catalog's doors.

    Every method is async and runs its catalog calls in a worker thread, because the spec client is blocking.
    ``token`` answers this pod's bearer for the catalog, read on every call: the kubelet rotates a projected
    token at about 515 s of its 600 s life. ``None`` sends none, which only a catalog with authentication off
    accepts.
    """

    def __init__(self, *, catalog_url: str, namespace: Sequence[str], token: Callable[[], str | None], timeout_seconds: float = 30.0) -> None:
        if not catalog_url:
            raise ValueError("the provenance store needs the catalog's URL (LINEAGE_CATALOG_URL)")
        if not namespace:
            raise ValueError("the provenance store needs a namespace to keep its tables in")
        self._url = catalog_url.rstrip("/")
        self._namespace = list(namespace)
        self._token = token
        self._timeout = timeout_seconds

    @classmethod
    def from_settings(cls, settings: LineageSettings) -> ProvenanceStore:
        """The store a deployment configures: the catalog at ``LINEAGE_CATALOG_URL``, the namespace it keeps for provenance."""
        token_file = settings.catalog_identity_token_file

        def token() -> str | None:
            return read_identity_token(token_file) if token_file else None

        return cls(
            catalog_url=settings.catalog_url,
            namespace=settings.provenance_namespace.split(CATALOG_DELIMITER),
            token=token,
            timeout_seconds=settings.catalog_timeout_seconds,
        )

    def table_id(self, table: str) -> list[str]:
        return [*self._namespace, table]

    async def _run(self, call: Callable[[], _T]) -> _T:
        return await asyncio.to_thread(call)

    def _reader(self, table: str) -> CatalogTableReader:
        return CatalogTableReader(RestCatalogTransport(self._url, token=self._token(), timeout=self._timeout), self.table_id(table))

    def _writer(self, table: str) -> RestCatalogWriteTransport:
        return RestCatalogWriteTransport(self._url, self.table_id(table), token=self._token(), timeout=self._timeout)

    # ---- tables -------------------------------------------------------------------------------------------

    async def ensure(self) -> None:
        """Create each table this store keeps, keeping one that already exists (``exist_ok``)."""
        await self._run(self._ensure)

    def _ensure(self) -> None:
        from lance_namespace_urllib3_client.api.data_api import DataApi

        api = DataApi(catalog_api_client(self._url))
        for table, schema in _TABLES:
            with translate_catalog_errors():
                api.create_table(
                    CATALOG_DELIMITER.join(self.table_id(table)),
                    encode_arrow_stream(schema.empty_table()),
                    delimiter=CATALOG_DELIMITER,
                    mode="exist_ok",
                    _request_timeout=self._timeout,
                    _headers=request_headers(self._token()),
                )

    async def ready(self) -> None:
        """Raise unless the feed table answers: the one thing every door of this service needs."""
        await self._run(lambda: self._reader(FEED).table_version())

    # ---- writes -------------------------------------------------------------------------------------------

    async def record(self, event: RunEvent | DatasetEvent) -> None:
        """Record one admitted event: its run's author, its edges, then its feed row. Idempotent: a redelivery lands nothing new.

        A run is bound to its author first, by its first event, so the binding exists before any row of the run is
        visible; a later event of the run reads the binding and writes none. The edges go before the feed row so a
        follower that sees the row can read every edge it asserts. Each is its own commit on its own table, so a failure
        between them leaves the earlier ones; the caller retries the delivery, and the retry lands what is missing and
        nothing twice.
        """
        received = datetime.now(UTC)
        edges = edges_of(event, received_at=received)
        row = feed_row(event, received_at=received)
        await self._run(lambda: self._record(event, received, edges, row))

    def _record(self, event: RunEvent | DatasetEvent, received: datetime, edges: list[dict[str, Any]], row: dict[str, Any]) -> None:
        if isinstance(event, RunEvent) and event.run.run_id and not self._bound(event.run.run_id):
            binding = {"run_id": event.run.run_id, "author": author_sub_from_payload(json.loads(row["event"])), "received_at": received}
            self._writer(RUNS).merge_insert_only(pa.Table.from_pylist([binding], schema=RUN_SCHEMA), on="run_id")
        if edges:
            self._writer(EDGES).merge_insert_only(pa.Table.from_pylist(edges, schema=EDGE_SCHEMA), on="edge_key")
        self._writer(FEED).merge_insert_only(pa.Table.from_pylist([row], schema=FEED_SCHEMA), on="event_key")

    def _bound(self, run_id: str) -> bool:
        return self._reader(RUNS).count_rows(f"run_id = {sql_literal(run_id)}") > 0

    async def record_refusal(self, *, outbox_key: str, run_id: str | None, author: str | None, reason: str, event_json: str) -> None:
        """Keep a settled governance refusal of a staged event, so the staged object can be retired ([[LH-182]]).

        An upsert on the outbox key: the drain re-reads the outbox every tick, and a crash between this write and
        the object's delete leaves the object to be refused again into the same row.
        """
        row = {"outbox_key": outbox_key, "run_id": run_id, "author": author, "reason": reason, "event": event_json, "refused_at": datetime.now(UTC)}
        await self._run(lambda: self._writer(REFUSALS).merge_upsert(pa.Table.from_pylist([row], schema=REFUSAL_SCHEMA), on="outbox_key"))

    async def prune_events(self, retention_days: int) -> int:
        """Delete the feed rows this service received more than ``retention_days`` ago; return how many it found to delete."""
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        predicate = f"received_at < arrow_cast({sql_literal(cutoff.isoformat(timespec='microseconds'))}, 'Timestamp(Microsecond, Some(\"UTC\"))')"
        return await self._run(lambda: self._prune(predicate))

    def _prune(self, predicate: str) -> int:
        doomed = self._reader(FEED).count_rows(predicate)
        if doomed:
            self._writer(FEED).delete(predicate)
        return doomed

    # ---- reads --------------------------------------------------------------------------------------------

    async def list_events(self, limit: int = 500, *, after: int | None = None, since: int | None = None, summary: bool = False) -> list[EventRecord]:
        """Feed rows: newest first, older than ``after``, or oldest first after ``since`` (the follower's read).

        ``summary`` leaves the payload out of the read itself. Passing both cursors is a caller error.
        """
        if after is not None and since is not None:
            raise ValueError("list_events takes `after` or `since`, not both")
        return await self._run(lambda: self._list_events(limit, after=after, since=since, summary=summary))

    def _list_events(self, limit: int, *, after: int | None, since: int | None, summary: bool) -> list[EventRecord]:
        reader = self._reader(FEED)
        version = reader.table_version()
        if since is not None:
            selector: str | None = f"_rowid >= {since}"
        elif after is not None:
            selector = f"_rowid < {after - 1}" if after > 1 else None
            if selector is None:
                return []
        else:
            selector = None
        ids = _row_ids(reader.to_table(columns=["event_key"], filter=selector, with_row_id=True, version=version))
        forward = since is not None
        chosen = sorted(ids, reverse=not forward)[:limit]
        if not chosen:
            return []
        rows = reader.to_table(
            columns=_SUMMARY_COLUMNS if summary else _FEED_COLUMNS,
            filter=f"_rowid >= {min(chosen)} AND _rowid <= {max(chosen)}",
            with_row_id=True,
            version=version,
        )
        records = [_record_of(row, summary=summary) for row in rows.to_pylist()]
        return sorted(records, key=lambda record: record.seq, reverse=not forward)

    async def feed_bounds(self) -> tuple[int, int] | None:
        """The oldest and the newest seq the feed holds, or ``None`` when it holds nothing.

        ``None`` rather than a floor of ``0`` on an empty feed: zero would read as "everything since the
        beginning is still here", the opposite of what an empty feed means after retention.
        """

        def bounds() -> tuple[int, int] | None:
            ids = _row_ids(self._reader(FEED).to_table(columns=["event_key"], with_row_id=True))
            return (min(ids) + 1, max(ids) + 1) if ids else None

        return await self._run(bounds)

    async def recorded_event(self, feed_id: str, event_type: str | None) -> dict[str, Any] | None:
        """The payload the feed holds for ``(feed_id, event_type)``, the first such row; ``None`` when it holds none."""
        if event_type is None:
            return None
        rows = await self._run(
            lambda: self._reader(FEED).to_table(
                columns=["event"], filter=f"run_id = {sql_literal(feed_id)} AND event_type = {sql_literal(event_type)}", with_row_id=True
            )
        )
        first = min(rows.to_pylist(), key=lambda row: row["_rowid"], default=None)
        stored = json.loads(first["event"]) if first and first["event"] else None
        return stored if isinstance(stored, dict) else None

    async def derived(self, table: str, *, max_depth: int = MAX_DERIVED_DEPTH) -> tuple[list[DerivedTable], bool]:
        """Every table derived from ``table``, nearest first, and whether the walk reached the end of it.

        The tables written by the runs that read ``table``, then by the runs that read those, to ``max_depth``
        hops, read off the edge list at one version. An edge is kept whatever version of its input the run
        named, so a table derived from any version of ``table`` is in the answer: the direction an erasure
        needs, since it must reach every table that may hold what it erases. The walk is ``False``-complete
        when it stopped at ``max_depth`` with tables left to follow.
        """
        return await self._run(lambda: self._derived(table, max_depth))

    def _derived(self, table: str, max_depth: int) -> tuple[list[DerivedTable], bool]:
        reader = self._reader(EDGES)
        version = reader.table_version()
        reached: dict[str, int] = {}
        frontier = {table}
        for depth in range(1, max_depth + 1):
            frontier = _outputs_of(reader, frontier, version) - reached.keys() - {table}
            if not frontier:
                break
            reached.update(dict.fromkeys(frontier, depth))
        else:
            frontier = _outputs_of(reader, frontier, version) - reached.keys() - {table}
        found = [DerivedTable(table=name, depth=depth) for name, depth in sorted(reached.items(), key=lambda item: (item[1], item[0]))]
        return found, not frontier

    async def cursor(self, follower: str) -> int:
        """The seq of the last feed row ``follower`` handled; 0 before its first."""
        rows = await self._run(lambda: self._reader(CURSORS).to_table(columns=["seq"], filter=f"follower = {sql_literal(follower)}"))
        return max(rows.column("seq").to_pylist(), default=0)

    async def advance_cursor(self, follower: str, seq: int) -> None:
        """Move ``follower``'s cursor to ``seq``, never backward: a replica's slower tick cannot undo a faster one's progress."""
        await self._run(lambda: self._writer(CURSORS).merge_upsert(_cursor_row(follower, seq), on="follower", matched_when="source.seq > target.seq"))

    async def reset_cursor(self, follower: str, seq: int) -> None:
        """Set ``follower``'s cursor to ``seq`` whichever way it moves: for a cursor the feed never issued (a rebuilt feed)."""
        await self._run(lambda: self._writer(CURSORS).merge_upsert(_cursor_row(follower, seq), on="follower"))

    async def run_recorded(self, run_id: str) -> bool:
        """Whether the feed holds any row of this run."""
        return await self._run(lambda: self._reader(FEED).count_rows(f"run_id = {sql_literal(run_id)}")) > 0

    async def run_author(self, run_id: str) -> str | None:
        """The verified author sub the run is bound to, or ``None`` when no event of it was recorded or its first named none."""
        rows = await self._run(lambda: self._reader(RUNS).to_table(columns=["author"], filter=f"run_id = {sql_literal(run_id)}"))
        return next((author for author in rows.column("author").to_pylist() if author), None)


def _cursor_row(follower: str, seq: int) -> pa.Table:
    return pa.Table.from_pylist([{"follower": follower, "seq": seq, "updated_at": datetime.now(UTC)}], schema=CURSOR_SCHEMA)


def _outputs_of(reader: CatalogTableReader, inputs: set[str], version: int) -> set[str]:
    """The tables the edge list records a run writing from any of ``inputs``, at ``version`` of it."""
    names = sorted(inputs)
    found: set[str] = set()
    for start in range(0, len(names), _IN_LIST):
        listed = ", ".join(sql_literal(name) for name in names[start : start + _IN_LIST])
        found.update(reader.to_table(columns=["out_table"], filter=f"in_table IN ({listed})", version=version).column("out_table").to_pylist())
    return found


def _row_ids(table: pa.Table) -> list[int]:
    return [int(value) for value in table.column("_rowid").to_pylist()] if "_rowid" in table.column_names else []


def _record_of(row: dict[str, Any], *, summary: bool) -> EventRecord:
    payload = json.loads(row["event"]) if not summary and row.get("event") else {}
    return EventRecord(
        seq=int(row["_rowid"]) + 1,
        event_type=row["event_type"],
        event_time=row["event_time"],
        job=row["job"],
        author=row["author"],
        inputs=list(row["inputs"] or []),
        outputs=list(row["outputs"] or []),
        event=payload if isinstance(payload, dict) else {},
    )
