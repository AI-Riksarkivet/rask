"""Attach derived columns to a Lance dataset via *data evolution* (``add_columns``).

Type-agnostic core: the output column can be any Arrow type (a vector, a string,
a struct), so the same two functions back text embeddings, frame embeddings,
captions, and summaries. Lance writes one new column file alongside the existing
fragments and links it with a metadata-only op — far cheaper than ``merge_insert``,
which rewrites every matched fragment.

Two shapes, by where the input lives:

* :func:`upsert_scan_column` — single pass; the UDF derives each batch's values
  from that batch's own *scanned* columns (e.g. ``text``). Supports incremental
  null-fill so rows added by a later ingest get backfilled.
* :func:`upsert_blob_column` — two passes keyed by ``_rowid``; the input lives in
  a Blob V2 column a scan can't materialise (e.g. frame JPEGs), so we compute by
  id first, then attach by id — order-independent and race-free.

Pure functions over a dataset path + a ``compute`` callable; they import no model
client, so a Ray/Ray Data driver can fan them out later.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import lance
import pyarrow as pa


if TYPE_CHECKING:
    from collections.abc import Callable

    import lancedb

logger = logging.getLogger(__name__)


class _ValueCheckpoint:
    """Append-only ``{row_id: value}`` sidecar so a killed blob-compute can resume.

    The blob-derived columns (captions especially) can take hours: each value is
    one Gemma call. The compute used to live only in memory and persist via a
    single ``add_columns`` at the very end, so a mid-run kill lost *everything*.
    This writes each computed value as one JSONL line as it's produced, so a
    re-run skips what's already done and a crash costs at most the in-flight
    batch. A no-op when no checkpoint path is given; the file is removed on success.
    """

    def __init__(self, base: str | Path | None) -> None:
        self.path = Path(f"{base}.values.jsonl") if base is not None else None

    def load(self) -> dict[int, Any]:
        if self.path is None or not self.path.exists():
            return {}
        out: dict[int, Any] = {}
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    out[int(rec["id"])] = rec["v"]
        logger.info("resuming blob-compute from %s: %s value(s) already computed", self.path, len(out))
        return out

    def extend(self, pairs: list[tuple[int, Any]]) -> None:
        if self.path is None or not pairs:
            return
        with self.path.open("a", encoding="utf-8") as f:
            for row_id, value in pairs:
                f.write(json.dumps({"id": int(row_id), "v": value}, ensure_ascii=False) + "\n")
            f.flush()

    def cleanup(self) -> None:
        if self.path is not None:
            self.path.unlink(missing_ok=True)


def upsert_scan_column(
    dataset_path: str | Path,
    *,
    name: str,
    output_type: pa.DataType,
    key_columns: list[str],
    read_columns: list[str],
    compute: Callable[[pa.RecordBatch], pa.Array],
    batch_rows: int = 256,
    checkpoint_file: str | Path | None = None,
    overwrite: bool = False,
    progress: Callable[[int], None] | None = None,
) -> int:
    """Attach/refresh a scan-derived column. Returns the number of rows written.

    * Column absent → add it for every row with a single-pass checkpointed UDF.
    * Column present, ``overwrite`` → drop and rebuild.
    * Column present, not ``overwrite`` → fill only residual ``NULL`` rows (rows
      a later ingest appended) via ``merge_insert`` on ``key_columns``.

    ``compute`` receives a batch carrying ``read_columns`` (the fill path also
    includes ``key_columns``, which ``compute`` ignores) and returns the output
    array for that batch.
    """
    ds = lance.dataset(str(dataset_path))
    has_col = name in ds.schema.names

    if has_col and overwrite:
        ds.drop_columns([name])
        ds = lance.dataset(str(dataset_path))
        has_col = False

    if has_col:
        return _fill_null_scan_column(
            ds,
            name=name,
            key_columns=key_columns,
            read_columns=read_columns,
            compute=compute,
            batch_rows=batch_rows,
            progress=progress,
        )

    total = ds.count_rows()
    if total == 0:
        return 0

    schema = pa.schema([pa.field(name, output_type, nullable=True)])

    @lance.batch_udf(output_schema=schema, checkpoint_file=_as_str(checkpoint_file))
    def udf(batch: pa.RecordBatch) -> pa.RecordBatch:
        out = compute(batch)
        if progress is not None:
            progress(batch.num_rows)
        return pa.RecordBatch.from_arrays([out], names=[name])

    logger.info("adding column %s (%s) to %s row(s) via add_columns", name, output_type, total)
    ds.add_columns(udf, read_columns=read_columns, batch_size=batch_rows)
    return total


def _fill_null_scan_column(
    ds: lance.LanceDataset,
    *,
    name: str,
    key_columns: list[str],
    read_columns: list[str],
    compute: Callable[[pa.RecordBatch], pa.Array],
    batch_rows: int,
    progress: Callable[[int], None] | None,
) -> int:
    """Fill only ``NULL`` rows of an existing column via ``merge_insert`` on the key."""
    pending = ds.to_table(columns=[*key_columns, *read_columns], filter=f"{name} IS NULL")
    if pending.num_rows == 0:
        logger.info("%s already populated — nothing to fill", name)
        return 0

    logger.info("filling %s NULL %s row(s) via merge_insert", pending.num_rows, name)
    for batch in pending.to_batches(max_chunksize=batch_rows):
        update = pa.table({**{k: batch.column(k) for k in key_columns}, name: compute(batch)})
        ds.merge_insert(key_columns).when_matched_update_all().execute(update)
        if progress is not None:
            progress(batch.num_rows)
    return pending.num_rows


def upsert_blob_column(
    dataset_path: str | Path,
    *,
    name: str,
    output_type: pa.DataType,
    blob_column: str,
    compute: Callable[[list[bytes]], pa.Array],
    batch_rows: int = 256,
    checkpoint_file: str | Path | None = None,
    overwrite: bool = False,
    progress: Callable[[int], None] | None = None,
) -> int:
    """Attach a column derived from a Blob V2 column. Returns the number of rows written.

    Two passes keyed by ``_rowid`` (stable within a dataset version): compute a
    ``{row_id: value}`` map from the blob payloads, then an ``add_columns`` UDF
    places each value by id. A row id missing from the map raises (loud failure)
    rather than misaligning, so the result is correct regardless of scan order.
    No null-fill: the column is all-or-nothing (``overwrite`` rebuilds).

    When ``checkpoint_file`` is given, the (potentially hours-long) compute pass is
    **resumable**: each value is written to a ``{checkpoint_file}.values.jsonl``
    sidecar as it's produced, so a killed run resumes instead of recomputing. The
    sidecar is removed once the column is attached.
    """
    ds = lance.dataset(str(dataset_path))
    if name in ds.schema.names:
        if not overwrite:
            logger.info("%s already exists — nothing to do (pass overwrite=True)", name)
            return 0
        ds.drop_columns([name])
        ds = lance.dataset(str(dataset_path))

    total = ds.count_rows()
    if total == 0:
        return 0

    # Resume from any prior partial run; compute only the rows not already done.
    ckpt = _ValueCheckpoint(checkpoint_file)
    value_by_row_id: dict[int, Any] = ckpt.load()
    for batch in ds.to_batches(columns=[], with_row_id=True, batch_size=batch_rows):
        row_ids = batch.column("_rowid").to_pylist()
        todo = [r for r in row_ids if r not in value_by_row_id]
        if todo:
            values = compute(_read_blobs(ds, blob_column, todo)).to_pylist()
            new_pairs = list(zip(todo, values, strict=True))
            ckpt.extend(new_pairs)  # persist before counting progress — survives a kill
            value_by_row_id.update(new_pairs)
        if progress is not None:
            progress(len(row_ids))

    attach_values_by_rowid(
        dataset_path,
        name=name,
        output_type=output_type,
        value_by_row_id=value_by_row_id,
        batch_rows=batch_rows,
        checkpoint_file=checkpoint_file,
    )
    ckpt.cleanup()  # column is durably attached — the sidecar is no longer needed
    return total


def attach_values_by_rowid(
    dataset_path: str | Path,
    *,
    name: str,
    output_type: pa.DataType,
    value_by_row_id: dict[int, Any],
    batch_rows: int = 256,
    checkpoint_file: str | Path | None = None,
) -> None:
    """Attach a fully-computed ``{_rowid: value}`` map as a new column.

    The second pass of the blob shape, shared with the Ray driver
    (:mod:`ratch.core.driver`), which distributes the compute pass and hands
    the collected map here. A row id missing from the map raises (loud failure)
    rather than misaligning.
    """
    ds = lance.dataset(str(dataset_path))
    schema = pa.schema([pa.field(name, output_type, nullable=True)])

    @lance.batch_udf(output_schema=schema, checkpoint_file=_as_str(checkpoint_file))
    def attach(batch: pa.RecordBatch) -> pa.RecordBatch:
        row_ids = batch.column("_rowid").to_pylist()
        out = pa.array([value_by_row_id[r] for r in row_ids], type=output_type)
        return pa.RecordBatch.from_arrays([out], names=[name])

    logger.info("attaching column %s (%s) for %s row(s)", name, output_type, len(value_by_row_id))
    ds.add_columns(attach, read_columns=["_rowid"], batch_size=batch_rows)


def _read_blobs(ds: lance.LanceDataset, column: str, row_ids: list[int]) -> list[bytes]:
    """Read each row's blob payload by id (Blob V2 columns aren't materialised in a scan).

    ``take_blobs`` DROPS null rows (measured — docs/architecture/lance-blob-v2-findings.md), so a null
    payload makes the result SHORT and every caller pairs it with ``row_ids`` by position. The callers'
    ``zip(..., strict=True)`` does catch that, but with an opaque "zip() argument is shorter" — so the
    mismatch is named here instead, at the layer that knows why.
    """
    payloads: list[bytes] = []
    for blob in ds.take_blobs(column, ids=row_ids):
        with blob as handle:
            payloads.append(handle.read())
    if len(payloads) != len(row_ids):
        raise ValueError(
            f"{column}: take_blobs returned {len(payloads)} payload(s) for {len(row_ids)} row id(s) — the "
            "column has NULL rows and this derived column is all-or-nothing. Restrict the source rows, or "
            f"read via ds.scanner(columns=['{column}'], blob_handling='all_binary') which keeps the nulls."
        )
    return payloads


def ensure_vector_index(
    table: lancedb.table.Table,
    column: str,
    *,
    num_partitions: int,
    num_sub_vectors: int,
    metric: Literal["l2", "cosine", "dot"] = "cosine",
) -> bool:
    """Build an IVF_PQ index on ``column`` once it is fully populated.

    Returns ``True`` if an index was (re)built, ``False`` if skipped. Two
    preconditions are checked, both of which otherwise crash Lance's trainer:

    * every row must have a vector — the IVF trainer rejects partial-``NULL``
      columns;
    * the table must have at least ``num_partitions`` rows — IVF k-means needs at
      least one training vector per partition. Below that, flat (brute-force)
      search is used until the table grows, which is fine at small scale.

    Rebuilding after compaction is the caller's responsibility (compaction
    invalidates the row addresses the index points at).
    """
    nulls = table.count_rows(filter=f"{column} IS NULL")
    if nulls > 0:
        logger.warning("skipping index on %s: %s row(s) still NULL", column, nulls)
        return False
    row_count = table.count_rows()
    if row_count < num_partitions:
        logger.warning(
            f"skipping index on {column}: {row_count} row(s) < num_partitions={num_partitions} "
            f"(IVF_PQ needs ≥ num_partitions rows to train; flat search is used until then)"
        )
        return False
    logger.info(f"building IVF_PQ {metric} index on {column} (num_partitions={num_partitions}, num_sub_vectors={num_sub_vectors})")
    from lancedb.index import IvfPq

    table.create_index(
        column,
        replace=True,
        config=IvfPq(
            distance_type=metric,
            num_partitions=num_partitions,
            num_sub_vectors=num_sub_vectors,
        ),
    )
    return True


def ensure_fts_index(
    table: lancedb.table.Table,
    column: str,
    *,
    language: str = "Swedish",
) -> bool:
    """Build a native FTS (BM25) index on a string ``column`` once it's populated.

    Mirrors the transcript FTS index (``ingest.reindex_fts``): ``with_position``
    for phrase queries, the Swedish stemmer so caption queries match inflections.
    Skips (returns ``False``) while any row is still ``NULL`` — the index would be
    incomplete. Used for ``chunk_frames.caption`` so ``mode=scene`` can keyword-search.
    """
    from lancedb.index import FTS

    nulls = table.count_rows(filter=f"{column} IS NULL")
    if nulls > 0:
        logger.warning("skipping FTS index on %s: %s row(s) still NULL", column, nulls)
        return False
    logger.info("building FTS index on %s (language=%s)", column, language)
    table.create_index(
        column,
        replace=True,
        config=FTS(with_position=True, remove_stop_words=False, language=language),
    )
    return True


def _as_str(path: str | Path | None) -> str | None:
    """Coerce an optional path to ``str`` for APIs that don't take ``Path``."""
    return None if path is None else str(path)
