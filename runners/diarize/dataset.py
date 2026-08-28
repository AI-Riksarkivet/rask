"""The single Lance table create/append/overwrite path for this runner.

Vendored from ``ratch.core.dataset`` at the ratch dissolution (2026-08-28 —
``open_ray-kernel.md``, moves 10 and 11). It is a COPY rather than an import,
and that is the seal working rather than drift: this runner is SEALED — its own
``pyproject.toml`` pins ``requires-python = ">=3.10,<3.13"`` for the cu128 torch
stack, while every platform package (``ratch``, ``service-kit``, ``ray-kit``) is
``>=3.13``. No platform package can be imported here at all, so the write seam
this runner needs has to live in the runner.

Every Lance write this runner makes goes through this module, so the
create-time-only invariants can never be forgotten at a call site:

* ``data_storage_version="2.2"`` — blob-v2 columns require it (``speaker_turns``
  holds none today, but the tier is kept at 2.2 for consistency with the other
  tables, and an overwrite that silently dropped to the default would fork it);
* ``enable_stable_row_ids=True`` — ``_rowid``-holding features (blob attach,
  atlas selection) survive compaction.

Overwrite RE-creates the dataset, so it re-applies all create-time flags — the
subtle bug this module exists to prevent, and the reason ``overwrite_dataset``
is a function here rather than an inline ``lance.write_dataset(mode=...)`` at
the call site.

What did NOT travel from the origin, and why:

* ``create_dataset`` and ``read_descriptor`` — no caller in this runner.
* the ``descriptor`` schema-metadata stamp (``_with_descriptor``,
  ``DESCRIPTOR_METADATA_KEY``) — that is the declared half of the platform's
  dataset descriptor, read back by the schema-agnostic backend. A sealed runner
  is not part of that contract and never passed one; with ``descriptor=None``
  the origin's ``overwrite_dataset`` reduces exactly to the one below.
* ``allow_external_blobs`` on ``append_rows`` — ``speaker_turns`` has no Blob V2
  column, and ``False`` is Lance's own default for the underlying flag.
"""

from __future__ import annotations

import logging
from pathlib import Path

import lance
import pyarrow as pa


logger = logging.getLogger(__name__)

DATA_STORAGE_VERSION = "2.2"


def empty_table(schema: pa.Schema) -> pa.Table:
    """A zero-row table of ``schema`` — the shape actor computes return when a
    whole batch yields nothing (and what a fresh dataset is created from)."""
    return pa.table({f.name: pa.array([], type=f.type) for f in schema}, schema=schema)


def append_rows(path: str | Path, data: pa.Table | pa.RecordBatch) -> lance.LanceDataset:
    """Append rows to an existing dataset (create-time flags are inherited)."""
    return lance.write_dataset(data, str(path), mode="append")


def overwrite_dataset(path: str | Path, data: pa.Table) -> lance.LanceDataset:
    """Replace a dataset's contents, RE-applying every create-time invariant."""
    logger.info("overwriting lance dataset %s (storage %s, stable row ids)", path, DATA_STORAGE_VERSION)
    return lance.write_dataset(
        data,
        str(path),
        mode="overwrite",
        data_storage_version=DATA_STORAGE_VERSION,
        enable_stable_row_ids=True,
    )
