"""Vector-index maintenance for this runner's Lance tables.

VENDORED from ratch at the dissolution (2026-08-28, ``open_ray-kernel.md``); origin
``packages/ratch/src/ratch/core/engine.py``, narrowed to :func:`ensure_vector_index`. A COPY because
the runner is sealed (``requires-python >=3.10,<3.13``; platform packages are ``>=3.13``).

TAKEN: :func:`ensure_vector_index` alone. It is a closed set — it calls no other function in the
origin module. The origin's private helpers (``_ValueCheckpoint``, ``_fill_null_scan_column``,
``_read_blobs``, ``_as_str``) belong to the data-evolution functions (``upsert_scan_column``,
``upsert_blob_column``, ``attach_values_by_rowid``) and to ``ensure_fts_index``, none of which this
runner calls; taking them would have vendored the origin's ``lance``/``json``/``Path`` imports for
code with no caller here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal


if TYPE_CHECKING:
    import lancedb

logger = logging.getLogger(__name__)


def ensure_vector_index(
    table: lancedb.table.Table,
    column: str,
    *,
    num_partitions: int,
    num_sub_vectors: int,
    metric: Literal["l2", "cosine", "dot"] = "cosine",
) -> bool:
    """Build an IVF_PQ index on ``column`` once it is fully populated.

    Returns ``True`` if an index was (re)built, ``False`` if skipped. Two
    preconditions are checked, both of which otherwise crash Lance's trainer:

    * every row must have a vector — the IVF trainer rejects partial-``NULL``
      columns;
    * the table must have at least ``num_partitions`` rows — IVF k-means needs at
      least one training vector per partition. Below that, flat (brute-force)
      search is used until the table grows, which is fine at small scale.

    Rebuilding after compaction is the caller's responsibility (compaction
    invalidates the row addresses the index points at).
    """
    nulls = table.count_rows(filter=f"{column} IS NULL")
    if nulls > 0:
        logger.warning("skipping index on %s: %s row(s) still NULL", column, nulls)
        return False
    row_count = table.count_rows()
    if row_count < num_partitions:
        logger.warning(
            f"skipping index on {column}: {row_count} row(s) < num_partitions={num_partitions} "
            f"(IVF_PQ needs ≥ num_partitions rows to train; flat search is used until then)"
        )
        return False
    logger.info(f"building IVF_PQ {metric} index on {column} (num_partitions={num_partitions}, num_sub_vectors={num_sub_vectors})")
    from lancedb.index import IvfPq

    table.create_index(
        column,
        replace=True,
        config=IvfPq(
            distance_type=metric,
            num_partitions=num_partitions,
            num_sub_vectors=num_sub_vectors,
        ),
    )
    return True
