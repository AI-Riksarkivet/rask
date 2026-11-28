"""The read doors served in-process: a row count on the ref the request names and the change feed over a version window.

Scope: pylance dataset semantics only. HTTP read semantics over a blob payload — byte ranges, ``If-Range``, ETags,
satisfiability, the bounded streaming window — belong to ``blob_serving.py``, which changes for RFC 9110 reasons
rather than pylance ones. Nothing here shapes a response."""

import io
import itertools
from collections.abc import Iterator
from typing import Any, Final

import lance
import pyarrow as pa
from lance_namespace import CountTableRowsRequest, CountTableRowsResponse, InvalidInputError, InvalidTableStateError, LanceNamespace, UnsupportedOperationError
from pydantic import BaseModel

from catalog.core.lance_refusals import caller_sql, raise_if_store_outage
from catalog.core.query_bounds import refuse_an_unbounded_boolean_chain
from catalog.core.version_retention import recorded_branch
from catalog.schemas import ChangeKind
from catalog.services import changes, table_claims
from lakehouse.lancekit.transactions import read_transaction
from lakehouse.storage.options import StorageOptions


__all__ = [
    "MOST_VERSIONS_IN_A_CHANGE_WINDOW",
    "RowCount",
    "count_rows",
    "read_changes",
    "refuse_a_branch_this_door_cannot_honour",
]


def refuse_a_branch_this_door_cannot_honour(branch: str | None, *, door: str, remedy: str | None = None) -> None:
    """Refuse a branch-scoped read the upstream implementation answers from MAIN.

    `query_table`, `explain_table_query_plan` and `analyze_table_query_plan` all declare `branch` and
    all delegate whole-request to the upstream implementation, which ignores it. Verified live
    2026-08-31: after deleting two of three rows on `work`, `/query` returned 3 rows for `branch=work`
    AND 3 for a branch that had never been created — main's answer, twice, with a 200.

    An Unsupported answer (406) rather than a wrong 200, and that IS the fix at this door rather than a deferral of it. The
    defect is not "branch queries are missing"; it is that the parameter is accepted and disregarded, so
    a caller staging work on a branch reads main and is told nothing. Answering "this backend does not
    do that" is correct, complete and spec-shaped — error code 0, `Unsupported` — where answering with
    another dataset's rows is neither.

    THE INDEX DOORS ARE HERE TOO, and they are WRITES, which makes the refusal more urgent rather than
    less. Measured live 2026-08-31: `create_scalar_index` with `branch=work` returned 200, MAIN advanced
    a version and took the index, and the branch got none. An index on the wrong dataset is not inert —
    it changes which plans the query engine picks for every later reader of main.

    NOT the same call as `count_rows`, which is honoured rather than refused: a count is a single
    documented operation over a dataset handle, so serving it branch-aware adds no second definition of
    anything. A faithful branch-scoped `query` would have to re-derive vector search, full-text search,
    prefilter, nprobes, refine_factor and distance_type against a handle, and a subtle divergence there
    is a wrong answer wearing the right shape — the failure this whole file exists to stop. The index
    doors sit on the same side of that line for the same reason: `CreateTableIndexRequest` carries a
    whole full-text-search option surface (`base_tokenizer`, `language`, `stem`, `ascii_folding`,
    `remove_stop_words`, `with_position`, `max_token_length`, `lower_case`) plus vector parameters, and
    silently building an index with a DEFAULT where the caller asked for a setting is the same class of
    quiet wrongness as building it on the wrong dataset.

    Both implementations are real work with their own tests; they are named in the lakehouse register, row O1 (drained 2026-09-10; in git history), not
    smuggled in behind a door that currently lies.

    `main` names the branch every one of these doors serves: "The exact name `main` is a virtual name for the default
    branch. It may appear in API reference contexts as an alias for the default branch"
    (`lance_docs/lance/format/table/branch_tag.md` § Branch Naming), so it is read as no branch ([[LH-547]]).
    """
    if recorded_branch(branch) is not None:
        # ONE EXPRESSION DECIDES THE CODE, and `remedy` exists so that staying inside it costs a door
        # nothing. `describe_table` refused this same condition inline with `InvalidInputError` (13)
        # while fifteen doors answered `UnsupportedOperationError` (0) — the spec calls 13 "Malformed
        # request or invalid parameters" (`spec.yaml:2425`) and 0 "Operation not supported by this
        # backend" (`:2412`), and a well-formed branch name this backend does not serve is the second.
        # A client dispatching on the 24 codes cannot tell two answers apart as one condition, so the
        # divergence was a contract defect rather than a wording one. Doors differ in what the caller
        # should do INSTEAD; they must not differ in what kind of thing happened.
        raise UnsupportedOperationError(
            f"{door} cannot be scoped to a branch {branch!r}: the underlying implementation answers from the main branch regardless of "
            f"`branch`, so honouring the parameter here would return main's rows labelled as {branch!r}. "
            + (remedy or "Read the branch through `count_rows`, or query it directly.")
        )


class RowCount(BaseModel):
    """A count and the version it was counted at, which is what the door's read record states."""

    count: int
    version: int


def count_rows(table: table_claims.DoorTable, so: StorageOptions, req: CountTableRowsRequest, *, data_ns: LanceNamespace) -> RowCount:
    """Count rows on the ref the request NAMES — the read-side twin of `update_table`'s omission; main's native count runs on ``data_ns``.

    The upstream `count_table_rows` takes the whole request and answers from main whatever `branch`
    says, so a branch-scoped count returned a number that was correct for a dataset the caller did not
    ask about. Verified live 2026-08-31 against ground truth read from S3 with pylance: after deleting
    two of three rows on `work`, the branch held 1 and this door reported 3 — and a branch that had
    NEVER BEEN CREATED also reported 3, which is what proves the parameter was not read rather than
    mishandled.

    Spec-mandated on both counts. `CountTableRowsRequest.branch` is "Branch to target. When not
    specified, the main branch is used" (lance_docs/ns_catalog/namespace/operations/models/CountTableRowsRequest.md), and the SDK states the general rule for a
    branch-scoped handle: "Reads and writes operate in the branch's context." An absent ref is error
    code 22 `TableBranchNotFound`, which the door's open already raises — so routing through it buys the
    404 as well as the correct answer.

    MAIN STAYS ON THE UPSTREAM PATH. A branchless count is the overwhelmingly common one and already
    correct; re-implementing it here would put a second definition of "count" in the estate for no
    defect, and any divergence in predicate dialect or version resolution would be ours to own. The native count
    names the table, so the answer is kept only when the name still resolves to the door's location ([[LH-530]]).
    """
    refuse_an_unbounded_boolean_chain(req.predicate, field="predicate")
    if req.branch is None:
        # PINNED TO THE VERSION JUDGED ([[LH-279]]): the native count opens the table inside Rust, so the
        # request carries the exact version whose bases were just checked.
        req.version = int(table.open(so, version=req.version).version)
        response = table.read_natively("count_table_rows", req, via=data_ns)
        if not isinstance(response, CountTableRowsResponse) or response.count is None:
            raise TypeError(f"count_table_rows must answer a CountTableRowsResponse with a count, got {type(response).__name__}: {response!r}")
        return RowCount(count=response.count, version=req.version)
    dataset = table.open(so, version=req.version, branch=req.branch)
    with caller_sql("invalid count predicate"):
        counted = dataset.count_rows(filter=req.predicate) if req.predicate else dataset.count_rows()
    return RowCount(count=int(counted), version=int(dataset.version))


class _ChunkSink(io.RawIOBase):
    """A write target that holds only what the IPC writer has emitted since the last drain.

    ON THE DOCUMENTED PATH, not a workaround: Arrow's IPC guide states the record-batch writers
    "can write to a writeable ``NativeFile`` object **or a writeable Python object**", and
    `pa.output_stream`'s own signature takes ``source : str, Path, buffer, file-like object``. A
    Python sink is first-class API, so `pa.ipc.new_file` takes this directly with no wrapper.

    It exists because the obvious sink cannot stream. `pa.BufferOutputStream` accumulates every byte
    until `getvalue()` — which is the one-shot shape, and the shape every FastAPI+Arrow example
    reaches for (`BytesIO` + `RecordBatchFileWriter` + `Response`). That is correct for a small
    answer and is precisely what costs 2.5x the payload on a large one. Draining after each batch
    hands the bytes out and forgets them, so what is held is one batch rather than the whole table.

    `io.RawIOBase` rather than a bare class with `write`: it supplies `writable()`, `closed` and the
    context-manager protocol that pyarrow's `PythonFile` probes for, without restating them.
    """

    def __init__(self) -> None:
        self._parts: list[bytes] = []

    def writable(self) -> bool:
        return True

    def write(self, b: Any) -> int:  # noqa: ANN401 — pyarrow writes bytes, memoryview or bytearray
        payload = bytes(b)
        self._parts.append(payload)
        return len(payload)

    def drain(self) -> bytes:
        """Everything written since the last call, and nothing after it."""
        out = b"".join(self._parts)
        self._parts.clear()
        return out


def _arrow_file_chunks(schema: pa.Schema, first: pa.RecordBatch | None, rest: Iterator[pa.RecordBatch]) -> Iterator[bytes]:
    """One Arrow FILE, yielded a batch at a time — byte-identical to the one-shot form.

    Byte-identical is verified, not assumed (2026-09-22, 58,410,370 bytes from both paths over the
    same projection, matching schema and rows). It matters because it is what makes this a pure
    memory change: no consumer can tell the two apart, so nothing downstream has to be migrated.

    FILE framing rather than STREAM is not a detail to trade away for streamability: `/query` answers
    `application/vnd.apache.arrow.file` and a consumer handed the other framing fails at the first
    batch. The footer that makes it a FILE is written by `new_file.__exit__`, so it arrives as the
    last drain — which is exactly why the close happens inside this generator rather than before it.

    ``first`` is passed in already pulled: its caller needs the pull to happen under its own error
    guard, and re-deriving it here would either lose that batch or scan twice.
    """
    sink = _ChunkSink()
    # `first is None` means the scan was empty, so `rest` is exhausted too and chaining it in would
    # hand `write_batch` a None. The file is still written: an empty FILE is a schema and a footer,
    # which is what a consumer asking a window nothing changed in must receive.
    batches = rest if first is None else itertools.chain((first,), rest)
    with pa.ipc.new_file(sink, schema) as writer:
        for batch in batches:
            writer.write_batch(batch)
            if chunk := sink.drain():
                yield chunk
    if chunk := sink.drain():
        yield chunk


def read_changes(
    table: table_claims.DoorTable,
    so: StorageOptions,
    *,
    begin_version: int,
    end_version: int | None,
    kind: ChangeKind,
    columns: list[str] | None = None,
    branch: str | None = None,
) -> tuple[int, Iterator[bytes]]:
    """The version the window ends at, and the rows that changed in ``(begin_version, end_version]``, Arrow FILE-framed (§ J4), handed out in pieces.

    ONE SNAPSHOT, the window's end. The dataset is opened AT ``end_version`` — at the head it read, for
    an open window — and every kind is answered from that handle, so a closed window answers the same set
    however far the table has moved on (`changes` module docstring, THE WINDOW IS ONE SNAPSHOT). Before
    any row is read the table must track row versions (`changes.require_row_versions`) and the window
    must hold no transaction the version columns cannot describe (:func:`_require_followable_window`).

    `inserted` and `updated` are scans with the predicate `services/changes.py` composes, which owns the
    two documented windows; this function does not know what a change is and must not learn — a second
    place that composes either is a second place it can drift from `lance_docs/lance/format/table/row_id_lineage.md`. Its own scan rather
    than the query door's, because `QueryTableRequest` is a VECTOR model (`k` and `vector` required).

    `deleted` is not a predicate: the rows are gone from the scan, so Lance answers from the transaction
    range instead (`DatasetDelta.get_deleted_row_ids()`, a single `_rowid` column). A writer that hard-deletes
    with `when_not_matched_by_source_delete` leaves no row for the inserted and updated windows to show, so
    without this a retracted row left the table without a trace any consumer could follow. Served on
    demand rather than stamped into an event (owner decision, 2026-09-11): a deleted-row set is
    unbounded, and stamping it turns a large delete into a large message on the bus.

    FILE FRAMING, not stream: this answers `application/vnd.apache.arrow.file`, matching the query
    door, and a consumer reading one framing as the other fails at the first batch rather than
    degrading. THE PROJECTION IS PART OF THE ANSWER: Lance returns only data columns unless the version
    pseudo-columns are named, so a feed that passed the caller's `columns` through verbatim handed back
    changed rows with no version on them and no way to ask for the next window.

    IT YIELDS rather than returns, because the answer is sized by the DATA and this door has no bound
    to put on it — no `limit`, no page token, and none available: the version window is the only
    cursor a consumer has, and one version can carry the whole table. MEASURED 2026-09-22 (200k rows x
    256B, 58.4 MB of Arrow over the feed's five projected columns): the materialising form peaked at
    **147.6 MB of RSS for one call, 2.53x the payload**; yielding peaks at **60.1 MB, 1.03x**, and the
    wire output is byte-identical (58,410,370 both). RSS is the measurement because `tracemalloc`
    reports 0 MB for the scan and the encode, watching the Python heap while Arrow allocates elsewhere.

    YIELDING BOUNDS WHAT THIS FUNCTION HOLDS, NOT WHAT LANCE HOLDS. The scan itself runs under the
    process scan defaults the catalog sets at boot (`catalog.main.bound_lance_scans`, [[LH-247]]):
    Lance's own size a batch in ROWS (8,192, whatever their width) and buffer up to 2 GB of IO ahead of
    the consumer (`lance_docs/lance/guide/performance.md:297-318`), and on a 164 MB table of 1024-dim float32 vectors
    this feed peaked the catalog at 597,224 kB, past a 524,288 kB limit (pylance 12.0.0, measured
    from outside a one-CPU catalog over a moto store). No scanner option is passed here, so the feed
    and the native backend's scans share one bound an operator can move in one place.

    Raises:
        InvalidInputError: an invalid window, one wider than :data:`MOST_VERSIONS_IN_A_CHANGE_WINDOW`, or a predicate
            or projection Lance rejects.
        TableVersionNotFoundError: ``end_version`` is not a version of the ref.
        InvalidTableStateError: the table has no stable row ids, or the window spans a transaction the
            version columns cannot describe or whose record cannot be read.
    """
    changes.validate_window(begin_version=begin_version, end_version=end_version)
    label = ".".join(table.segments)
    dataset = table.open(so, version=end_version, branch=branch)
    changes.require_row_versions(dataset.has_stable_row_ids, table=label)
    end = int(dataset.version)
    if begin_version > 0 and end - begin_version > MOST_VERSIONS_IN_A_CHANGE_WINDOW:
        raise InvalidInputError(
            f"the change window on table {label} spans {end - begin_version} versions and one request follows at most "
            f"{MOST_VERSIONS_IN_A_CHANGE_WINDOW}: ask with end_version={begin_version + MOST_VERSIONS_IN_A_CHANGE_WINDOW}, then from there"
        )
    spans = _window_spans(dataset, begin_version, end, branch=branch)
    if begin_version > 0:
        _require_followable_window(spans, table=label)
    if kind == "deleted":
        # A window from version 0 names only rows the consumer never held, and `delta()` would need the
        # manifest of version 1, which maintenance cleanup removes from every table older than its
        # retention: that resynchronisation is answered empty rather than read.
        return end, (_deleted_row_ids(spans) if begin_version > 0 else _arrow_file_chunks(_DELETED_SCHEMA, None, iter(())))
    predicate = changes.change_filter(begin_version=begin_version, end_version=end, kind=kind)
    projection = changes.feed_projection(columns, data_columns=dataset.schema.names)
    with caller_sql("invalid change-feed predicate"):
        scanner = dataset.scanner(filter=predicate, columns=projection)
        # THE FIRST BATCH IS PULLED INSIDE THE GUARD, and that placement is the whole reason this is
        # not one line. `to_batches()` is lazy, so a malformed predicate does not raise until the
        # first pull — and a generator's body does not run until the response is already streaming,
        # where a raise becomes a truncated 200 instead of the 400 `caller_sql` exists to produce.
        batches = scanner.to_batches()
        first = next(batches, None)
    return end, _arrow_file_chunks(scanner.projected_schema, first, batches)


#: The most versions one change-feed request follows. The feed reads one transaction per version of its window before
#: it answers (:func:`_require_followable_window`), a store read each, so an unbounded window lets any reader have the
#: catalog make one read per version a table ever had. A window from version 0 reads none and is not bounded.
MOST_VERSIONS_IN_A_CHANGE_WINDOW: Final = 1000


class _WindowSpan(BaseModel):
    """The part of a window one ref committed: versions ``(low, high]`` of ``handle``."""

    model_config = {"arbitrary_types_allowed": True}

    handle: lance.LanceDataset
    #: The ref ``handle`` is on, `None` for main — what `checkout_version((ref, v))` names.
    ref: str | None
    low: int
    high: int


def _window_spans(dataset: lance.LanceDataset, begin: int, end: int, *, branch: str | None) -> list[_WindowSpan]:
    """``(begin, end]`` split by the ref that committed each version, newest first.

    A branch's own versions start above its `parent_version`; everything at or below it is its parent's
    history, read on the parent's handle at that version (`checkout_version((parent_branch, v))`, where
    `None` is main; measured on pylance 12.0.0, nested branches included). The branch's first manifest
    carries the parent's version number and the `Clone` that made the branch, never a write's transaction
    (measured on pylance 13.0.0), while the versions below it do not exist on the branch at all — so no
    span ever asks a branch for a version at or below its branch point.
    """
    spans: list[_WindowSpan] = []
    handle, high, name = dataset, end, recorded_branch(branch)
    while True:
        if name is None:
            spans.append(_WindowSpan(handle=handle, ref=None, low=begin, high=high))
            break
        listed = handle.branches.list().get(name) or {}
        parent = listed.get("parent_version")
        floor = int(parent) if isinstance(parent, int) else 0
        spans.append(_WindowSpan(handle=handle, ref=name, low=max(begin, floor), high=high))
        if begin >= floor:
            break
        parent_branch = listed.get("parent_branch")
        name = parent_branch if isinstance(parent_branch, str) else None
        handle, high = handle.checkout_version((name, floor)), floor
    return [span for span in spans if span.high > span.low]


def _require_followable_window(spans: list[_WindowSpan], *, table: str) -> None:
    """Refuse a window spanning a transaction the version columns cannot describe (`changes.unfollowable`).

    One `read_transaction` per version in the window, and the window's BEGIN manifest must still exist:
    `delta()` reads it for the `deleted` kind, so a window whose begin maintenance cleaned up is refused
    here for every kind alike rather than answered by the scans and failed by the delta. A failed read
    refuses the window rather than answering it: a cleaned-up version's manifest is gone (OSError,
    measured on 12.0.0), and a read that PANICS raises a pyo3 `PanicException`, which derives from
    BaseException and cannot be caught by name.

    Raises:
        InvalidTableStateError: a version in the window is unfollowable or unreadable.
    """
    for span in spans:
        for version in range(span.high, span.low, -1):
            _require_followable_version(span, version, table=table)
    if not spans:
        return
    lowest = spans[-1]
    try:
        lowest.handle.checkout_version((lowest.ref, lowest.low))
    except BaseException as exc:  # noqa: BLE001 — a Rust PANIC is not an Exception
        if isinstance(exc, KeyboardInterrupt | SystemExit):
            raise
        raise_if_store_outage(exc, table=table)
        raise InvalidTableStateError(
            f"the change window on table {table} cannot be followed: its begin version {lowest.low} is no longer retained "
            f"({type(exc).__name__}). Resynchronise from begin_version=0."
        ) from exc


def _require_followable_version(span: _WindowSpan, version: int, *, table: str) -> None:
    """Refuse ``version`` of ``span``'s ref when its transaction is unreadable or one the version columns cannot describe."""
    try:
        transaction = read_transaction(span.handle, version)
    except BaseException as exc:  # noqa: BLE001 — a Rust PANIC is not an Exception
        if isinstance(exc, KeyboardInterrupt | SystemExit):
            raise
        raise_if_store_outage(exc, table=table)
        raise InvalidTableStateError(
            f"the change window on table {table} cannot be followed: version {version}'s transaction is unreadable "
            f"({type(exc).__name__}). Resynchronise from begin_version=0."
        ) from exc
    if (reason := changes.unfollowable(transaction)) is not None:
        raise InvalidTableStateError(
            f"the change window on table {table} cannot be followed: version {version} {reason}, so the version columns "
            "do not describe what changed. Resynchronise from begin_version=0."
        )


#: The deleted-row feed's schema: Lance's own answer is a single `_rowid` column.
_DELETED_SCHEMA: Final = pa.schema([pa.field("_rowid", pa.uint64())])


def _deleted_row_ids(spans: list[_WindowSpan]) -> Iterator[bytes]:
    """The `_rowid`s each span's ref deleted, Arrow FILE-framed.

    One `delta()` per span, because `delta()` on a branch refuses a begin below its branch point (the
    manifest is not there; measured on 12.0.0). Each is closed (`delta()` refuses an open window,
    "Must specify both with_begin_version and with_end_version"). The caller asks only for a window with
    a begin above 0, whose begin manifest `_require_followable_window` has already found.
    """
    readers = []
    with caller_sql("invalid deleted-row window"):
        for span in spans:
            readers.append(span.handle.delta(begin_version=span.low, end_version=span.high).get_deleted_row_ids())
        batches = (*reader for reader in readers)
        first = next(batches, None)
    return _arrow_file_chunks(readers[0].schema if readers else _DELETED_SCHEMA, first, batches)
