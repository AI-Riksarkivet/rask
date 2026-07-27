"""Blob-v2 column detection — the single seam every blob decision routes through.

The lance-ns pattern (``services/common/blobs.py``): a blob-v2 column is
identified by the ``lance.blob.v2`` Arrow extension type (registered when
``lance`` is imported), with the raw ``ARROW:extension:name`` field-metadata
fallback for schemas decoded where the extension isn't registered.

The driver needs this because Lance's ``merge_insert`` crashes the blob decoder
on blob-bearing tables (the §7.1 landmine) — any table where
:func:`schema_has_blob` is true must take the ``add_columns``/attach path.
"""

from __future__ import annotations

import pyarrow as pa


BLOB_V2_EXTENSION_NAME = "lance.blob.v2"


def is_blob_field(field: pa.Field) -> bool:
    """True when ``field`` is a Lance blob-v2 column (requires file format >= 2.2)."""
    if getattr(field.type, "extension_name", None) == BLOB_V2_EXTENSION_NAME:
        return True
    metadata = field.metadata or {}
    return metadata.get(b"ARROW:extension:name") == BLOB_V2_EXTENSION_NAME.encode()


def schema_has_blob(schema: pa.Schema) -> bool:
    """True when any field in ``schema`` is a blob-v2 column."""
    return any(is_blob_field(field) for field in schema)


def blob_field_names(schema: pa.Schema) -> list[str]:
    """Names of the blob-v2 columns in ``schema`` (empty when there are none)."""
    return [field.name for field in schema if is_blob_field(field)]
