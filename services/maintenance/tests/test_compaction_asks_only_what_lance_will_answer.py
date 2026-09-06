"""Compaction does not ask for a deferred index remap on a dataset that cannot take one.

`defer_index_remap` needs a fragment-reuse layout. Lance refuses it in BOTH directions and says so:

    no stable row ids  -> "defer_index_remap requires row_addrs but none were provided"
    stable row ids     -> "defer_index_remap=true is not supported on datasets with stable row IDs:
                           stable row IDs do not require index remapping during compaction, so there
                           is nothing to defer."

The estate already measured the cost of MISSING the second one — a live sweep reporting
`datasets 31, fragments_removed 0, errors 11`, every error this, because the cascade writes with
stable row ids and so produced exactly the datasets compaction could never touch.

The recovery it grew is a try/except that matches on the parameter name and retries plain. That is
correct and it works. It is also asking a question whose answer the dataset already carries:
`ds.has_stable_row_ids` is a public accessor (`ingest/catalog.py::_has_stable_row_ids` already reads
it), so the refusal is predictable rather than discoverable. Measured on the deployed estate:
211 `compact_defer_index_remap_unsupported` warnings in 48 hours, one per governed dataset per tick,
for a call that could not have succeeded.

Branching keeps the except in place — a THIRD refusal reason would still be caught rather than
crashing a tick — while stopping the estate from asking for something it knows will be refused.
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa
import pytest

from maintenance.services.optimize import compact_one


def _dataset(tmp: Path, *, stable: bool) -> str:
    uri = str(tmp / ("stable.lance" if stable else "plain.lance"))
    table = pa.table({"id": pa.array(range(64), pa.int64()), "v": pa.array([f"x{i}" for i in range(64)])})
    for start in (0, 16, 32, 48):
        lance.write_dataset(
            table.slice(start, 16),
            uri,
            mode="create" if start == 0 else "append",
            data_storage_version="2.2",
            **({"enable_stable_row_ids": True} if stable and start == 0 else {}),
        )
    return uri


@pytest.mark.parametrize("stable", [True, False])
def test_compaction_still_compacts_either_way(tmp_path: Path, stable: bool) -> None:
    """The branch must not change the OUTCOME — only the call that gets made."""
    uri = _dataset(tmp_path, stable=stable)
    before = len(lance.dataset(uri).get_fragments())
    compact_one(uri, {}, None, target_rows_per_fragment=32, cleanup_enabled=False, optimize_indices_enabled=False)
    after = len(lance.dataset(uri).get_fragments())
    assert after < before, f"compaction stopped reducing fragments ({before} -> {after}) with stable={stable}"


def test_a_stable_row_id_dataset_is_not_asked_for_a_deferred_remap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: no wasted call, no warning, on every governed dataset in the estate."""
    uri = _dataset(tmp_path, stable=True)
    asked: list[bool] = []
    optimizer = lance.dataset(uri).optimize.__class__

    def _spy(self: object, *args: object, **kwargs: object) -> None:
        """Records the call and performs none: this test is about WHICH call is made, and letting the
        real compaction run would make it about Lance's behaviour instead."""
        asked.append("defer_index_remap" in kwargs)

    monkeypatch.setattr(optimizer, "compact_files", _spy)
    compact_one(uri, {}, None, target_rows_per_fragment=32, cleanup_enabled=False, optimize_indices_enabled=False)

    assert asked, "compact_files was never called"
    assert not any(asked), (
        "a deferred index remap was requested on a stable-row-id dataset — Lance refuses it by "
        "definition, so this is one guaranteed-failed call and one warning per governed dataset per tick"
    )
