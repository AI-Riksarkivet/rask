"""The single table-create/append/overwrite path (P1.5 of LANCE_MEDIA_MERGE).

Every Lance table this pipeline creates goes through :func:`create_dataset`, so
the create-time-only invariants can never be forgotten at a call site:

* ``data_storage_version="2.2"`` — blob-v2 columns require it;
* ``enable_stable_row_ids=True`` — ``_rowid``-holding features (blob attach,
  atlas selection) survive compaction;
* the ``lance_media.descriptor`` schema-metadata stamp — the declared half of
  the dataset descriptor (§4.2), read back by the schema-agnostic backend.

Appends/overwrites also route through here (:func:`append_rows`,
:func:`overwrite_dataset`) so the P1.5 grep gate can hold: no direct
``lance.write_dataset`` outside this module. Overwrite RE-creates the dataset,
so it re-applies all create-time flags — the subtle bug this module exists to
prevent.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import lance
import pyarrow as pa


if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

DESCRIPTOR_METADATA_KEY = b"lance_media.descriptor"
DATA_STORAGE_VERSION = "2.2"


def _with_descriptor(schema: pa.Schema, descriptor: Mapping[str, Any] | None) -> pa.Schema:
    if descriptor is None:
        return schema
    metadata = dict(schema.metadata or {})
    metadata[DESCRIPTOR_METADATA_KEY] = json.dumps(descriptor, ensure_ascii=False).encode()
    return schema.with_metadata(metadata)


def empty_table(schema: pa.Schema) -> pa.Table:
    """A zero-row table of ``schema`` — the shape actor computes return when a
    whole batch yields nothing (and what a fresh dataset is created from)."""
    return pa.table({f.name: pa.array([], type=f.type) for f in schema}, schema=schema)


def create_dataset(
    path: str | Path,
    schema: pa.Schema,
    *,
    descriptor: Mapping[str, Any] | None = None,
    data: pa.Table | pa.RecordBatch | None = None,
    allow_external_blobs: bool = False,
) -> lance.LanceDataset:
    """Create a Lance dataset with the lance-media invariants applied.

    ``data`` seeds initial rows (it must match ``schema``); omitted, the table
    is created empty and populated by later :func:`append_rows` calls.
    ``allow_external_blobs`` permits Blob-v2 external URIs outside registered
    base paths (the documents table's ``file://``/``hf://`` media pointers).
    """
    stamped = _with_descriptor(schema, descriptor)
    table = empty_table(stamped)
    if data is not None:
        table = pa.Table.from_batches(data.to_batches() if isinstance(data, pa.Table) else [data], schema=stamped)
    logger.info("creating lance dataset %s (storage %s, stable row ids)", path, DATA_STORAGE_VERSION)
    return lance.write_dataset(
        table,
        str(path),
        schema=stamped,
        mode="create",
        data_storage_version=DATA_STORAGE_VERSION,
        enable_stable_row_ids=True,
        allow_external_blob_outside_bases=allow_external_blobs,
    )


def append_rows(
    path: str | Path,
    data: pa.Table | pa.RecordBatch,
    *,
    allow_external_blobs: bool = False,
) -> lance.LanceDataset:
    """Append rows to an existing dataset (create-time flags are inherited)."""
    return lance.write_dataset(
        data,
        str(path),
        mode="append",
        allow_external_blob_outside_bases=allow_external_blobs,
    )


def overwrite_dataset(
    path: str | Path,
    data: pa.Table,
    *,
    descriptor: Mapping[str, Any] | None = None,
) -> lance.LanceDataset:
    """Replace a dataset's contents, RE-applying every create-time invariant."""
    stamped_schema = _with_descriptor(data.schema, descriptor)
    logger.info("overwriting lance dataset %s (storage %s, stable row ids)", path, DATA_STORAGE_VERSION)
    return lance.write_dataset(
        data.cast(stamped_schema) if descriptor is not None else data,
        str(path),
        mode="overwrite",
        data_storage_version=DATA_STORAGE_VERSION,
        enable_stable_row_ids=True,
    )


def read_descriptor(path: str | Path) -> dict[str, Any] | None:
    """The declared descriptor stamped on a dataset's schema, if any."""
    metadata = lance.dataset(str(path)).schema.metadata or {}
    raw = metadata.get(DESCRIPTOR_METADATA_KEY)
    return None if raw is None else json.loads(raw.decode())
