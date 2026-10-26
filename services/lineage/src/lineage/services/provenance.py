"""The commit feed and the table-level edge list, kept as Lance tables the catalog governs (ADR 0158, LH-468).

Lineage keeps no database and holds no storage credential for these tables. They live in the catalog's
provenance namespace, which the catalog keeps for them and never announces a write to (an announcement of a
feed write would be ingested as a feed write, without end). Every row lands through the catalog's own
``merge_insert`` door and is read back through its ``query`` door, as this service's own principal, so a
write is authorized, judged and audited like any other writer's.

THE FEED'S SEQUENCE IS LANCE'S OWN. Its tables are created with stable row ids, and Lance assigns a row's id
from the manifest's ``next_row_id`` when its commit lands, re-assigning it when a racing commit wins first
(`lance_docs/lance/format/table/row_id_lineage.md`, "Row ID Assignment"). So ``_rowid`` order is commit order: measured on pylance
13.0.0, four writers racing 40 keyed appends left the row-id order equal to the created-at-version order.
A follower reading ``seq > since`` therefore never misses a row that commits after it read. ``seq`` is
``_rowid + 1``, so the first row is 1 and ``since=0`` reads from the start.

A ROW IS WRITTEN ONCE. Each table declares an unenforced primary key and is written with an insert-only
merge on it, which the catalog runs on Lance's own enforcement: a redelivered event lands no second row, and
two racing deliveries of one event land it once (``catalog.services.primary_keys``). The feed keeps one row
per terminal state of a run, one per arrival of a non-terminal state, and one per static change.

A BURST IS ONE COMMIT PER TABLE ([[LH-533]]). Every commit is a Lance version and a fragment that each later open and
scan of the table pays for (ADR 0174), and the sidecar delivers lineage's subscription in parallel (`concurrencyMode:
parallel`, chart `services.lineage.deliveriesInFlight`), so the events recorded while a commit is in flight wait and land
together in the next one: one writer per process, started by the first event of a burst and gone with its last, with no
timer, so an idle estate waits on nothing. Measured on pylance 13.0.0 against the catalog app, 29
concurrent deliveries recorded one commit each took 16, 16 and 13 commits on `runs`, `edges` and `events`, and the
catalog refused some of them 429 at its concurrent-write cap; 200 recorded by group commit took 2 on each, and none was
refused.

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
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
from typing import Any, Final, TypeVar

import pyarrow as pa
from opentelemetry import trace
from opentelemetry.trace import SpanContext
from pydantic import BaseModel, ConfigDict

from lineage.core.config import LineageSettings
from lineage.models import Dataset, DatasetEvent, ForeignRunError, RunEvent, author_sub_from_payload
from lineage.schemas import DerivedTable, EventRecord
from service_kit.governed.machine_identity import read_identity_token
from service_kit.lakehouse import derived_tables
from service_kit.lakehouse.derived_tables import EDGES
from service_kit.lakehouse.naming import CATALOG_DELIMITER, is_external_source_namespace
from service_kit.lancekit.arrow_ipc import encode_arrow_stream
from service_kit.lancekit.catalog_client import catalog_api_client, request_headers
from service_kit.lancekit.filters import in_lists, sql_literal
from service_kit.lancekit.reader import CatalogTableReader, RestCatalogTransport, translate_catalog_errors
from service_kit.lancekit.writer import RestCatalogWriteTransport
from service_kit.openlineage import BRANCH_IDENTIFIER, dataset_uuids


log = logging.getLogger(__name__)

tracer = trace.get_tracer(__name__)

_T = TypeVar("_T")

#: The field metadata Lance reads a table's unenforced primary key from (`lance_docs/lance/format/table/index.md`, "Unenforced Primary Key").
_PRIMARY_KEY: Final = {"lance-schema:unenforced-primary-key": "true"}

#: The states a run ends in. The feed keeps the FIRST row of each, whatever its event time, so a terminal event a
#: producer re-sends with a fresh time is one row; a non-terminal state keeps every arrival, its progress trail.
TERMINAL_STATES: Final = frozenset({"COMPLETE", "FAIL", "ABORT"})

FEED: Final = "events"
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
        # The uuids of every governed table the event names, and of their namespaces (ADR 0173 §10): what a scoped
        # read keys on, so the history of a table a rename gave a new name is one history.
        pa.field("table_uuids", pa.list_(pa.string())),
        pa.field("namespace_uuids", pa.list_(pa.string())),
        # The governed tables the event named that resolved to no uuid (an outside producer's names its credential
        # could not resolve): a scoped read keys these by name.
        pa.field("unresolved_tables", pa.list_(pa.string())),
        pa.field("event", pa.large_string()),
        # The estate's clock, never the producer's `eventTime`: retention reads it, and a skewed producer stamp
        # would strand a row for ever or delete a fresh one.
        pa.field("received_at", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)

#: One row per (run, input, output) a successful run asserts. `in_table` and `out_table` are the dataset's key under
#: :func:`edge_identity`: a governed table's uuid, so a rename keeps its edges ([[LH-522]]); an external source's
#: namespace-qualified name, so an external input can never name a governed table's edges ([[LIN-3]]). `in_name` and
#: `out_name` are the names the event used. A branch is named by its ref and by the incarnation of that ref
#: (`in_branch_identifier`, `out_branch_identifier`, the last uuid of pylance's `branch_identifier`), since a branch
#: deleted and created again under one name restarts its numbering ([[LH-413]]); both are null on main.
EDGE_SCHEMA: Final = pa.schema(
    [
        pa.field("edge_key", pa.string(), nullable=False, metadata=_PRIMARY_KEY),
        pa.field("run_id", pa.string()),
        pa.field("writer_run_id", pa.string()),
        pa.field("author", pa.string()),
        pa.field("in_table", pa.string(), nullable=False),
        pa.field("in_name", pa.string()),
        pa.field("in_version", pa.int64()),
        pa.field("in_branch", pa.string()),
        pa.field("in_branch_identifier", pa.string()),
        pa.field("out_table", pa.string(), nullable=False),
        pa.field("out_name", pa.string()),
        pa.field("out_version", pa.int64()),
        pa.field("out_branch", pa.string()),
        pa.field("out_branch_identifier", pa.string()),
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
#: The most bytes of rows one group commit takes, a sixteenth of the catalog's default body cap (`LANCE_MAX_BODY_BYTES`,
#: 256 MiB), and always at least one event. A catalog configured below it refuses a larger write, which then lands in
#: parts (`ProvenanceStore._land`).
_BATCH_BYTES: Final = 16 * 1024 * 1024
#: The statuses the catalog answers a write whose rows it refuses: InvalidInput, a body over its cap, a body it cannot
#: process. Every other failure is about the catalog or the caller, not the rows: one it cannot take now (429, a 5xx, no
#: answer) or will not take from this caller (401, 403, 404).
_REFUSED_ROWS: Final = frozenset({400, 413, 422})
_SUMMARY_COLUMNS: Final = ["event_type", "event_time", "job", "author", "inputs", "outputs", "table_uuids", "namespace_uuids", "unresolved_tables"]
_FEED_COLUMNS: Final = [*_SUMMARY_COLUMNS, "event"]


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


def _branch_identifier_of(dataset: Dataset) -> str | None:
    """The incarnation of the ref an input was read on, from the catalog's ``lance`` dataset facet; ``None`` on main."""
    facet = dataset.facet("lance")
    identifier = facet.get(BRANCH_IDENTIFIER) if facet else None
    return identifier if isinstance(identifier, str) and identifier else None


def _edge_key(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()


def edge_identity(dataset: Dataset) -> str:
    """The key a dataset's edges are recorded under: a governed table's uuid, else its :attr:`Dataset.lineage_name`.

    A governed table is keyed by the uuid the catalog minted for it, which a rename keeps, so its edges before and after
    one are one history ([[LH-522]], ADR 0173 §10). An external source keeps its namespace-qualified name ([[LIN-3]]),
    and a governed table whose event named no uuid (an outside producer's name the caller's credential could not
    resolve) keeps its bare name. The three cannot collide: a uuid holds no delimiter and no ``/``, a governed name
    always holds the delimiter, and an external one always holds a ``/``.
    """
    if is_external_source_namespace(dataset.namespace, dataset.name):
        return dataset.lineage_name
    return dataset_uuids({"facets": dataset.facets}).table_uuid or dataset.lineage_name


class Scope(BaseModel):
    """What a feed row is about: the uuids of the governed tables it names and of their namespaces, and the governed
    tables it named that carry no uuid, each once, in the order named."""

    table_uuids: list[str]
    namespace_uuids: list[str]
    unresolved_tables: list[str]


def scope_of(datasets: list[Dataset]) -> Scope:
    """The :class:`Scope` of an event naming ``datasets``."""
    governed = [dataset for dataset in datasets if not is_external_source_namespace(dataset.namespace, dataset.name)]
    named = [(dataset.name, dataset_uuids({"facets": dataset.facets})) for dataset in governed]
    return Scope(
        table_uuids=list(dict.fromkeys(uuids.table_uuid for _, uuids in named if uuids.table_uuid)),
        namespace_uuids=list(dict.fromkeys(uuids.namespace_uuid for _, uuids in named if uuids.namespace_uuid)),
        unresolved_tables=list(dict.fromkeys(name for name, uuids in named if not uuids.table_uuid)),
    )


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
        out_table, out_version = edge_identity(output), _version_of(output)
        for source in event.inputs:
            in_table = edge_identity(source)
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
                    "in_name": source.lineage_name,
                    "in_version": in_version,
                    "in_branch": in_branch,
                    "in_branch_identifier": _branch_identifier_of(source) if in_branch is not None else None,
                    "out_table": out_table,
                    "out_name": output.lineage_name,
                    "out_version": out_version,
                    "out_branch": event.write_ref,
                    "out_branch_identifier": event.write_ref_identifier,
                    "event_time": event.event_time,
                    "received_at": received_at,
                }
            )
    return rows


def feed_row(event: RunEvent | DatasetEvent, *, received_at: datetime) -> dict[str, Any]:
    """The feed row one admitted event becomes. The payload is the event as the parsed model dumps it, the JSON its bound weighs."""
    scope = scope_of([*event.inputs, *event.outputs]).model_dump()
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
            **scope,
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
        **scope,
        "event": event.model_dump_json(by_alias=True),
        "received_at": received_at,
    }


class _Recording(BaseModel):
    """One admitted event's rows, waiting for the group commit that lands them, and the delivery waiting on it."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    #: The run a run event names, and the verified author it is recorded for: what a run's first event binds it to.
    run_id: str | None
    author: str | None
    received_at: datetime
    edges: pa.Table
    row: pa.Table
    #: The bytes of its rows, what a batch is bounded by.
    weight: int
    #: The span of the delivery, which the commit landing these rows links to.
    origin: SpanContext
    landed: Future[None]


def _recording_of(event: RunEvent | DatasetEvent, *, received_at: datetime) -> _Recording:
    row = feed_row(event, received_at=received_at)
    run_id = event.run.run_id if isinstance(event, RunEvent) and event.run.run_id else None
    edges = pa.Table.from_pylist(edges_of(event, received_at=received_at), schema=EDGE_SCHEMA)
    rows = pa.Table.from_pylist([row], schema=FEED_SCHEMA)
    return _Recording(
        run_id=run_id,
        author=author_sub_from_payload(json.loads(row["event"])) if run_id else None,
        received_at=received_at,
        edges=edges,
        row=rows,
        weight=rows.nbytes + edges.nbytes,
        origin=trace.get_current_span().get_span_context(),
        landed=Future(),
    )


def _first_of_each(rows: pa.Table, key: str) -> pa.Table:
    """``rows`` keeping only the first row of each value of ``key``, in their order: the catalog refuses a write that repeats a key."""
    order = rows.select([key]).append_column("order", pa.array(range(rows.num_rows), pa.int64()))
    firsts = order.group_by(key).aggregate([("order", "min")]).column("order_min")
    return rows.take(pa.array(sorted(firsts.to_pylist()), pa.int64()))


def _refused_rows(error: Exception) -> bool:
    """Whether the catalog answered ``error`` refusing the rows a write carried (:data:`_REFUSED_ROWS`).

    The spec client's answer is the cause of every error `translate_catalog_errors` raises, carrying the HTTP status.
    """
    from lance_namespace_urllib3_client.exceptions import ApiException

    return isinstance(error.__cause__, ApiException) and error.__cause__.status in _REFUSED_ROWS


def _extends_another_authors(recording: _Recording, authors: Mapping[str, str | None]) -> bool:
    """Whether ``recording`` names an author other than the one its run is bound to; an unauthored side compares to nothing, as at the doors."""
    if recording.run_id is None or recording.author is None:
        return False
    owner = authors[recording.run_id]
    return owner is not None and owner != recording.author


def _settle(recording: _Recording, error: BaseException | None) -> None:
    """Answer the delivery waiting on ``recording`` with its outcome, unless it stopped waiting."""
    if not recording.landed.running():
        return
    if error is None:
        recording.landed.set_result(None)
    else:
        recording.landed.set_exception(error)


class ProvenanceStore:
    """The feed, the edge list and the outbox refusals, read and written through the catalog's doors.

    Every method is async and runs its catalog calls off the event loop, because the spec client is blocking: a record on
    the writer thread of its burst, every other call in a worker thread. ``token`` answers this pod's bearer for the
    catalog, read on every call: the kubelet rotates a projected token at about 515 s of its 600 s life. ``None`` sends
    none, which only a catalog with authentication off accepts. ``refuse_foreign_runs`` is whether a commit refuses an event
    of a run another author bound (:meth:`_commit`): the doors' rule, on when they authorize.
    """

    def __init__(
        self,
        *,
        catalog_url: str,
        namespace: Sequence[str],
        token: Callable[[], str | None],
        refuse_foreign_runs: bool,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not catalog_url:
            raise ValueError("the provenance store needs the catalog's URL (LINEAGE_CATALOG_URL)")
        if not namespace:
            raise ValueError("the provenance store needs a namespace to keep its tables in")
        self._url = catalog_url.rstrip("/")
        self._namespace = list(namespace)
        self._token = token
        self._refuse_foreign_runs = refuse_foreign_runs
        self._timeout = timeout_seconds
        self._pending: deque[_Recording] = deque()
        self._pending_lock = threading.Lock()
        #: Whether a writer thread is landing what is pending; set and cleared under the lock, so a burst has one.
        self._landing = False

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
            refuse_foreign_runs=settings.fga_enabled,
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

        The commits are shared: the event joins the group commit the writer thread makes next, with every event
        recorded while the one before it is in flight (:meth:`_land`), and this returns once that commit landed it.

        Raises:
            Exception: whatever kept the event's rows from landing; nothing it carries is recorded past the table that refused it.
        """
        recording = _recording_of(event, received_at=datetime.now(UTC))
        with self._pending_lock:
            self._pending.append(recording)
            start = not self._landing
            self._landing = True
        if start:
            try:
                threading.Thread(target=self._land_pending, name="provenance-record").start()
            except BaseException:
                # No writer took the burst, so the next record starts one, which lands this event too.
                with self._pending_lock:
                    self._landing = False
                raise
        await asyncio.wrap_future(recording.landed)

    def _land_pending(self) -> None:
        """The writer thread of one burst: land what is pending, a batch per commit, until nothing is."""
        batch: list[_Recording] = []
        try:
            while batch := self._next_batch():
                self._land(batch)
        except BaseException as exc:
            # A panic in a native library is a BaseException, which `_land` does not take: the batch's deliveries fail
            # with it, and the next record starts a writer for what is still pending.
            for recording in batch:
                _settle(recording, exc)
            with self._pending_lock:
                self._landing = False
            raise

    def _next_batch(self) -> list[_Recording]:
        """The recordings the next commit takes, oldest first, within :data:`_BATCH_BYTES` and at least one; none ends the writer."""
        batch: list[_Recording] = []
        weight = 0
        with self._pending_lock:
            while self._pending and (not batch or weight + self._pending[0].weight <= _BATCH_BYTES):
                recording = self._pending.popleft()
                # A delivery that stopped waiting is landed all the same and answered by nothing: its redelivery finds it recorded.
                recording.landed.set_running_or_notify_cancel()
                weight += recording.weight
                batch.append(recording)
            if not batch:
                self._landing = False
        return batch

    def _land(self, batch: list[_Recording]) -> None:
        """Commit ``batch`` and answer each delivery in it with the outcome.

        A FAILURE ONE EVENT CAUSES STAYS ITS OWN. A batch whose rows the catalog refuses (:data:`_REFUSED_ROWS`) lands in
        halves, each again in halves while it is refused: the deliveries in the halves that land succeed, and only the
        event the catalog refuses fails its delivery. Any other failure fails the batch whole, and each of its deliveries
        retries: a split would send a catalog that cannot take a write now, its write cap's 429 among them, up to twice as
        many.
        """
        try:
            foreign = {id(recording) for recording in self._commit(batch)}
        except Exception as exc:
            if len(batch) > 1 and _refused_rows(exc):
                log.warning("provenance_batch_refused", extra={"events": len(batch), "error": str(exc)})
                half = len(batch) // 2
                self._land(batch[:half])
                self._land(batch[half:])
                return
            log.warning("provenance_record_failed", extra={"events": len(batch), "error": str(exc)})
            for recording in batch:
                _settle(recording, exc)
            return
        for recording in batch:
            refused = id(recording) in foreign
            _settle(recording, ForeignRunError(f"run {recording.run_id} was founded by another subject; only its author may extend it") if refused else None)

    def _commit(self, batch: Sequence[_Recording]) -> list[_Recording]:
        """Land ``batch``'s rows in one commit per table, in :meth:`record`'s order: the run bindings, the edges, the feed rows.

        Answers the recordings refused because their run is another author's, whose rows it does not write.

        A RUN IS EXTENDED ONLY BY ITS AUTHOR ([[LIN-5]]). The doors refuse an event of a run the table binds to another
        subject, but events delivered together were each authorized before any of them bound its run, so the commit asks
        again where the doors ask at all: a run is bound to the author of its first event, the table's binding or else the
        batch's first, and an event of it naming another author is refused. With authorization off the doors take an
        author as claimed, and so does the commit.

        A key the batch repeats is sent once, its first delivery's row, since the catalog refuses a write that repeats one.
        """
        links = [trace.Link(recording.origin) for recording in batch if recording.origin.is_valid]
        with tracer.start_as_current_span("lineage.record", links=links) as span:
            span.set_attribute("lance.lineage.events", len(batch))
            bound = self._authors_of({recording.run_id for recording in batch if recording.run_id})
            founders: dict[str, _Recording] = {}
            for recording in batch:
                if recording.run_id and recording.run_id not in bound:
                    founders.setdefault(recording.run_id, recording)
            authors = {**{run: founder.author for run, founder in founders.items()}, **bound}
            foreign = [recording for recording in batch if self._refuse_foreign_runs and _extends_another_authors(recording, authors)]
            refused = {id(recording) for recording in foreign}
            landing = [recording for recording in batch if id(recording) not in refused]
            if founders:
                bindings = [{"run_id": run, "author": founder.author, "received_at": founder.received_at} for run, founder in founders.items()]
                self._writer(RUNS).merge_insert_only(pa.Table.from_pylist(bindings, schema=RUN_SCHEMA), on="run_id")
            if landing:
                edges = _first_of_each(pa.concat_tables([recording.edges for recording in landing]), "edge_key")
                if edges.num_rows:
                    self._writer(EDGES).merge_insert_only(edges, on="edge_key")
                rows = _first_of_each(pa.concat_tables([recording.row for recording in landing]), "event_key")
                self._writer(FEED).merge_insert_only(rows, on="event_key")
        return foreign

    def _authors_of(self, runs: set[str]) -> dict[str, str | None]:
        """The author the table binds each of ``runs`` to, for those of them it binds."""
        reader = self._reader(RUNS)
        authors: dict[str, str | None] = {}
        for where in in_lists("run_id", runs):
            bound = reader.to_table(columns=["run_id", "author"], filter=where)
            authors.update(zip(bound.column("run_id").to_pylist(), bound.column("author").to_pylist(), strict=True))
        return authors

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

    async def derived(self, sources: set[str], *, max_depth: int = MAX_DERIVED_DEPTH) -> tuple[list[DerivedTable], bool]:
        """Every table derived from the table keyed by ``sources``, nearest first, and whether the walk reached the end of it.

        ``sources`` are edge keys (:func:`edge_identity`): a governed table's uuid, so the walk follows the edges the table
        left under every name it has had, and the name an outside producer's unresolved event keyed it by. The tables written by the runs that read it, then by the runs that read those,
        to ``max_depth`` hops, read off the edge list at one version. An edge is kept whatever version of its input the
        run named, so a table derived from any version of ``table`` is in the answer: the direction an erasure needs,
        since it must reach every table that may hold what it erases. The walk is ``False``-complete when it stopped at
        ``max_depth`` with tables left to follow. Each derived table is answered by the name its newest edge used.
        """
        return await self._run(lambda: self._derived(sources, max_depth))

    def _derived(self, sources: set[str], max_depth: int) -> tuple[list[DerivedTable], bool]:
        reader = self._reader(EDGES)
        version = reader.table_version()
        walked = derived_tables.walk(
            lambda where: reader.to_table(columns=list(derived_tables.COLUMNS), filter=where, version=version).to_pylist(), sources, max_depth=max_depth
        )
        found = [
            DerivedTable(table=walked.names.get(key, key), uuid=key if derived_tables.is_table_uuid(key) else None, depth=depth)
            for key, depth in sorted(walked.depths.items(), key=lambda item: (item[1], walked.names.get(item[0], item[0])))
        ]
        return found, walked.complete

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
        table_uuids=list(row["table_uuids"] or []),
        namespace_uuids=list(row["namespace_uuids"] or []),
        unresolved_tables=list(row["unresolved_tables"] or []),
        event=payload if isinstance(payload, dict) else {},
    )
