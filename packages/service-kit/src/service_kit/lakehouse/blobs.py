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

from service_kit.lancekit.blobs import BLOB_V2_EXTENSION_NAME, blob_field_names, is_blob_field, schema_has_blob


#: Re-exported deliberately: callers import these FROM here and the names are part of this module's
#: surface, so ruff must not read them as unused.
__all__ = [
    "BLOB_V2_EXTENSION_NAME",
    "EXTERNAL_BASE_KEY",
    "EXTERNAL_KIND",
    "blob_column_resolves",
    "blob_field_names",
    "carry_external_descriptor",
    "dangling_blob_columns",
    "external_base_of",
    "is_blob_field",
    "read_aligned_table",
    "schema_has_blob",
    "stamp_external_base",
]

if TYPE_CHECKING:
    import lance

log = logging.getLogger(__name__)

#: Arrow extension name Lance stamps on a blob-v2 column (lance_docs/guide.md — Version Compatibility).
#:
#: RE-EXPORTED from `lancekit.blobs`, which is the ONE implementation. The four-function detection
#: seam existed three times in this repo — here, in `lancekit`, and in `ratch.core` — and three
#: copies of "which Arrow extension name marks a blob column" is three places for the answer to
#: drift. `lancekit` is the canonical one because it is the standalone, dependency-light seam by
#: contract; this module keeps only what it adds on top.

#: The descriptor `kind` for an EXTERNAL blob — the payload lives at a URI the dataset does not own.
#: Measured, not documented: 0 inline, 1 packed, 2 dedicated, 3 external.
EXTERNAL_KIND = 3

#: Schema-metadata key naming the external base a dataset's blob URIs are relative to.
#:
#: THIS EXISTS BECAUSE PYLANCE EXPOSES NO WAY TO READ A DATASET'S REGISTERED BASES. `add_bases`
#: writes them and nothing reads them back; the base path is not recoverable from the manifest
#: either (probed on pylance 10.0.0). And it has to be recoverable, because a scanned descriptor's
#: `blob_uri` is BASE-RELATIVE — carrying one into another dataset verbatim is refused with
#: "outside registered external bases", so a mover cannot forward a pointer it cannot resolve.
#:
#: Stamped into the SCHEMA rather than kept in config for the same reason #21 puts the lineage
#: coordinates there: the data becomes self-describing, and a mover that has never met the service
#: that wrote it can still resolve the pointer from the dataset alone. Verified to survive both
#: `create` and `append`.
EXTERNAL_BASE_KEY = b"rask.blob.external_base"


def external_base_of(ds: lance.LanceDataset) -> str | None:
    """The external base this dataset's blob URIs are relative to, or None if it owns its bytes.

    None is the MANAGED answer and is not an error: a dataset whose payloads exist at no URI (an
    Arrow-IPC fragment landed by `lance-append`, a source whose lifecycle is not the estate's) must
    own them, and a caller reading None should copy rather than refuse.
    """
    raw = (ds.schema.metadata or {}).get(EXTERNAL_BASE_KEY)
    return raw.decode() if raw else None


def stamp_external_base(schema: pa.Schema, base: str | None) -> pa.Schema:
    """`schema` carrying `base` in its metadata — the write half of :func:`external_base_of`.

    Merges rather than replaces: the estate stamps other self-describing coordinates into the same
    map (#21's `lineage.*`), and a replace here would silently destroy them.
    """
    if not base:
        return schema
    return schema.with_metadata({**(schema.metadata or {}), EXTERNAL_BASE_KEY: base.encode()})


def carry_external_descriptor(descriptor: object, base: str) -> object | None:
    """One scanned descriptor, mapped onto the shape a WRITE takes. None when it is not external.

    THE READ AND WRITE SHAPES ARE NOT SYMMETRIC, which is why this is a mapping and not a copy. A
    scan returns ``struct<kind, position, size, blob_id, blob_uri>``; a write takes a ``Blob``. Two
    details in between are silent if got wrong, and both were got wrong first:

    * ``blob_uri`` is RELATIVE to the base (``page-000.bin``, not a URI). Passing it through is
      refused at write — loudly, which is the good case.
    * ``size == 0`` means THE WHOLE OBJECT. Passing it back as a slice length asks for zero bytes and
      yields an empty read with no error at all — the silent case.
    """
    from lance.blob import Blob

    if not isinstance(descriptor, dict) or descriptor.get("kind") != EXTERNAL_KIND:
        return None
    relative = descriptor.get("blob_uri")
    if not relative:
        return None
    absolute = f"{base.rstrip('/')}/{relative}"
    size = descriptor.get("size") or 0
    if size:
        return Blob(uri=absolute, position=descriptor.get("position") or 0, size=size)
    return Blob.from_uri(absolute)


def read_aligned_table(
    ds: lance.LanceDataset,
    *,
    columns: list[str] | None = None,
    with_row_id: bool = False,
    limit: int | None = None,
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

    ``limit`` bounds the scan. It exists because a caller that only needs to LOOK at a payload — to
    decide whether a deriver applies, say — otherwise materialises the whole corpus to answer a
    question about one row. Unbounded stays the default: every caller that consumes payloads by row
    position needs all of them, and a silent cap there would misalign the output.

    Prefer this over ``read_blobs``/``take_blobs`` whenever payloads are consumed BY ROW POSITION. The
    take-path remains correct for single-row serving (``ids=[rowid]``, where an empty result IS the null
    signal) — see :func:`blob_column_resolves` and the viewer's blob endpoints.
    """
    return ds.scanner(columns=columns, blob_handling="all_binary", with_row_id=with_row_id, limit=limit).to_table()


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
