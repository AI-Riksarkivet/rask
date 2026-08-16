"""Blob-v2 column detection, shared across services.

Lance blob-v2 columns require file format ``>= 2.2`` and are identified by the
``lance.blob.v2`` Arrow extension type (registered when ``lance`` is imported).
These helpers let the catalog (create path) and the medallion compute (cascade)
recognise a blob column from an Arrow schema without materialising the payloads.

A blob-v2 column cannot be written at the default 2.1 format, so detecting one is
what routes a write onto the ``data_storage_version="2.2"`` path.

:func:`read_aligned_table` is the READ counterpart: the one blob read path that keeps
row alignment when a payload is null (the ``read_blobs``/``take_blobs`` landmine).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pyarrow as pa


if TYPE_CHECKING:
    import lance

log = logging.getLogger(__name__)

#: Arrow extension name Lance stamps on a blob-v2 column (lance_docs/guide.md — Version Compatibility).
BLOB_V2_EXTENSION_NAME = "lance.blob.v2"


def is_blob_field(field: pa.Field) -> bool:
    """True when ``field`` is a Lance blob-v2 column (requires file format >= 2.2).

    Prefers the registered extension type's ``extension_name`` (present when ``lance`` is imported,
    which every service does) and falls back to the raw ``ARROW:extension:name`` field metadata for
    a schema decoded in a process where the extension is not registered.
    """
    if getattr(field.type, "extension_name", None) == BLOB_V2_EXTENSION_NAME:
        return True
    metadata = field.metadata or {}
    return metadata.get(b"ARROW:extension:name") == BLOB_V2_EXTENSION_NAME.encode()


def schema_has_blob(schema: pa.Schema) -> bool:
    """True when any field in ``schema`` is a Lance blob-v2 column."""
    return any(is_blob_field(field) for field in schema)


def blob_field_names(schema: pa.Schema) -> list[str]:
    """Names of the blob-v2 columns in ``schema`` (empty when there are none)."""
    return [field.name for field in schema if is_blob_field(field)]


def read_aligned_table(
    ds: lance.LanceDataset,
    *,
    columns: list[str] | None = None,
    with_row_id: bool = False,
) -> pa.Table:
    """One ROW-ALIGNED scan whose blob-v2 columns arrive as ``large_binary`` bytes, **nulls included**.

    THE null-blob landmine guard (``docs/architecture/lance-blob-v2-findings.md``, re-measured on pylance
    9.0.0): ``read_blobs`` / ``take_blobs`` silently DROP null rows — 3 selected rows with one null blob
    return 2 payloads — so pairing their output positionally against a second scan of the tabular columns
    is length-mismatched the moment ONE payload is null. That is precisely the medallion's own
    "a failed harvest, a skipped page" case, and it turned a single null page into an opaque
    ``ArrowInvalid: Column 1 named payload expected length 3 but got length 2`` that the movers route as a
    TRANSIENT failure (RETRY storm → DLQ) even though redelivery can never fix it.

    ``blob_handling="all_binary"`` is the read path that preserves logical cardinality (measured: 5 rows in,
    5 rows out, the nulls correctly ``None``), so alignment holds **by construction** — no presence mask to
    maintain, one scan instead of two, and the payload list can be handed straight back to
    :func:`lance.blob_array` (which accepts ``None`` entries) to re-wrap a blob column for a 2.2 write.

    Prefer this over ``read_blobs``/``take_blobs`` whenever payloads are consumed BY ROW POSITION. The
    take-path remains correct for single-row serving (``ids=[rowid]``, where an empty result IS the null
    signal) — see :func:`blob_column_resolves` and the viewer's blob endpoints.
    """
    return ds.scanner(columns=columns, blob_handling="all_binary", with_row_id=with_row_id).to_table()


def blob_column_resolves(ds: lance.LanceDataset, column: str) -> bool:
    """Whether ``column``'s blob payloads actually dereference — probed on the FIRST and LAST rows.

    The SHARED pointer-health probe (quality gate at promotion time; reconcile after the fact). One
    real byte is read per probed payload: ``BlobFile.size()`` reads only the stored descriptor
    (probed at pylance 8.0.0 — it succeeds against a DELETED object), so only an actual
    ``read_range`` proves the bytes are reachable; and for a dangling EXTERNAL pointer even
    ``take_blobs`` itself raises (it opens the object), which is why the whole probe sits in the
    try. First+last catches the wholesale failures these checks exist for (wiped bucket, wrong or
    unregistered external base) at the cost of two 1-byte reads; per-row bitrot auditing is a
    scrubber's job. Zero-length/null payloads resolve trivially — through pylance 9 ``take_blobs`` returned no
    handle for them at all; from 10.0.0 it returns ``None`` in that slot, which the loop skips. An EMPTY dataset
    resolves trivially too (nothing to probe).
    """
    rows = ds.count_rows()
    if rows == 0:
        return True
    try:
        for row in sorted({0, rows - 1}):
            for handle in ds.take_blobs(column, indices=[row]):
                # `handle is None` IS the null payload, and testing for it is required from pylance
                # 10.0.0. Through 9.0.0 `take_blobs` OMITTED a null row from its result, so the loop
                # simply did not run for it — the "returns no handle for them" the docstring above
                # describes. 10.0.0 returns a same-length list with `None` in that slot instead
                # (measured 2026-08-16: row 0 -> BlobFile, row 1 (null) -> None, row 2 -> BlobFile),
                # so the old loop reached `None.size()` and raised AttributeError — turning a healthy
                # dataset whose first or last payload is null into a reported DANGLING column, which
                # is a promotion-blocking verdict on correct data.
                if handle is None:
                    continue
                if handle.size() > 0:
                    handle.read_range(0, 1)
    except Exception as exc:
        log.warning("blob_resolve_failed", extra={"column": column, "error": str(exc)})
        return False
    return True


def dangling_blob_columns(ds: lance.LanceDataset) -> list[str]:
    """The blob-v2 columns of ``ds`` whose payloads do NOT dereference (empty = healthy or no blobs)."""
    return [column for column in blob_field_names(ds.schema) if not blob_column_resolves(ds, column)]
