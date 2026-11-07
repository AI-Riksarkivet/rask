"""Which indices an optimize is due on: the index half of a table's due record (ADR 0162).

Maintenance optimizes no index; it names the indices a bring-your-own job should optimize. An index is due when it
leaves rows unindexed — queries touching them fall back to a flat scan — or carries more delta segments than a query
should fan out over. An index whose statistics cannot be read is not due: nothing an optimize is known to fix.

MEASURED, not assumed. pylance 9.0.0's `stats.index_stats(name)` exposes `num_unindexed_rows` and `num_indices`;
`describe_indices()` gives `name`, `index_type` and `field_names`.
"""

from __future__ import annotations

from pathlib import Path

import lance
import numpy as np
import pyarrow as pa
import pytest

from lakehouse.maintenance_due import IndexDue
from maintenance.services import index_health
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


def test_a_JSON_index_is_never_asked_for_its_stats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`index_stats` on a JSON index panics in Lance (`scalar/json.rs:95`, a `todo!()`), and a pyo3 panic writes a Rust
    backtrace to stderr every tick. `describe_indices` already reports the type, so the call is skipped and the index,
    whose health is unknown, is not listed as due."""
    uri = _indexed(tmp_path)

    class _JsonIndex:
        name = "lineage_run_id_idx"
        field_names = ("lineage",)
        index_type = "Json"

    monkeypatch.setattr(lance.LanceDataset, "describe_indices", lambda self: [_JsonIndex()], raising=False)
    called: list[str] = []
    monkeypatch.setattr(index_health, "_stats", lambda ds, name: called.append(name))

    assert due_indices(lance.dataset(uri)) == []
    assert called == [], f"stats must not be asked of a JSON index — that is the panic: {called}"
