"""The stage write: everything from a run's transformed rows to its ONE marked commit on the destination tier.

Both cascade engines import this module and nothing else for their write. The in-process engine
(``medallion.services.compute.transform_stage``) produces a table for a tabular upstream and a re-runnable stream
of slices for a blob upstream; the Ray stage job
(``scripts/ray_stage_job.py``) produces a table on the driver for the head and delta lanes, a re-runnable stream of
batches for the media lane, and a staged Lance dataset for the distributed lane. How rows are produced is each
engine's business. What a tier write MEANS is this module's, so a write-semantics fix lands once for both.

A run goes through two calls:

* :func:`plan_window` decides, before any row is read, whether the run converges the whole tier or only the rows
  its upstream changed since the order's version floor, and refuses a destination without stable row ids;
* :func:`write_tier` makes the run's commits, in this order: disarm commit-path auto-cleanup ([[LH-245]]), retract
  the upstream's deletions (delta lane), widen the tier by id, correct the declared dataset id, carry the
  upstream's governance labels, then ONE converge or create carrying the run's commit marker (CP-029 D-5). The
  lineage JSON index is built after it, and the stage contract is checked last.

WHY THE MARKED COMMIT IS LAST. ``commit_marker`` defines "the run's marker is in the destination's history" as
"every write of the run landed", so every commit the run cannot mark (a delete, a metadata update, a by-id merge)
is ordered before the one it can. The lineage index is the exception and is built after the marked commit: an
index changes no row (``lineage/core/reconcile.py`` classifies ``CreateIndex`` as maintenance), a merge leaves an
index built before it covering none of the rows it rewrote, and its absence costs a reader a scan, never a wrong
answer. The version this module reports is the marked commit's, never the index's.

Lance's own distributed-write model is the same shape: fragments are written in parallel and then ONE operation is
committed (``lance_docs/guide.md:1530-1540``). A merge reaches that shape through ``execute_uncommitted`` and a
stamped ``LanceDataset.commit``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from typing import Final, Literal

import lance
import pyarrow as pa
from lance import blob_array
from lance.blob import BlobType
from lance.commit import CommitConflictError
from lance.indices.builder import IndexConfig
from pydantic import BaseModel, ConfigDict

from service_kit.lakehouse import auto_cleanup, blobs
from service_kit.lakehouse.commit_marker import CommitMarker, stamped
from service_kit.lakehouse.stage_stamp import (
    CARDINALITIES,
    LINEAGE_COLUMN,
    ONE_TO_ONE,
    SOURCE_ROWID_COLUMN,
    UnstableRowIdsError,
    declare_dataset_id,
    ensure_declared_dataset_id,
)


log = logging.getLogger(__name__)

#: What a run hands :func:`write_tier`: rows in memory, a staged Lance dataset the converge streams, or a factory
#: that produces a fresh stream of batches each time it is called. A stream is a factory because a marked merge
#: that loses a commit race is planned again, and a consumed reader cannot be read twice.
type RowSource = pa.Table | lance.LanceDataset | Callable[[], pa.RecordBatchReader]

#: How many times a marked merge that lost a race is planned again against the newer version. The same bound
#: `MergeInsertBuilder.conflict_retries` defaults to ("Default is 10", pylance 12.0.0), so a marked merge survives
#: the contention an unmarked `.execute()` survives.
MERGE_CONFLICT_RETRIES: Final = 10

#: How many bytes of a streamed run one converge commit carries (:func:`_converge_stream`). 16 MiB keeps an in-process
#: re-run of 1 MiB rows to ~0.17 GB of growth inside a 512 Mi pod (`medallion.core.config.stage_commit_mb`, measured on
#: pylance 12.0.0) while a run of small rows commits a few times rather than once per slice.
STREAM_COMMIT_BYTES: Final = 16 << 20

#: How many times a widening re-reads the tier after another commit passed the version it read. A maintenance
#: compaction is the expected concurrent writer, and it does not land three times in one run.
WIDEN_ATTEMPTS: Final = 3

#: The field-metadata namespace the estate acts on ([[LH-058]]): `rask.classification` decides whether a table's
#: bytes may be vended raw (`catalog.core.vending.CLASSIFICATION_KEY`).
GOVERNANCE_FIELD_PREFIX: Final = "rask."

#: The manifest name a tier registers its inherited external blob base under. One base per dataset, matching what
#: ingest registers, so a descriptor's `blob_id` stays unambiguous.
EXTERNAL_BASE_NAME: Final = "source"

#: The JSON scalar index over ``lineage -> run_id`` (R26's "indexable" half): `run_id` is the join key back to the
#: lineage graph, so a consumer filtering ``json_get_string(lineage, 'run_id') = ...`` gets an index, not a scan.
LINEAGE_INDEX_NAME: Final = "lineage_run_id_idx"
LINEAGE_INDEX_PATH: Final = "run_id"

#: Lance's reserved row-identity metacolumn, the cascade head's join key for a retraction.
_ROWID: Final = "_rowid"

#: How many keys go into one `IN (...)` predicate. A caller may delete a million rows in one version, so every
#: predicate built from Lance's deletion record is chunked rather than sized by the deletion.
RETRACT_CHUNK: Final = 1000


class StageContractError(RuntimeError):
    """A run's written rows break what its lane owes the tier: a row with no parent, or a 1:1 count that moved."""


class EmptyFullSyncError(RuntimeError):
    """A full-sync converge was handed zero rows for an upstream that holds rows.

    `when_not_matched_by_source_delete` against an empty source matches every destination row, so landing it would
    empty the tier. A stage that read rows and produced none (a filter that matched nothing, a half-failed
    distributed write) is refused rather than read as "the tier is now empty".
    """


class TierTarget(BaseModel):
    """The destination of one stage run, and what its write must carry."""

    model_config = ConfigDict(frozen=True)

    uri: str
    storage_options: dict[str, str]
    #: The destination's canonical catalog name, declared on its schema. Empty means unwired: the producer's stamp
    #: then drops an inherited name, and nothing here writes one.
    dataset_id: str = ""
    #: The lane's declared row cardinality (`stage_stamp.CARDINALITIES`).
    cardinality: str = ONE_TO_ONE
    #: The run's commit marker, carried by its last data commit. ``None`` marks nothing (a run with no order key).
    marker: CommitMarker | None = None


class Window(BaseModel):
    """Which upstream rows a run converges: all of them, or those changed since ``floor``."""

    model_config = ConfigDict(frozen=True)

    #: The delta boundary in force, exclusive. ``None`` is a full run.
    floor: int | None = None

    @property
    def row_filter(self) -> str | None:
        """The change-data-feed predicate an engine reads the upstream with, or ``None`` for every row.

        ONE COLUMN ANSWERS BOTH HALVES of "what changed since N": a row never updated carries
        ``_row_last_updated_at_version`` equal to its creation version (measured 2026-09-11: three rows created at
        v1, one appended at v2, one updated at v3 read `[1, 1, 2, 3]`), so `> N` selects inserted and updated rows
        together, where `_row_created_at_version` (`lance_docs/file_format.md:4277-4285`) drops every in-place
        correction. A compaction does not widen it: `compact_files` and `cleanup_old_versions` leave both version
        columns byte-identical (measured 2026-09-11).
        """
        return None if self.floor is None else f"_row_last_updated_at_version > {self.floor}"


class TierWriteResult(BaseModel):
    """What one run's write did."""

    model_config = ConfigDict(frozen=True)

    #: The destination version of the run's marked data commit, ``None`` when an empty delta committed no data.
    version: int | None
    lane: Literal["full", "delta"]
    rows_in: int
    rows_out: int
    #: Destination rows the delta lane deleted because Lance recorded their upstream row as deleted.
    retracted: int = 0


def _open(uri: str, storage_options: dict[str, str], session: lance.Session | None) -> lance.LanceDataset | None:
    """The dataset at ``uri``, or ``None`` when there is none.

    A read, not a stat: an object store has no directories. Absent, unreadable or not a dataset all mean "create",
    which is the answer both engines gave the create-or-converge question.
    """
    try:
        return lance.dataset(uri, storage_options=storage_options, session=session)
    except Exception:  # noqa: BLE001 — every failure to open means "there is no tier here to converge into"
        return None


def plan_window(upstream: lance.LanceDataset, target: TierTarget, version_floor: int | None, *, session: lance.Session | None = None) -> Window:
    """The rows this run converges, decided before any row is read.

    A FULL RUN unless every condition for a delta holds: the order names a floor, the upstream carries no blob
    column, and the destination exists. A blob tier converges whole because its derived artifacts are decided from
    the column's first non-null payload, which a window cannot see. A destination that does not exist yet has
    nothing a delta could be merged into, and its first run creates it whole.

    Raises:
        UnstableRowIdsError: the destination exists without stable row ids. Its `_rowid` is a physical address
            (`lance_docs/file_format.md:4011-4015`), so the tier above's `source_rowid` cannot name its rows, and the
            property is create-time-only: no run can repair it.
    """
    destination = _open(target.uri, target.storage_options, session)
    if destination is not None and not destination.has_stable_row_ids:
        raise UnstableRowIdsError(
            f"{target.uri} was created without stable row ids, so no stage run may converge into it; recreate it with "
            "enable_stable_row_ids=True (it is create-time-only)"
        )
    if version_floor is None or destination is None or blobs.blob_field_names(upstream.schema):
        return Window()
    return Window(floor=version_floor)


def carried_blob_column(
    upstream: lance.LanceDataset, name: str, scanned: list[object] | pa.ChunkedArray, row_ids: list[int], external_base: str | None
) -> tuple[pa.Field, pa.Array]:
    """One upstream blob column as a downstream write takes it: the UPSTREAM field, and the values to write.

    The field is the upstream's own, never a fresh `blob_field(name)`: Blob V2 thresholds and `rask.classification`
    are this field's metadata (lance_docs/guide.md, blob v2), and a rebuilt field creates the tier above declassified
    and on default placement ([[LH-217]]).

    ``scanned`` is one scan's values for the column, row-aligned with ``row_ids``. Without an external base the scan
    was ``blob_handling="all_binary"`` and the values are the payload bytes. With one it was the default handling and
    the values are descriptors: kind-3 rows are forwarded as pointers and every other non-null row's bytes are read by
    row id (`blobs.carried_blob_values`), because an external base does not make every row external.

    Managed bytes handed over as the scanned Arrow column are wrapped without leaving Arrow (:func:`_bytes_as_blobs`);
    a list goes through `blob_array`, which copies every payload into Python and back.
    """
    field = upstream.schema.field(name)
    if external_base:
        descriptors = scanned.to_pylist() if isinstance(scanned, pa.ChunkedArray) else scanned
        return field, blob_array(blobs.carried_blob_values(upstream, name, descriptors, row_ids, external_base))
    if isinstance(scanned, pa.ChunkedArray):
        return field, _bytes_as_blobs(scanned)
    return field, blob_array(scanned)


def _bytes_as_blobs(scanned: pa.ChunkedArray) -> pa.Array:
    """A scanned ``large_binary`` payload column as the blob array `blob_array` would build from the same bytes.

    The storage `lance.blob.BlobArray.from_pylist` builds for inline bytes is ``struct<data, uri, position, size>``
    with only ``data`` set and the struct null where the payload is; this builds it around the scanned buffer instead
    of copying each payload through a Python ``bytes`` (measured on pylance 12.0.0: storage-equal to
    ``blob_array(scanned.to_pylist())`` over ``[10 B, None, 1 MiB]``, and the written tier reads back byte-identical).
    """
    data = scanned.combine_chunks() if scanned.num_chunks != 1 else scanned.chunk(0)
    rows = len(data)
    storage = pa.StructArray.from_arrays(
        [data.cast(pa.large_binary()), pa.nulls(rows, pa.utf8()), pa.nulls(rows, pa.uint64()), pa.nulls(rows, pa.uint64())],
        names=["data", "uri", "position", "size"],
        mask=data.is_null(),
    )
    return pa.ExtensionArray.from_storage(BlobType(), storage)


def blob_handling(external_base: str | None) -> Literal["all_binary"] | None:
    """The scan handling :func:`carried_blob_column` expects: bytes for a managed upstream, descriptors otherwise."""
    return None if external_base else "all_binary"


def retract_deleted(upstream: lance.LanceDataset, floor: int, destination: lance.LanceDataset) -> int:
    """Delete tier rows whose upstream row was deleted since ``floor``, and answer how many.

    A delta's source is only what changed, so the full lane's `when_not_matched_by_source_delete` would delete
    everything the delta did not carry. The deleted set is Lance's own record instead:
    `upstream.delta(begin_version=floor).get_deleted_row_ids()` names the stable `_rowid`s deleted in the window
    (measured on pylance 12.0.0: one `delete("id = 2")` answers `[1]`; a compaction window answers `[]`). The cost is
    sized by the deletion: no key column is read whole on either side.

    THE JOIN IS ROOT PROVENANCE. `stage_stamp.carry_source_rowid` keeps `source_rowid` across hops, so
    `gold.source_rowid == silver.source_rowid == bronze._rowid`. At the cascade head the deleted `_rowid`s are the
    keys; deeper in, each is mapped to its `source_rowid` through the upstream at ``floor``, where the deleted rows
    still exist. A `1:N` lane shares one root key between siblings, so a key is retracted only when no upstream row
    still carries it.

    Raises:
        UnstableRowIdsError: the upstream has no stable row ids, so its `_rowid` is a physical address and its
            deleted-row record does not exist (`lance_docs/file_format.md:4011-4015`).
    """
    if not upstream.has_stable_row_ids:
        raise UnstableRowIdsError(f"{upstream.uri} was created without stable row ids, so a delta run cannot follow its deletions; rebuild it with a full run")
    if SOURCE_ROWID_COLUMN not in destination.schema.names:
        return 0
    deleted = sorted(
        key for batch in upstream.delta(begin_version=floor, end_version=upstream.version).get_deleted_row_ids() for key in batch.column(_ROWID).to_pylist()
    )
    if not deleted:
        return 0
    if SOURCE_ROWID_COLUMN in upstream.schema.names:
        before = upstream.checkout_version(floor)
        candidates = sorted(
            {
                key
                for where in _in_chunks(_ROWID, deleted)
                for key in before.to_table(columns=[SOURCE_ROWID_COLUMN], filter=where).column(SOURCE_ROWID_COLUMN).to_pylist()
                if key is not None
            }
        )
        surviving = {
            key
            for where in _in_chunks(SOURCE_ROWID_COLUMN, candidates)
            for key in upstream.to_table(columns=[SOURCE_ROWID_COLUMN], filter=where).column(SOURCE_ROWID_COLUMN).to_pylist()
        }
        dead = [key for key in candidates if key not in surviving]
    else:
        dead = deleted
    for where in _in_chunks(SOURCE_ROWID_COLUMN, dead):
        destination.delete(where)
    return len(dead)


def _in_chunks(column: str, keys: list[object]) -> list[str]:
    """`column IN (...)` predicates over ``keys``, `RETRACT_CHUNK` keys each."""
    return [f"{column} IN ({', '.join(_literal(key) for key in keys[start : start + RETRACT_CHUNK])})" for start in range(0, len(keys), RETRACT_CHUNK)]


def _literal(key: object) -> str:
    """A key as a SQL literal in a Lance filter: an integer as itself, a string quoted with its quotes doubled.

    Raises:
        TypeError: a key of any other type, which a predicate built here could not name exactly.
    """
    if isinstance(key, int) and not isinstance(key, bool):
        return str(key)
    if isinstance(key, str):
        return "'" + key.replace("'", "''") + "'"
    raise TypeError(f"a tier key must be an integer or a string to be retracted by key, not {type(key).__name__}")


def write_tier(
    upstream: lance.LanceDataset,
    rows: RowSource,
    window: Window,
    target: TierTarget,
    *,
    session: lance.Session | None = None,
    commit_bytes: int = STREAM_COMMIT_BYTES,
) -> TierWriteResult:
    """Land one run's rows on the destination tier, ending on the run's ONE marked commit.

    ``upstream`` is the dataset the engine read, at the version it read; ``rows`` are what the engine produced from
    it under ``window``; the provenance columns (`stage_stamp.stamp_stage`) are already on them.

    A FULL RUN converges the tier to exactly ``rows``: the destination is created (file format 2.2, stable row ids,
    the upstream's external base registered) or merged into on `id` with `when_not_matched_by_source_delete`. A merge
    keeps every surviving row's stable `_rowid`, which the tier above resolves its `source_rowid` against; an
    overwrite would re-mint every one (`lance_docs/file_format.md:3998` against `:4025`).

    A DELTA RUN first retracts what the upstream deleted in the window (:func:`retract_deleted`), then merges
    ``rows`` on `id` with no retraction clause. A delta that produced no rows commits no data and answers ``version``
    ``None``: a redelivered event whose rows were already processed lands here, and an empty version would announce
    data nobody added. A retraction it made carries no marker, so a reader that finds none resubmits the run, which
    converges.

    Raises:
        EmptyFullSyncError: a full run produced no rows from an upstream that holds rows.
        StageContractError: the written rows break the lane's contract (checked after the commit, so a run that
            committed still reports failed).
        TypeError: a delta run was handed a stream; the delta lane reads a bounded window into memory.
        lance.commit.CommitConflictError: the marked merge lost every race, or an incompatible commit replaced the
            table under it.
    """
    so = target.storage_options
    rows_in = upstream.count_rows(filter=window.row_filter)
    destination = _open(target.uri, so, session)
    if destination is not None and (removed := auto_cleanup.disarm(destination)):
        log.warning("tier_write_auto_cleanup_disarmed", extra={"uri": target.uri, "keys": removed})
    if isinstance(rows, pa.Table):
        rows = declare_dataset_id(rows, target.dataset_id or None)

    retracted = 0
    if window.floor is not None:
        if destination is None:
            raise ValueError(f"a delta run needs an existing destination; plan_window answers a full run for {target.uri}")
        if not isinstance(rows, (pa.Table, lance.LanceDataset)):
            raise TypeError(f"a delta run converges a bounded window and takes a table or a dataset, not {type(rows).__name__}")
        retracted = retract_deleted(upstream, window.floor, destination)
        if _known_count(rows) == 0:
            log.info("tier_write_delta_empty", extra={"uri": target.uri, "floor": window.floor, "retracted": retracted})
            return TierWriteResult(version=None, lane="delta", rows_in=rows_in, rows_out=0, retracted=retracted)
    elif rows_in and _known_count(rows) == 0:
        raise EmptyFullSyncError(
            f"the run produced no rows from {upstream.uri}, which holds {rows_in}; refusing a full-sync converge that would empty {target.uri}"
        )

    counted: list[int] = []
    if destination is None:
        version = _create(upstream, rows, target, counted)
    else:
        _widen_by_id(rows, target, session)
        ensure_declared_dataset_id(target.uri, target.dataset_id, so, session=session)
        _carry_governance_labels(upstream.schema, target.uri, so, session)
        version = (
            _converge(rows, target, full_sync=window.floor is None)
            if isinstance(rows, (pa.Table, lance.LanceDataset))
            else _converge_stream(rows, target, counted, rows_in=rows_in, upstream_uri=upstream.uri, commit_bytes=commit_bytes)
        )
    rows_out = _known_count(rows)
    if rows_out is None:
        rows_out = counted[-1]

    written = lance.dataset(target.uri, storage_options=so, session=session)
    if LINEAGE_COLUMN in written.schema.names:
        index_lineage(target.uri, so, session=session)
    parentless = written.count_rows(filter=f"{SOURCE_ROWID_COLUMN} IS NULL") if SOURCE_ROWID_COLUMN in written.schema.names else written.count_rows()
    assert_stage_contract(rows_in=rows_in, rows_out=rows_out, cardinality=target.cardinality, parentless=parentless)
    lane: Literal["full", "delta"] = "full" if window.floor is None else "delta"
    log.info("tier_write_landed", extra={"uri": target.uri, "lane": lane, "version": version, "rows_in": rows_in, "rows_out": rows_out, "retracted": retracted})
    return TierWriteResult(version=version, lane=lane, rows_in=rows_in, rows_out=rows_out, retracted=retracted)


def _known_count(rows: RowSource) -> int | None:
    """How many rows ``rows`` holds when that is knowable without producing them; ``None`` for a stream."""
    if isinstance(rows, pa.Table):
        return rows.num_rows
    if isinstance(rows, lance.LanceDataset):
        return rows.count_rows()
    return None


def _reader(rows: RowSource, counted: list[int]) -> pa.Table | lance.LanceDataset | pa.RecordBatchReader:
    """``rows`` as one attempt's source. A stream is produced afresh and its rows are counted into ``counted``."""
    if isinstance(rows, (pa.Table, lance.LanceDataset)):
        return rows
    stream = rows()
    counted.append(0)

    def batches() -> Iterator[pa.RecordBatch]:
        for batch in stream:
            counted[-1] += batch.num_rows
            yield batch

    return pa.RecordBatchReader.from_batches(stream.schema, batches())


def _create(upstream: lance.LanceDataset, rows: RowSource, target: TierTarget, counted: list[int]) -> int:
    """Write the destination into being, marked: 2.2, stable row ids, the upstream's external base registered.

    `enable_stable_row_ids` and `initial_bases` are create-time-only, which is why a tier is created once and merged
    into thereafter. The base must be registered on the tier a carried pointer lands in, or Lance refuses the write.
    """
    carried_base = blobs.external_base_of(upstream)
    created = lance.write_dataset(  # noqa: TID251 — the stage write is a registered write door
        _reader(rows, counted),
        target.uri,
        mode="create",
        storage_options=target.storage_options,
        data_storage_version="2.2",
        enable_stable_row_ids=True,
        initial_bases=[lance.DatasetBasePath(carried_base, EXTERNAL_BASE_NAME)] if carried_base else None,
        transaction_properties=target.marker.properties() if target.marker is not None else None,
    )
    return int(created.version)


def _converge(rows: pa.Table | lance.LanceDataset, target: TierTarget, *, full_sync: bool) -> int:
    """Merge ``rows`` into the destination on `id`, as ONE commit carrying the marker; answer its version."""

    def clauses(tier: lance.LanceDataset) -> lance.MergeInsertBuilder:
        builder = tier.merge_insert("id").when_matched_update_all().when_not_matched_insert_all()
        return builder.when_not_matched_by_source_delete() if full_sync else builder

    return _marked_merge(clauses, rows, target)


def _converge_stream(
    rows: Callable[[], pa.RecordBatchReader], target: TierTarget, counted: list[int], *, rows_in: int, upstream_uri: str, commit_bytes: int
) -> int:
    """Converge a stream into the destination holding one COMMIT UNIT at a time, ending on ONE marked commit.

    WHY NOT ONE MERGE. Measured on pylance 12.0.0 over blob-v2 rows of 1 MiB: one `merge_insert` over the whole stream
    peaked at 1.29, 3.43 and 6.05 GB VmHWM for 0.2, 0.8 and 1.6 GB of payload, and a `when_not_matched_by_source_delete`
    reads the whole target, payloads included, even from a source of ids alone (one such merge into a 0.4 GB tier peaked
    at 0.65 GB, into a 1.6 GB tier at 1.86 GB). A 16-row upsert into either tier peaked at 0.27 GB: an upsert is sized
    by its source. The driver runs in the Ray head, beside the GCS, the dashboard and Serve.

    THE SHAPE. Slices are gathered into COMMIT UNITS of ``commit_bytes`` (a slice larger than that is a unit alone), and
    every unit but the last lands as its own unmarked upsert (`when_matched_update_all` + `when_not_matched_insert_all`,
    no retraction, so no unit deletes what an earlier one wrote). The last unit is held back. Once the stream is read,
    the rows to retract are the tier's ids the run did not produce, found from the `id` column alone and deleted by key
    in chunks. The held-back unit then lands as the run's ONE marked commit, so the
    marker sits on the run's last commit and "the marker is found" still means "every write landed". An empty stream
    lands an empty upsert, which still commits a version (measured on 12.0.0).

    THE COMMIT UNIT IS NOT THE SCAN BATCH. An upsert joins its source against the whole target's `id`s and adds a
    version, so committing every slice made a re-run of N rows cost N / slice commits each sized by the growing tier:
    measured on pylance 12.0.0, a re-run of 32,000 1 KiB blob rows in 8-row slices took 454 s and 4,001 versions. A
    byte budget keeps the unit sized by memory whatever the row size, so a column of short clips commits a few times
    and a column of page images still holds only ``commit_bytes`` of rows.

    A key repeated across slices is refused with the message Lance gives a repeated key inside one merge, because a
    per-slice upsert would otherwise keep the last copy silently where one merge refuses ([[LH-243]]).

    Raises:
        EmptyFullSyncError: the stream produced no rows from an upstream that holds rows.
        ValueError: the stream repeated a key across slices.
    """
    so = target.storage_options
    stream = rows()
    counted.append(0)
    seen: set[object] = set()
    # `held` is a full unit; a further slice proves it is not the last, so it lands then. At most one unit plus the
    # slices gathering into the next are held at once.
    held: pa.Table | None = None
    gathering: list[pa.RecordBatch] = []
    gathered = 0
    for batch in stream:
        if not batch.num_rows:
            continue
        keys = batch.column("id").to_pylist()
        if repeated := next((key for key in keys if key in seen), None):
            raise ValueError(f"Ambiguous merge inserts are prohibited: multiple source rows match the same target row on (id = {repeated})")
        seen.update(keys)
        counted[-1] += batch.num_rows
        if held is not None:
            _upsert(target.uri, so).execute(held)
            held = None
        gathering.append(batch)
        gathered += batch.nbytes
        if gathered >= commit_bytes:
            held, gathering, gathered = pa.Table.from_batches(gathering), [], 0
    if gathering:
        held = pa.Table.from_batches(gathering)
    if rows_in and not counted[-1]:
        raise EmptyFullSyncError(
            f"the run produced no rows from {upstream_uri}, which holds {rows_in}; refusing a full-sync converge that would empty {target.uri}"
        )
    tier = lance.dataset(target.uri, storage_options=so)
    dead = [key for key in tier.to_table(columns=["id"]).column("id").to_pylist() if key not in seen]
    for where in _in_chunks("id", dead):
        tier.delete(where)
    last = held if held is not None else stream.schema.empty_table()
    return _marked_merge(lambda dataset: dataset.merge_insert("id").when_matched_update_all().when_not_matched_insert_all(), last, target)


def _upsert(uri: str, storage_options: dict[str, str]) -> lance.MergeInsertBuilder:
    return lance.dataset(uri, storage_options=storage_options).merge_insert("id").when_matched_update_all().when_not_matched_insert_all()


def _marked_merge(clauses: Callable[[lance.LanceDataset], lance.MergeInsertBuilder], source: pa.Table | lance.LanceDataset, target: TierTarget) -> int:
    """Commit one merge of ``source`` into the destination, carrying the marker; answer its version.

    A MARKED MERGE RE-PLANS ITSELF WHEN IT LOSES A RACE. `.execute()` re-runs a merge a concurrent commit preempted
    (`conflict_retries`); `execute_uncommitted` plus `LanceDataset.commit` does not, because the commit's own
    `max_retries` only rebases a transaction that CAN be rebased. A merge preempted by a concurrent merge, compaction
    or delete raises `CommitConflictError` with ``retryable=True`` (measured on pylance 12.0.0), which
    `lance_docs/file_format.md` § Conflict Resolution defines as "re-execute at the application level with updated
    data". An incompatible conflict (``retryable=False``: a concurrent overwrite replaced the table) is raised
    unchanged, because re-planning would merge into contents this run never read.
    """
    so = target.storage_options
    for attempt in range(MERGE_CONFLICT_RETRIES + 1):
        builder = clauses(lance.dataset(target.uri, storage_options=so))
        if target.marker is None:
            builder.execute(source)
            return int(lance.dataset(target.uri, storage_options=so).version)
        transaction, _stats = builder.execute_uncommitted(source)
        try:
            committed = lance.LanceDataset.commit(target.uri, stamped(transaction, target.marker), storage_options=so)
        except CommitConflictError as exc:
            if not exc.retryable or attempt == MERGE_CONFLICT_RETRIES:
                raise
            log.info("tier_write_merge_replanned", extra={"uri": target.uri, "attempt": attempt + 1, "error": str(exc)[:300]})
            continue
        return int(committed.version)
    raise AssertionError("unreachable: the last attempt returns or raises")


def _widen_by_id(rows: RowSource, target: TierTarget, session: lance.Session | None) -> None:
    """Add the columns this run produces and the tier lacks, joined on `id`, through the handle that chose them.

    `merge_insert` refuses a source with a column the target lacks ("Append with different schema", pylance 12.0.0).
    ``LanceDataset.merge`` joins on the key, so no value depends on where its row sits, and it commits a Merge whose
    read version is the one this handle read the schema at: Lance refuses it when another commit has passed (measured
    on pylance 12.0.0: "This Merge transaction was preempted by concurrent transaction Update"), so the decision and
    the write never describe two different tiers. A refused attempt re-reads the tier and decides again. The values
    are written by the converge that follows; this step makes its source and target agree on the schema, and every
    version that holds a new column holds its real values.

    A stream is produced once more here, for its `id` and new columns only, and only when a new column exists.
    """
    names = _schema_of(rows).names
    for attempt in range(1, WIDEN_ATTEMPTS + 1):
        tier = lance.dataset(target.uri, storage_options=target.storage_options, session=session)
        new = [name for name in names if name not in tier.schema.names]
        if not new:
            return
        try:
            tier.merge(_columns(rows, ["id", *new]), left_on="id")
        except OSError:
            # pylance raises a conflict here as a bare OSError, so the tier's own version decides whether this was
            # one: a write nobody preempted failed for a reason a retry cannot fix.
            if attempt == WIDEN_ATTEMPTS or tier.latest_version == tier.version:
                raise
            log.info("tier_write_widen_preempted", extra={"uri": target.uri, "attempt": attempt, "read_version": tier.version})
            continue
        log.info("tier_write_added_columns", extra={"uri": target.uri, "columns": new})
        return


def _schema_of(rows: RowSource) -> pa.Schema:
    if isinstance(rows, (pa.Table, lance.LanceDataset)):
        return rows.schema
    return rows().schema


def _columns(rows: RowSource, columns: list[str]) -> pa.Table:
    if isinstance(rows, pa.Table):
        return rows.select(columns)
    if isinstance(rows, lance.LanceDataset):
        return rows.to_table(columns=columns)
    return pa.Table.from_batches([batch.select(columns) for batch in rows()])


def _carry_governance_labels(upstream: pa.Schema, uri: str, storage_options: dict[str, str], session: lance.Session | None) -> None:
    """Copy the upstream's `rask.*` field labels onto the same-named tier fields that lack them.

    A label set on bronze AFTER silver exists is the usual order (ingest, cascade, then classify), and a converge keeps
    the tier's schema (the full-sync `merge_insert` and the by-id widening `merge`, measured on pylance 12.0.0), so
    without this the label reaches no tier above and silver stays vendable raw while bronze is restricted ([[LH-217]]).

    ADD-ONLY. A key the tier already holds is never replaced or removed: the estate defines no order between
    classification values (the vocabulary is delegated, [[LH-055]]) and vending refuses on a label's presence, so the
    only change known to be tighter is absent -> present. No `can_classify` is asked for: this copies a label a
    classifier already put on the upstream, which can only make the tier less vendable. Top-level fields only, which is
    where the classify door's labels on the cascade's tiers live.
    """
    tier = lance.dataset(uri, storage_options=storage_options, session=session)
    updates: dict[str, dict[str, str | None]] = {}
    for field in upstream:
        if field.name not in tier.schema.names:
            continue
        held = tier.schema.field(field.name).metadata or {}
        missing: dict[str, str | None] = {
            key.decode(): value.decode()
            for key, value in (field.metadata or {}).items()
            if key.decode().startswith(GOVERNANCE_FIELD_PREFIX) and key not in held
        }
        if missing:
            updates[field.name] = missing
    if updates:
        tier.update_field_metadata(updates)
        log.info("tier_write_carried_governance_labels", extra={"uri": uri, "fields": sorted(updates)})


def index_lineage(uri: str, storage_options: dict[str, str], *, session: lance.Session | None = None) -> None:
    """Build the JSON scalar index over ``lineage -> run_id``: one JSONB path, a btree underneath."""
    lance.dataset(uri, storage_options=storage_options, session=session).create_scalar_index(
        LINEAGE_COLUMN,
        IndexConfig(index_type="json", parameters={"target_index_type": "btree", "path": LINEAGE_INDEX_PATH}),
        name=LINEAGE_INDEX_NAME,
    )


def assert_stage_contract(*, rows_in: int, rows_out: int, cardinality: str, parentless: int) -> None:
    """What a stage owes its tier: every row names a parent, and a 1:1 lane keeps its count.

    Provenance is asserted for every cardinality and for delta and full runs alike: a transform that swapped two rows
    for two unrelated ones keeps a count and fails this. The count is asserted only where a lane DECLARED it holds; a
    `1:N` lane (a video into frames, a recording into speaker turns) legitimately grows.

    An unknown cardinality is refused rather than defaulted, so a typo in a declared lane cannot buy the loosest
    contract by falling through.

    Raises:
        StageContractError: an unknown cardinality, a parentless row, or a 1:1 count that moved.
    """
    if cardinality not in CARDINALITIES:
        raise StageContractError(f"unknown stage cardinality {cardinality!r}; declare one of {sorted(CARDINALITIES)}")
    if parentless:
        raise StageContractError(f"stage transform produced {parentless} row(s) with no parent: {SOURCE_ROWID_COLUMN} is null")
    if cardinality == ONE_TO_ONE and rows_out != rows_in:
        raise StageContractError(f"stage transform produced wrong row count: {rows_out} out for {rows_in} in, on a {cardinality} lane")
