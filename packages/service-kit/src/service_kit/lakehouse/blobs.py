"""Blob-v2 column detection, shared across services.

Lance blob-v2 columns require file format ``>= 2.2`` and are identified by the
``lance.blob.v2`` Arrow extension type (registered when ``lance`` is imported).
These helpers recognise a blob column from an Arrow schema without materialising
the payloads; blob serving, the cascade's column map, the stage job's media lane
(``scripts/ray_stage_job.py``) and the pointer-health probe use them.

Detection picks no storage version: no writer chooses one from the schema, and every create in
``services/``, ``packages/`` and ``scripts/`` names ``data_storage_version="2.2"`` (grep, 2026-09-25).

:func:`read_aligned_table` is the READ counterpart: one scan whose blob columns arrive as bytes,
row-aligned with the tabular columns and with a null payload as ``None`` on its own row.
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
    "EXTERNAL_KIND",
    "blob_column_resolves",
    "blob_field_names",
    "carried_blob_values",
    "carry_external_descriptor",
    "dangling_blob_columns",
    "external_base_of",
    "is_blob_field",
    "read_aligned_table",
    "schema_has_blob",
]

if TYPE_CHECKING:
    import lance

log = logging.getLogger(__name__)

#: Arrow extension name Lance stamps on a blob-v2 column (lance_docs/guide.md — Version Compatibility).
#:
#: RE-EXPORTED from `lancekit.blobs`, which is the ONE implementation. The four-function detection
#: seam existed three times in this repo — a third copy died with the pipeline package — and every
#: copy of "which Arrow extension name marks a blob column" is another place for the answer to
#: drift. `lancekit` is the canonical one because it is the standalone, dependency-light seam by
#: contract; this module keeps only what it adds on top.

#: The descriptor `kind` for an EXTERNAL blob — the payload lives at a URI the dataset does not own.
#: Measured, not documented: 0 inline, 1 packed, 2 dedicated, 3 external.
EXTERNAL_KIND = 3


def external_base_of(ds: lance.LanceDataset) -> str | None:
    """The external base this dataset's blob URIs are relative to, or None if it owns its bytes.

    None is the MANAGED answer and is not an error: a dataset whose payloads exist at no URI (an
    Arrow-IPC fragment landed by `lance-append`, a source whose lifecycle is not the estate's) must
    own them, and a caller reading None should copy rather than refuse.

    THE MANIFEST ONLY. Bases are manifest state (`lance_docs/file_format.md` § Base Path System),
    written by the same commit as the data; `ds._ds.base_paths()` returns every registered base with
    its name, path and `is_dataset_root` flag and survives a reopen (probed on pylance 10.0.0). A
    schema-metadata key naming a base is not read ([[LH-208]]): schema metadata travels with a schema
    copy and any table writer could set it, and a forged one made the in-process cascade null every
    payload and register the forged base on the tier above.
    """
    for base in (_registered_bases(ds) or {}).values():
        path = getattr(base, "path", None)
        if path:
            return str(path)
    return None


def _registered_bases(ds: lance.LanceDataset) -> dict[int, object] | None:
    """`ds`'s registered bases, or None where the accessor is unavailable.

    Reached through `_ds` because pylance exposes no public wrapper — the same private tier
    `service_kit.lakehouse.features` already relies on for `serialized_manifest()`. Guarded rather
    than assumed: a pylance upgrade that renames or removes it degrades to the managed answer, which
    copies the bytes rather than breaking every blob-carrying stage runner.
    """
    accessor = getattr(getattr(ds, "_ds", None), "base_paths", None)
    if accessor is None:
        return None
    try:
        return accessor()
    except Exception:  # noqa: BLE001 — any failure here means "managed", never a failed stage
        return None


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


def carried_blob_values(ds: lance.LanceDataset, column: str, descriptors: list[object], row_ids: list[int], base: str) -> list[object | None]:
    """One blob column of a dataset with an external base, as the values a downstream write takes.

    AN EXTERNAL BASE DOES NOT MAKE EVERY ROW EXTERNAL. Blob V2 places each value by itself: a
    `Blob(uri=...)` under the base is kind 3, and bytes written beside it land inline (0), packed (1) or
    dedicated (2) by the column's thresholds (`lance_docs/lancemultibasebranchingblobv2.md`, Blob V2 storage
    kinds). Kind 3 rows are forwarded as pointers; every other non-null row owns bytes that exist at no
    URI, so those bytes are read and carried. Measured on pylance 12.0.0: one column held kinds
    [3, 0, 1, 2, null], and mapping only kind 3 wrote [Blob, None, None, None, None] ([[LH-217]]).

    `descriptors` and `row_ids` are one scan's blob descriptors and `_rowid`s, row-aligned. The managed
    rows are read by row id with `preserve_order=True`, and only they are read: a null row keeps the `None`
    its descriptor mapped to, and no external row's object is opened.
    """
    values = [carry_external_descriptor(descriptor, base) for descriptor in descriptors]
    managed = [i for i, d in enumerate(descriptors) if isinstance(d, dict) and d.get("kind") != EXTERNAL_KIND]
    if managed:
        read = ds.read_blobs(column, ids=[row_ids[i] for i in managed], preserve_order=True)
        for position, (_, payload) in zip(managed, read, strict=True):
            values[position] = payload
    return values


def read_aligned_table(
    ds: lance.LanceDataset,
    *,
    columns: list[str] | None = None,
    with_row_id: bool = False,
    limit: int | None = None,
) -> pa.Table:
    """One ROW-ALIGNED scan whose blob-v2 columns arrive as ``large_binary`` bytes, **nulls included**.

    ``blob_handling="all_binary"`` returns tabular and blob columns from the same scan, with a null payload as
    ``None`` on its own row, so a caller pairing payloads with the rest of the row needs no second read to keep them
    aligned, and the payload list can be handed straight back to :func:`lance.blob_array` (which accepts ``None``
    entries) to re-wrap a blob column for a 2.2 write.

    ``read_blobs`` and ``take_blobs`` keep a null row's slot too (measured on pylance 12.0.0: ``read_blobs`` over
    indices ``[0, 1, 2]`` with a null in slot 1 answers ``[(0, b'a'), (1, None), (2, b'c')]``, and ``take_blobs``
    answers ``[BlobFile, None, BlobFile]``). They read by row, and are the reads for single-row serving and for a
    known set of row ids (:func:`carried_blob_values`, :func:`blob_column_resolves`, the viewer's blob endpoints).

    THIS IS A WHOLE-RESULT READ: ``to_table`` holds every selected payload at once. ``limit`` bounds it for a caller
    that needs a few rows; a caller that consumes every payload streams a bounded scanner instead
    (``medallion.services.compute._blob_slices``, [[CP-051]]).
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
