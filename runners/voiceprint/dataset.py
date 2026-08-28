"""The single table-append/overwrite path — the create-time invariants, applied in ONE place.

VENDORED from ratch at the dissolution (2026-08-28, ``open_ray-kernel.md``); origin
``packages/ratch/src/ratch/core/dataset.py``, narrowed to the three functions this runner calls. A
COPY because the runner is sealed (``requires-python >=3.10,<3.13``; platform packages are
``>=3.13``).

The origin's reason for existing is the reason to carry it here rather than inline
``lance.write_dataset`` at the call sites: the create-time-only invariants can then never be
forgotten at one of them —

* ``data_storage_version="2.2"`` — blob-v2 columns require it;
* ``enable_stable_row_ids=True`` — ``_rowid``-holding features (blob attach, atlas selection)
  survive compaction.

Overwrite RE-creates the dataset, so it re-applies all create-time flags — the subtle bug this
module exists to prevent.

TAKEN: :func:`empty_table`, :func:`append_rows`, :func:`overwrite_dataset`.
LEFT BEHIND: ``create_dataset``, ``read_descriptor``, and with them the third create-time invariant
— the ``lance_media.descriptor`` schema-metadata stamp — plus the ``allow_external_blobs`` knob.
This runner writes ``speaker_embeddings`` and ``speakers``; neither carries a Blob V2 column or a
descriptor, and no call site here ever passed either. They are dropped rather than kept dark, so
nothing in this file reads as a knob someone may turn.
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
