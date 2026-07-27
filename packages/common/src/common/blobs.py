"""Blob-v2 column detection, shared across services.

Lance blob-v2 columns require file format ``>= 2.2`` and are identified by the
``lance.blob.v2`` Arrow extension type (registered when ``lance`` is imported).
These helpers let the catalog (create path) and the medallion compute (cascade)
recognise a blob column from an Arrow schema without materialising the payloads.

A blob-v2 column cannot be written at the default 2.1 format, so detecting one is
what routes a write onto the ``data_storage_version="2.2"`` path.
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


def blob_column_resolves(ds: lance.LanceDataset, column: str) -> bool:
    """Whether ``column``'s blob payloads actually dereference — probed on the FIRST and LAST rows.

    The SHARED pointer-health probe (quality gate at promotion time; reconcile after the fact). One
    real byte is read per probed payload: ``BlobFile.size()`` reads only the stored descriptor
    (probed at pylance 8.0.0 — it succeeds against a DELETED object), so only an actual
    ``read_range`` proves the bytes are reachable; and for a dangling EXTERNAL pointer even
    ``take_blobs`` itself raises (it opens the object), which is why the whole probe sits in the
    try. First+last catches the wholesale failures these checks exist for (wiped bucket, wrong or
    unregistered external base) at the cost of two 1-byte reads; per-row bitrot auditing is a
    scrubber's job. Zero-length/null payloads resolve trivially (``take_blobs`` returns no handle
    for them — the same probed behavior the catalog's serving path guards). An EMPTY dataset
    resolves trivially too (nothing to probe).
    """
    rows = ds.count_rows()
    if rows == 0:
        return True
    try:
        for row in sorted({0, rows - 1}):
            for handle in ds.take_blobs(column, indices=[row]):
                if handle.size() > 0:
                    handle.read_range(0, 1)
    except Exception as exc:
        log.warning("blob_resolve_failed", extra={"column": column, "error": str(exc)})
        return False
    return True


def dangling_blob_columns(ds: lance.LanceDataset) -> list[str]:
    """The blob-v2 columns of ``ds`` whose payloads do NOT dereference (empty = healthy or no blobs)."""
    return [column for column in blob_field_names(ds.schema) if not blob_column_resolves(ds, column)]
