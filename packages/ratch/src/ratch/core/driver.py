"""Ray Data drivers — distribute a stage's compute, keep every commit driver-side.

Three drivers, one per :class:`~ratch.core.registry.StageShape`, all built on
the same topology (LANCE_MEDIA_MERGE §4.3):

* **actors compute, the driver commits** — ``lance_ray.read_lance`` fans the
  pending rows out to a ``map_batches`` actor pool (each actor holds a warm
  client via the injected zero-arg ``factory``), and the driver serially
  applies ``merge_insert`` / ``add_columns`` as result batches stream back.
  Only plain appends may involve parallel workers (Appends never conflict).
* **heavy blobs never transit Ray Data blocks** — blob stages ship only
  ``_rowid``s; each actor opens the dataset itself and reads payloads lazily
  via ``take_blobs``.
* **resume is a property of the read**, not bookkeeping: scan stages read
  ``WHERE <column> IS NULL``, blob stages skip checkpointed row ids, append
  stages diff existing output keys — so re-running a killed stage always
  converges with no duplicates.

The module imports no modality or client code; compute factories arrive from
the composition root already bound to their clients.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import lance
import lance_ray
import pyarrow as pa

from ratch.core.blobs import schema_has_blob
from ratch.core.engine import _ValueCheckpoint, attach_values_by_rowid


if TYPE_CHECKING:
    from collections.abc import Callable

    import ray.data

    from ratch.core.registry import Stage

logger = logging.getLogger(__name__)


#: Written beside a stage's output so a later run can ask which TRANSFORM produced a row, not merely
#: whether one did. Nullable and additive: a dataset written before B4 simply has no such column, and
#: :func:`resume_filter` degrades to the original predicate rather than naming a column that is absent.
TRANSFORM_VERSION_COLUMN = "transform_version"


def resume_filter(output_column: str, *, identity: str = "", has_version_column: bool = False) -> str:
    """The rows a stage still owes work on.

    `<output> IS NULL` asks "was this row ever computed". With a transform identity available AND a
    `transform_version` column to compare against, it can ask the question B4 exists for — "was this
    row computed by THIS transform" — so an edited transform reclaims its own rows without a rebuild.

    The NULL case is spelled out rather than folded into an operator. A NULL version — every row
    written before this column existed — must count as STALE, and `<> 'x'` evaluates to NULL on a NULL
    left side, which drops the row: precisely the rows a first identity-aware run needs to claim. SQL's
    one-word form for that is `IS DISTINCT FROM`, and **Lance does not support it** — its planner
    answers "Expression ... is not supported SQL in lance" (lance-datafusion planner.rs). Verified by
    executing it, not by reading: `tests/test_resume_filter_against_lance.py` runs this predicate
    through a real dataset, which is what caught it.

    Both conditions must hold before the predicate widens. Missing either, this is the original filter:
    a dataset on disk from before B4 has no column to name, and naming it is a scan error, not a resume.
    """
    if not identity or not has_version_column:
        return f"{output_column} IS NULL"
    return f"({output_column} IS NULL OR {TRANSFORM_VERSION_COLUMN} IS NULL OR {TRANSFORM_VERSION_COLUMN} != '{identity}')"


def _table_uri(db_path: str | Path, table: str) -> str:
    # Resolved absolute path: a relative URI would be re-rooted inside Ray's
    # runtime-env working-dir COPY on the workers (a stale snapshot), silently
    # missing data files written after job submission.
    return str((Path(db_path) / f"{table}.lance").resolve())


class _ScanActor:
    """Generic scan-stage actor: warm compute over scanned columns."""

    def __init__(self, factory: Callable[[], Callable[[pa.Table], pa.Array]], out_name: str) -> None:
        self._fn = factory()
        self._out = out_name

    def __call__(self, batch: pa.Table) -> pa.Table:
        return batch.append_column(self._out, self._fn(batch))


def _empty_rowid_table(out_name: str, output_type: pa.DataType) -> pa.Table:
    return pa.table(
        {"_rowid": pa.array([], pa.uint64()), out_name: pa.array([], output_type)},
        schema=pa.schema([("_rowid", pa.uint64()), (out_name, output_type)]),
    )


class _BlobActor:
    """Generic blob-stage actor: reads its own payloads lazily by ``_rowid``.

    Ray Data blocks carry only row ids — the actor opens the dataset once and
    streams bytes via ``take_blobs`` (never materialising blobs into the
    object store), skipping ids the driver already has checkpointed.
    """

    def __init__(
        self,
        factory: Callable[[], Callable[[list[bytes]], pa.Array]],
        dataset_uri: str,
        blob_column: str,
        out_name: str,
        output_type: pa.DataType,
        done_ids: frozenset[int],
    ) -> None:
        self._fn = factory()
        self._ds = lance.dataset(dataset_uri)
        self._blob_column = blob_column
        self._out = out_name
        self._type = output_type
        self._done = done_ids

    def __call__(self, batch: pa.Table) -> pa.Table:
        row_ids = [r for r in batch.column("_rowid").to_pylist() if r not in self._done]
        if not row_ids:
            return _empty_rowid_table(self._out, self._type)
        payloads: list[bytes] = []
        for blob in self._ds.take_blobs(self._blob_column, ids=row_ids):
            if blob is None:
                raise ValueError(
                    f"{self._blob_column}: a NULL payload cannot be keyed by position by this stage. "
                    f"Filter the scan to `{self._blob_column} IS NOT NULL`, or read via a "
                    "blob_handling='all_binary' scan (service_kit.lakehouse.blobs.read_aligned_table)."
                )
            with blob as handle:
                payloads.append(handle.read())
        # A NULL payload arrives as `None` in its slot from pylance 10.0.0, where 8/9 omitted the row
        # entirely. Both are hazards, but different ones, and the fix differs:
        #   * 8/9 — the list came back SHORT, so pairing by position misattributed every later value.
        #     The length check below caught that.
        #   * 10 — the list is the right LENGTH but `with blob as handle` dereferences `None` and
        #     raises AttributeError before the check is ever reached.
        # Refusing on the null is the same verdict either way: this stage keys payloads BY POSITION and
        # cannot represent an absent one, so it names the cause rather than letting `pa.table` raise an
        # opaque length error — or worse, letting a broadcasting `self._fn` return the right length and
        # commit silent misattribution. The length check stays as the 8/9 belt; on 10 the null test
        # fires first.
        if len(payloads) != len(row_ids):
            raise ValueError(
                f"{self._blob_column}: take_blobs returned {len(payloads)} payload(s) for {len(row_ids)} "
                "row id(s) — the column has NULL rows, which this stage cannot key by position. Filter the "
                f"scan to `{self._blob_column} IS NOT NULL`, or read via a blob_handling='all_binary' scan."
            )
        values = self._fn(payloads)
        return pa.table({"_rowid": pa.array(row_ids, pa.uint64()), self._out: values})


class _ScanByRowidActor:
    """Scan-stage actor for BLOB-BEARING tables: results keyed by ``_rowid``.

    ``merge_insert`` crashes Lance's blob decoder on blob-bearing tables
    (invariant §7.1), so scan stages there return ``(_rowid, value)`` pairs for
    a driver-side ``add_columns`` attach instead.
    """

    def __init__(
        self,
        factory: Callable[[], Callable[[pa.Table], pa.Array]],
        out_name: str,
        output_type: pa.DataType,
        done_ids: frozenset[int],
    ) -> None:
        self._fn = factory()
        self._out = out_name
        self._type = output_type
        self._done = done_ids

    def __call__(self, batch: pa.Table) -> pa.Table:
        mask = pa.array([r not in self._done for r in batch.column("_rowid").to_pylist()], pa.bool_())
        pending = batch.filter(mask)
        if pending.num_rows == 0:
            return _empty_rowid_table(self._out, self._type)
        values = self._fn(pending)
        return pa.table({"_rowid": pending.column("_rowid"), self._out: values})


class _RowsActor:
    """Generic append-stage actor: source rows in, output-table rows out."""

    def __init__(self, factory: Callable[[], Callable[[pa.Table], pa.Table]]) -> None:
        self._fn = factory()

    def __call__(self, batch: pa.Table) -> pa.Table:
        return self._fn(batch)


def _map_batches(source: ray.data.Dataset, actor_cls: type, stage: Stage, **ctor: Any) -> ray.data.Dataset:
    from ratch.core.runners import runner_ray_remote_args

    return source.map_batches(
        actor_cls,
        batch_format="pyarrow",
        batch_size=stage.actor.batch_rows,
        concurrency=(stage.actor.min_actors, stage.actor.max_actors),
        num_cpus=stage.actor.num_cpus,
        num_gpus=stage.actor.num_gpus or None,
        fn_constructor_kwargs=ctor,
        # Runner-backed stages get the runner's env on the workers (cluster mode;
        # no-op locally) — the driver never carries a model dep.
        **runner_ray_remote_args(stage.runner),
    )


def _gate_filter(db_path: str | Path, stage: Stage) -> str | None:
    """SQL filter dropping docs the stage's MIME gate rejects (skips counted by caller).

    Doc-level MIME lives on the documents table; per LANCE_MEDIA_MERGE §4.3
    gated stages *skip* non-matching docs rather than crash. Returns a
    ``doc_id IN (...)`` filter, or ``None`` when the stage is ungated.
    """
    if stage.media_gate is None:
        return None
    docs = lance.dataset(_table_uri(db_path, "documents")).to_table(columns=["doc_id", "media_mime"])
    admitted = [doc_id for doc_id, mime in zip(docs["doc_id"].to_pylist(), docs["media_mime"].to_pylist(), strict=True) if stage.media_gate.admits(mime)]
    skipped = docs.num_rows - len(admitted)
    if skipped:
        logger.info("stage %s: media gate skipped %s/%s document(s)", stage.name, skipped, docs.num_rows)
    quoted = ", ".join(f"'{d}'" for d in admitted)
    return f"doc_id IN ({quoted})" if admitted else "doc_id IN ('')"


def run_scan_column_stage(
    db_path: str | Path,
    stage: Stage,
    *,
    factory: Callable[[], Callable[[pa.Table], pa.Array]],
    output_type: pa.DataType,
    checkpoint_file: str | Path | None = None,
) -> int:
    """Backfill ``stage.output_columns[0]``. Returns rows written.

    Two write shapes, chosen by whether the table carries blob-v2 columns:

    * **non-blob table** — the column is null-added (metadata-only) when
      absent, then one idempotent fill: read ``WHERE col IS NULL`` via
      lance-ray, compute on the actor pool, ``merge_insert`` per result batch
      (serialized here — Merge conflicts with nearly everything).
    * **blob-bearing table** — ``merge_insert`` crashes Lance's blob decoder
      (invariant §7.1), so the column is built all-or-nothing via ``_rowid``
      pairs + a driver-side ``add_columns`` attach; an all-NULL leftover column
      (e.g. from an aborted earlier run) is dropped and rebuilt.
    """
    [name] = stage.output_columns
    uri = _table_uri(db_path, stage.table)
    ds = lance.dataset(uri)
    blob_table = schema_has_blob(ds.schema)

    if name in ds.schema.names and blob_table:
        total = ds.count_rows()
        if ds.count_rows(filter=f"{name} IS NULL") == total:
            logger.info("stage %s: dropping all-NULL %s for a clean attach", stage.name, name)
            ds.drop_columns([name])
            ds = lance.dataset(uri)
        else:
            # Stays `IS NULL` rather than the identity-aware predicate: a blob table's only legal
            # write is the all-or-nothing `_rowid` rebuild below (merge_insert crashes the blob
            # decoder, §7.1), so there is no partial update for a widened predicate to drive. A
            # transform change on a blob column is a REBUILD, which is what this branch already says.
            pending = ds.count_rows(filter=f"{name} IS NULL")
            if pending:
                raise RuntimeError(
                    f"stage {stage.name}: {pending} NULL row(s) on blob table {stage.table} — "
                    "NULL-fill needs merge_insert, which crashes the blob decoder (§7.1); "
                    "rebuild the column instead"
                )
            logger.info("stage %s: nothing to fill", stage.name)
            return 0

    if name not in ds.schema.names:
        if blob_table:
            return _build_scan_column_by_rowid(uri, stage, factory=factory, output_type=output_type, checkpoint_file=checkpoint_file)
        ds.add_columns(pa.field(name, output_type, nullable=True))
        ds = lance.dataset(uri)

    # B4: the transform's identity, stamped on every row this run writes, so a LATER run can tell a row
    # computed by this transform from one computed by an older version of it. The factory's qualname
    # stands in for the bound implementation — the declaration cannot see which class the composition
    # root injected, and a rewritten actor under an unchanged declaration is still a new transform.
    identity = stage.identity(actor_qualname=getattr(factory, "__qualname__", ""))
    if TRANSFORM_VERSION_COLUMN not in ds.schema.names:
        ds.add_columns(pa.field(TRANSFORM_VERSION_COLUMN, pa.string(), nullable=True))
        ds = lance.dataset(uri)
    has_version = TRANSFORM_VERSION_COLUMN in ds.schema.names

    predicate = resume_filter(name, identity=identity, has_version_column=has_version)
    pending = ds.count_rows(filter=predicate)
    if pending == 0:
        logger.info("stage %s: nothing to fill", stage.name)
        return 0

    filters = [predicate]
    gate = _gate_filter(db_path, stage)
    if gate is not None:
        filters.append(gate)
    source = lance_ray.read_lance(
        uri,
        columns=[*stage.key_columns, *stage.read_columns],
        filter=" AND ".join(filters),
    )
    results = _map_batches(source, _ScanActor, stage, factory=factory, out_name=name)

    written = 0
    key_columns = list(stage.key_columns)
    for batch in (cast("pa.Table", b) for b in results.iter_batches(batch_format="pyarrow")):
        if batch.num_rows == 0:
            continue
        update = batch.select([*key_columns, name])
        stamp = pa.array([identity] * update.num_rows, pa.string())
        update = update.append_column(pa.field(TRANSFORM_VERSION_COLUMN, pa.string()), stamp)
        ds.merge_insert(key_columns).when_matched_update_all().execute(update)
        written += batch.num_rows
        logger.info("stage %s: %s/%s row(s) committed", stage.name, written, pending)
    return written


def _build_scan_column_by_rowid(
    uri: str,
    stage: Stage,
    *,
    factory: Callable[[], Callable[[pa.Table], pa.Array]],
    output_type: pa.DataType,
    checkpoint_file: str | Path | None,
) -> int:
    """All-or-nothing scan-column build for blob-bearing tables (attach by ``_rowid``)."""
    [name] = stage.output_columns
    ckpt = _ValueCheckpoint(checkpoint_file)
    value_by_row_id: dict[int, Any] = ckpt.load()

    source = lance_ray.read_lance(uri, columns=list(stage.read_columns), scanner_options={"with_row_id": True})
    results = _map_batches(
        source,
        _ScanByRowidActor,
        stage,
        factory=factory,
        out_name=name,
        output_type=output_type,
        done_ids=frozenset(value_by_row_id),
    )
    for batch in (cast("pa.Table", b) for b in results.iter_batches(batch_format="pyarrow")):
        pairs = list(zip(batch.column("_rowid").to_pylist(), batch.column(name).to_pylist(), strict=True))
        ckpt.extend(pairs)
        value_by_row_id.update(pairs)
        logger.info("stage %s: %s value(s) computed", stage.name, len(value_by_row_id))

    attach_values_by_rowid(
        uri,
        name=name,
        output_type=output_type,
        value_by_row_id=value_by_row_id,
        batch_rows=stage.actor.batch_rows,
        checkpoint_file=checkpoint_file,
    )
    ckpt.cleanup()
    return len(value_by_row_id)


def run_blob_column_stage(
    db_path: str | Path,
    stage: Stage,
    *,
    factory: Callable[[], Callable[[list[bytes]], pa.Array]],
    output_type: pa.DataType,
    checkpoint_file: str | Path | None = None,
    overwrite: bool = False,
) -> int:
    """Derive a column from blob payloads, distributed. Returns rows written.

    Two passes keyed by ``_rowid`` (the engine's blob shape, compute pass
    distributed): actors read their own payloads and stream ``(_rowid, value)``
    pairs back; the driver persists each result batch to the JSONL sidecar
    (kill-resumable), then attaches the full map via the engine's
    ``add_columns`` UDF. All-or-nothing per column, like the engine.
    """
    [name] = stage.output_columns
    if stage.blob_column is None:  # narrowed by the registry validator; keeps ty happy
        raise ValueError(f"stage {stage.name}: blob stage without blob_column")
    uri = _table_uri(db_path, stage.table)
    ds = lance.dataset(uri)
    if name in ds.schema.names:
        if not overwrite:
            logger.info("stage %s: %s already exists — nothing to do", stage.name, name)
            return 0
        ds.drop_columns([name])
        ds = lance.dataset(uri)

    total = ds.count_rows()
    if total == 0:
        return 0

    ckpt = _ValueCheckpoint(checkpoint_file)
    value_by_row_id: dict[int, Any] = ckpt.load()

    source = lance_ray.read_lance(uri, columns=[], scanner_options={"with_row_id": True})
    results = _map_batches(
        source,
        _BlobActor,
        stage,
        factory=factory,
        dataset_uri=uri,
        blob_column=stage.blob_column,
        out_name=name,
        output_type=output_type,
        done_ids=frozenset(value_by_row_id),
    )
    for batch in (cast("pa.Table", b) for b in results.iter_batches(batch_format="pyarrow")):
        pairs = list(zip(batch.column("_rowid").to_pylist(), batch.column(name).to_pylist(), strict=True))
        ckpt.extend(pairs)  # persist before progress — a kill costs at most the in-flight batch
        value_by_row_id.update(pairs)
        logger.info("stage %s: %s/%s value(s) computed", stage.name, len(value_by_row_id), total)

    attach_values_by_rowid(
        uri,
        name=name,
        output_type=output_type,
        value_by_row_id=value_by_row_id,
        batch_rows=stage.actor.batch_rows,
        checkpoint_file=checkpoint_file,
    )
    ckpt.cleanup()
    return total


def run_append_rows_stage(
    db_path: str | Path,
    stage: Stage,
    *,
    factory: Callable[[], Callable[[pa.Table], pa.Table]],
    output_schema: pa.Schema,
    create_output: Callable[[], object],
) -> int:
    """Append derived rows to ``stage.output_table``. Returns rows appended.

    Resume = key diff: source rows whose ``stage.key_columns`` already exist in
    the output table are dropped before fan-out. Result batches are appended by
    the driver (plain Appends are the one concurrency-safe write, but a single
    appender keeps ordering deterministic at this scale). ``create_output``
    creates the output table through :mod:`ratch.core.dataset` when missing.
    """
    from ratch.core.dataset import append_rows  # local import: dataset ← driver, never the reverse

    uri = _table_uri(db_path, stage.table)
    out_uri = _table_uri(db_path, stage.output_table or "")

    filters = []
    gate = _gate_filter(db_path, stage)
    if gate is not None:
        filters.append(gate)

    key_columns = list(stage.key_columns)
    done: set[tuple[Any, ...]] = set()
    if Path(out_uri).exists():
        existing = lance.dataset(out_uri).to_table(columns=key_columns)
        done = set(zip(*(existing[k].to_pylist() for k in key_columns), strict=True)) if existing.num_rows else set()
    else:
        create_output()

    source = lance_ray.read_lance(
        uri,
        columns=list(dict.fromkeys([*stage.key_columns, *stage.read_columns])),
        filter=" AND ".join(filters) if filters else None,
    )
    if done:
        done_broadcast = done  # captured by the lambda; small (key tuples only)

        def _drop_done(batch: pa.Table) -> pa.Table:
            keys = zip(*(batch[k].to_pylist() for k in key_columns), strict=True)
            mask = pa.array([k not in done_broadcast for k in keys], pa.bool_())
            return batch.filter(mask)

        source = source.map_batches(_drop_done, batch_format="pyarrow")

    results = _map_batches(source, _RowsActor, stage, factory=factory)

    appended = 0
    for batch in (cast("pa.Table", b) for b in results.iter_batches(batch_format="pyarrow")):
        if batch.num_rows == 0:
            continue
        append_rows(out_uri, batch.cast(output_schema))
        appended += batch.num_rows
        logger.info("stage %s: %s row(s) appended", stage.name, appended)
    return appended
