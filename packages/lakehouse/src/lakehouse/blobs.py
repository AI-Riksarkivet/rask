"""Blob-v2 column detection, shared across services.

Lance blob-v2 columns require file format ``>= 2.2`` and are identified by the
``lance.blob.v2`` Arrow extension type (registered when ``lance`` is imported).
These helpers recognise a blob column from an Arrow schema without materialising
the payloads; blob serving and the pointer-health probe use them.

Detection picks no storage version: no writer chooses one from the schema, and every create in
``services/``, ``packages/`` and ``scripts/`` names ``data_storage_version="2.2"`` (grep, 2026-09-25).

:func:`read_aligned_table` is the READ counterpart: one scan whose blob columns arrive as bytes,
row-aligned with the tabular columns and with a null payload as ``None`` on its own row.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pyarrow as pa

from lakehouse.lancekit.blobs import BLOB_V2_EXTENSION_NAME, blob_descriptors, blob_field_names, holds_blob, is_blob_field, nested_fields, schema_has_blob


#: Re-exported deliberately: callers import these FROM here and the names are part of this module's
#: surface, so ruff must not read them as unused.
__all__ = [
    "BLOB_V2_EXTENSION_NAME",
    "EXTERNAL_KIND",
    "blob_descriptors",
    "blob_field_names",
    "carry_external_descriptor",
    "holds_blob",
    "is_blob_field",
    "nested_fields",
    "read_aligned_table",
    "schema_has_blob",
]

if TYPE_CHECKING:
    import lance

log = logging.getLogger(__name__)

#: Arrow extension name Lance stamps on a blob-v2 column (lance_docs/lance/guide/blob.md — Version Compatibility).
#:
#: RE-EXPORTED from `lancekit.blobs`, which is the ONE implementation. The four-function detection
#: seam existed three times in this repo — a third copy died with the pipeline package — and every
#: copy of "which Arrow extension name marks a blob column" is another place for the answer to
#: drift. `lancekit` is the canonical one because it is the standalone, dependency-light seam by
#: contract; this module keeps only what it adds on top.

#: The descriptor `kind` for an EXTERNAL blob — the payload lives at a URI the dataset does not own.
#: Measured, not documented: 0 inline, 1 packed, 2 dedicated, 3 external.
EXTERNAL_KIND = 3


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

    ``blob_handling="all_binary"`` returns tabular and blob columns from the same scan, with a null payload as
    ``None`` on its own row, so a caller pairing payloads with the rest of the row needs no second read to keep them
    aligned, and the payload list can be handed straight back to :func:`lance.blob_array` (which accepts ``None``
    entries) to re-wrap a blob column for a 2.2 write.

    ``read_blobs`` and ``take_blobs`` keep a null row's slot too (measured on pylance 12.0.0: ``read_blobs`` over
    indices ``[0, 1, 2]`` with a null in slot 1 answers ``[(0, b'a'), (1, None), (2, b'c')]``, and ``take_blobs``
    answers ``[BlobFile, None, BlobFile]``). They read by row, and are the reads for single-row serving and for a
    known set of row ids.

    THIS IS A WHOLE-RESULT READ: ``to_table`` holds every selected payload at once. ``limit`` bounds it for a caller
    that needs a few rows; a caller that consumes every payload streams a bounded scanner instead ([[CP-051]]).
    """
    return ds.scanner(columns=columns, blob_handling="all_binary", with_row_id=with_row_id, limit=limit).to_table()
