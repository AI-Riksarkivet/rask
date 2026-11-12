"""Which indices an optimize is due on: the index half of a table's due record (ADR 0162).

Maintenance optimizes no index; it names the indices a bring-your-own job should optimize. An index is due when it
leaves rows unindexed — queries touching them fall back to a flat scan — or carries more delta segments than a query
should fan out over. Both are read off the manifest (``describe_indices()``'s segments and the fragments' recorded rows),
which on pylance 13.0.0 answer what ``stats.index_stats`` does without reading the index's files ([[LH-575]]).
"""

from __future__ import annotations

from pathlib import Path

import lance
import numpy as np
import pyarrow as pa

from lakehouse.maintenance_due import IndexDue
from maintenance.services.index_health import due_indices


def _indexed(tmp_path: Path, *, rows: int = 200) -> str:
    """A dataset with a scalar index over every row."""
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"id": pa.array(range(rows), pa.int64())}), uri)
    lance.dataset(uri).create_scalar_index("id", index_type="BTREE", name="id_btree")
    return uri


def test_UNINDEXED_ROWS_make_the_index_due(tmp_path: Path) -> None:
    uri = _indexed(tmp_path)
    lance.write_dataset(pa.table({"id": pa.array(range(200, 260), pa.int64())}), uri, mode="append")

    assert due_indices(lance.dataset(uri)) == [IndexDue(name="id_btree", unindexed_rows=60, deltas=1)]


def test_rows_deleted_from_an_unindexed_fragment_are_not_left_unindexed(tmp_path: Path) -> None:
    """A fragment's live rows are its physical rows less those its deletion file records, as ``stats.index_stats`` counts
    them: the test asks it as the oracle, which maintenance never does."""
    uri = _indexed(tmp_path)
    lance.write_dataset(pa.table({"id": pa.array(range(200, 260), pa.int64())}), uri, mode="append")
    lance.dataset(uri).delete("id >= 250")
    ds = lance.dataset(uri)

    due = due_indices(ds)

    assert (due, ds.stats.index_stats("id_btree")["num_unindexed_rows"]) == ([IndexDue(name="id_btree", unindexed_rows=50, deltas=1)], 50)


def _vectors(rows: int, *, seed: int) -> pa.FixedSizeListArray:
    return pa.FixedSizeListArray.from_arrays(pa.array(np.random.default_rng(seed).random(rows * 8, dtype=np.float32)), 8)


def test_a_vector_index_a_plain_optimize_only_appends_to_is_due_naming_the_merge(tmp_path: Path) -> None:
    """[[LH-562]], measured on pylance 13.0.0: ``optimize_indices()``, the call ``lance_docs`` tells a writer to
    make after an append, covers the new rows of an IVF_FLAT index with one more delta segment each round, so the index
    stays due until its optimize merges; the record names that merge in Lance's own parameter."""
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"v": _vectors(512, seed=0)}), uri)
    lance.dataset(uri).create_index("v", "IVF_FLAT", name="v_ivf", num_partitions=4)
    for round_ in range(1, 6):
        lance.write_dataset(pa.table({"v": _vectors(64, seed=round_)}), uri, mode="append")
        lance.dataset(uri).optimize.optimize_indices()

    assert due_indices(lance.dataset(uri)) == [IndexDue(name="v_ivf", unindexed_rows=0, deltas=5, num_indices_to_merge=5)]


def test_a_covering_index_is_not_due(tmp_path: Path) -> None:
    assert due_indices(lance.dataset(_indexed(tmp_path))) == []
