"""Blob-v2 column detection, for a client reading a Lance table's schema.

A blob-v2 column is identified by the ``lance.blob.v2`` Arrow extension type, registered when ``lance`` is imported, with
the raw ``ARROW:extension:name`` field metadata as the fallback when it is not (`lance_docs/lance/format/table/blob.md`).
"""

from __future__ import annotations

import pyarrow as pa


__all__ = ["BLOB_V2_EXTENSION_NAME", "blob_field_names", "is_blob_field"]

BLOB_V2_EXTENSION_NAME = "lance.blob.v2"


def is_blob_field(field: pa.Field) -> bool:
    """True when ``field`` is a Lance blob-v2 column (file format >= 2.2)."""
    if getattr(field.type, "extension_name", None) == BLOB_V2_EXTENSION_NAME:
        return True
    metadata = field.metadata or {}
    return metadata.get(b"ARROW:extension:name") == BLOB_V2_EXTENSION_NAME.encode()


def blob_field_names(schema: pa.Schema) -> list[str]:
    """Names of the blob-v2 columns in ``schema``, empty when there are none."""
    return [field.name for field in schema if is_blob_field(field)]
