"""Materialize external blob-v2 columns into Lance-managed bytes — the lance-ns way.

lance-ns's medallion ingest (``services/medallion/services/ingest.py::ingest_to_bronze``)
writes media as a **managed** blob-v2 column — the bytes live *in* the dataset — so a
plain directory copy to S3 carries them and they resolve anywhere, with no client S3
creds and no dangling external pointer. Our ingest writes ``documents.media_blob`` as an
**external** ``Blob.from_uri`` (``file://``) reference, which only resolves where that path
exists (fine on the build box, broken in a pod / on S3-only compute).

This converts external → managed **in place**, mirroring lance-ns's own re-wrap
(``compute._carry_forward``: ``read_blobs`` resolves each pointer to bytes, then
``blob_array`` re-writes them managed at file format 2.2). Run it locally (where the
``file://`` sources still resolve) *before* moving the dataset to S3; afterwards the
dataset is fully self-contained — the RASK_LANDING §4.4 remediation, as code.

    uv run ratch --db parity_new.lance materialize-blobs
    uv run ratch --db parity_new.lance materialize-blobs --table documents
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

import lance
import pyarrow as pa
from lance import blob_array, blob_field

from ratch.core.blobs import blob_field_names
from ratch.core.dataset import overwrite_dataset


logger = logging.getLogger(__name__)


def materialize_blobs(db_path: str | Path, table: str = "documents") -> dict[str, dict[str, int]]:
    """Rewrite every blob-v2 column of ``table`` as Lance-managed bytes (in place).

    External ``Blob.from_uri`` pointers become managed bytes; already-managed columns
    (e.g. inline thumbnails) are re-wrapped to the same bytes. The table is overwritten
    via the sanctioned :func:`~ratch.core.dataset.overwrite_dataset` path so the 2.2 +
    stable-row-id invariants and any descriptor stamp survive. Note the overwrite
    re-creates the table, so it DROPS any indexes (rebuild them after) and null blobs
    stay null; run it on the media table, not an indexed frame table, or reindex.

    Returns ``{column: {"rows": n, "bytes": total}}`` for each materialized blob column.
    """
    uri = str(Path(db_path) / f"{table}.lance")
    ds = lance.dataset(uri)
    schema = ds.schema
    blob_cols = blob_field_names(schema)
    if not blob_cols:
        logger.info("%s: no blob columns — nothing to materialize", table)
        return {}

    n = ds.count_rows()
    non_blob = [f.name for f in schema if f.name not in blob_cols]
    base = ds.to_table(columns=non_blob) if non_blob else pa.table({})

    # overwrite_dataset re-creates the table, so any scalar/vector indexes on it
    # are dropped (and must be rebuilt afterwards). Warn rather than silently
    # degrade search — the default `documents` table is unindexed, but a caller
    # pointing --table at an indexed table (e.g. chunk_frames) would lose them.
    dropped = [str(ix.get("name", "?")) for ix in cast("list[dict[str, Any]]", ds.list_indices())]
    if dropped:
        logger.warning("%s: overwrite drops %d index(es) %s — rebuild after", table, len(dropped), dropped)

    columns: dict[str, Any] = {}
    fields: list[pa.Field] = []
    stats: dict[str, dict[str, int]] = {}
    for field in schema:
        if field.name in blob_cols:
            # read_blobs resolves each pointer (external file:// or inline) to bytes,
            # but SKIPS null rows and returns physical addresses — so a plain list is
            # short and misaligned. Align by row order using the blob descriptor's
            # per-row size (0 = null/absent): read_blobs yields the present rows in
            # ascending row order, so zip it against the presence mask, leaving null
            # rows None. blob_array(bytes|None) then re-writes them MANAGED at the
            # original row count — the lance-ns re-wrap contract, nulls preserved.
            desc = ds.to_table(columns=[field.name]).column(field.name).combine_chunks()
            kinds = desc.field("kind").to_pylist()
            sizes = desc.field("size").to_pylist()
            # Present iff the descriptor references bytes. Per the Lance blob page
            # layout, a MANAGED blob smuggles validity into the description: a null
            # is size 0 (with the position holding the def level), a real value has
            # size > 0. An EXTERNAL blob (file:// / s3://) instead carries kind != 0
            # and reads size 0 until resolved. So: present ⇔ kind != 0 or size > 0.
            # read_blobs skips exactly the absent (null) ones, in ascending row
            # order — we zip its stream against this mask, leaving nulls None, which
            # blob_array supports (its own docs mix inline/external/null in one array).
            present = [k != 0 or s > 0 for k, s in zip(kinds, sizes, strict=True)]
            resolved = (p for _addr, p in ds.read_blobs(field.name, indices=list(range(n))))
            payloads = [next(resolved) if is_present else None for is_present in present]
            columns[field.name] = blob_array(payloads)
            fields.append(blob_field(field.name))
            filled = [p for p in payloads if p is not None]
            stats[field.name] = {"rows": len(filled), "bytes": sum(len(p) for p in filled)}
        else:
            columns[field.name] = base.column(field.name)
            fields.append(field)

    out = pa.table(
        {field.name: columns[field.name] for field in schema},
        schema=pa.schema(fields, metadata=schema.metadata),
    )
    overwrite_dataset(uri, out)
    logger.info("materialized %d blob column(s) of %s: %s", len(blob_cols), table, stats)
    return stats
