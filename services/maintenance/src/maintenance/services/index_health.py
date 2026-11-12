"""Which of a dataset's indices an optimize is due on: rows it does not cover, or too many delta segments.

Read-only. Maintenance builds and optimizes no index (ADR 0156 C6); it names the ones a bring-your-own job should
optimize, on the table's due record (:mod:`lakehouse.maintenance_due`).

READ OFF THE MANIFEST ALONE ([[LH-575]]). ``describe_indices()`` lists each index's segments with the fragments each
covers, and each fragment's live rows are its physical rows less the deletions its manifest entry records; neither
reads an object. Measured on pylance 13.0.0, the rows left unindexed and the segment count read this way equal
``stats.index_stats``' ``num_unindexed_rows`` and ``num_indices`` on a covering BTREE, a BTREE after an append, one
whose unindexed fragment lost rows to a delete, an IVF_FLAT after five plain optimizes and one after an append beside
them. ``index_stats`` is asked nothing: it reads the
index's own files under ``_indices/``, which maintenance's store session is granted no read on, since they hold the
table's values; it panics on a JSON index (``lance-index/src/scalar/json.rs:95``, a ``todo!()``); and with two segments
covering one fragment it commits index migrations, then crashes the process (measured on #796's work, 2026-11-12).
"""

from __future__ import annotations

import lance

from lakehouse.maintenance_due import IndexDue


#: Delta segments tolerated before an optimize is due. One is the steady state after an optimize that appended rather
#: than merged; a handful is where query fan-out starts to show.
DEFAULT_MAX_DELTAS = 4


def _live_rows(fragment: lance.LanceFragment) -> int:
    """The rows ``fragment`` holds, as its manifest entry records them: its physical rows less the ones it deleted."""
    deleted = fragment.metadata.deletion_file.num_deleted_rows if fragment.metadata.deletion_file is not None else 0
    return fragment.metadata.physical_rows - (deleted or 0)


def due_indices(ds: lance.LanceDataset, *, max_deltas: int = DEFAULT_MAX_DELTAS) -> list[IndexDue]:
    """Every user index an optimize is due on: one leaving live rows in a fragment no segment covers, or with more than
    ``max_deltas`` segments, which names the merge that clears them (:attr:`IndexDue.num_indices_to_merge`). A system
    index (``__``-named) is the table's own and never listed.

    A describe that fails raises: it says nothing about the indices, and read as "none due" it deleted the table's due
    record (:func:`maintenance.services.due.refresh_due` keeps the record and the pass asks again).
    """
    described = list(ds.describe_indices())
    live = {fragment.fragment_id: _live_rows(fragment) for fragment in ds.get_fragments()}
    due: list[IndexDue] = []
    for index in described:
        if not index.name or index.name.startswith("__"):
            continue
        covered = {fragment_id for segment in index.segments for fragment_id in segment.fragment_ids}
        unindexed_rows = sum(rows for fragment_id, rows in live.items() if fragment_id not in covered)
        deltas = len(index.segments)
        if unindexed_rows > 0 or deltas > max_deltas:
            due.append(IndexDue(name=index.name, unindexed_rows=unindexed_rows, deltas=deltas, num_indices_to_merge=deltas if deltas > max_deltas else None))
    return due
