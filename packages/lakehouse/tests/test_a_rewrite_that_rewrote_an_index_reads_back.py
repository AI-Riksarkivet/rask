"""A compaction that rewrote an index reads back through the lakehouse's transaction seam ([[LH-595]]).

pylance 14.0.3 raises ``TypeError`` from ``LanceDataset.read_transaction`` on such a version (lance-format/lance#11117),
and the catalog judges and announces every version from its transaction. Driven on real pylance: a stable-row-id table
with a BTREE index, compacted, which is the commit that rewrites one (measured 2026-11-15; BITMAP, LABEL_LIST and
INVERTED rewrite none).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest
from lance import LanceOperation

from lakehouse.lancekit import transactions
from lakehouse.lancekit.transactions import read_transaction


@pytest.fixture
def compacted(tmp_path: Path) -> lance.LanceDataset:
    """A stable-row-id table of ten small fragments with a BTREE index, compacted into one."""
    root = str(tmp_path / "t.lance")
    table = pa.table({"id": pa.array(range(100), pa.int64())})
    lance.write_dataset(table, root, data_storage_version="2.2", enable_stable_row_ids=True, max_rows_per_file=10).create_scalar_index("id", "BTREE")
    lance.dataset(root).optimize.compact_files(target_rows_per_fragment=1000)
    return lance.dataset(root)


def test_a_rewrite_that_rewrote_an_index_reads_back_with_its_groups_and_both_index_ids(compacted: lance.LanceDataset) -> None:
    before = {str(index["uuid"]) for index in compacted.checkout_version(compacted.version - 1).list_indices()}
    after = {str(index["uuid"]) for index in compacted.list_indices()}

    rewrite = read_transaction(compacted, compacted.version).operation

    assert type(rewrite).__name__ == "Rewrite"
    assert [(len(group.old_fragments), len(group.new_fragments)) for group in rewrite.groups] == [(10, 1)]
    [rewritten] = rewrite.rewritten_indices
    assert ({str(rewritten.old_id)}, {str(rewritten.new_id)}) == (before, after)
    assert before != after


@pytest.mark.xfail(
    strict=True,
    raises=TypeError,
    reason="lance-format/lance#11117: fixed by the release that carries #11118; then delete lakehouse.lancekit.transactions' stand-in",
)
def test_pylance_itself_reads_a_rewrite_that_rewrote_an_index(compacted: lance.LanceDataset, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LanceOperation, "RewrittenIndex", transactions._REWRITTEN_INDEX.__mro__[1])

    read: Any = compacted.read_transaction(compacted.version)

    assert read.operation.rewritten_indices
