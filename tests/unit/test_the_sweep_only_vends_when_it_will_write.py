"""A 900-second write credential must not be minted for a dataset that writes nothing (LH-134).

MEASURED, which is why this exists: the store accumulates one STS identity record per vend and prunes
none — 280 per minute on the live estate, and the previous store became unable to restart at 107,485
of them (its IAM bootstrap walks the whole prefix inside a 5-second disk timeout and then gives up
permanently). The sweep vended `tier=write` for every PLANNED dataset, before anything had decided the
dataset needed rewriting, against an estate whose own history is that almost nothing is ever rewritten
(`fragments_removed_total=0` across 785 ticks).

`credentials.py` argues NO CACHE because maintenance "vends once per WORK ITEM". That reasoning is
sound and this restores its premise rather than replacing it: a unit that will not write is not a work
item.

THE CHECK IS CONSERVATIVE BY CONSTRUCTION, and that direction is the whole safety argument. Skipping a
vend for a dataset that WOULD have been maintained means a dataset silently stops being maintained —
far worse than a wasted credential. So every uncertain answer, including an unreadable dataset, must
say "may write".
"""

from __future__ import annotations

from pathlib import Path

import lance
import pyarrow as pa

from maintenance.services.sweep import _may_write_anything


def _dataset(tmp: Path, name: str, *, fragments: int = 1) -> str:
    uri = str(tmp / name)
    for i in range(fragments):
        lance.write_dataset(
            pa.table({"id": pa.array([i], pa.int64())}),
            uri,
            mode="overwrite" if i == 0 else "append",
            enable_stable_row_ids=True,
        )
    return uri


def test_a_single_fragment_dataset_with_nothing_to_reclaim_needs_no_credential(tmp_path: Path) -> None:
    """The common case on this estate, and the one paying for 280 vends a minute."""
    uri = _dataset(tmp_path, "quiet", fragments=1)

    assert _may_write_anything(uri, {}, cleanup_enabled=False, optimize_indices_enabled=False) is False


def test_more_than_one_fragment_may_compact_so_it_vends(tmp_path: Path) -> None:
    """Compaction merges fragments; two of them is work that can happen."""
    uri = _dataset(tmp_path, "fragmented", fragments=3)

    assert _may_write_anything(uri, {}, cleanup_enabled=False, optimize_indices_enabled=False) is True


def test_cleanup_enabled_on_a_dataset_with_history_may_reclaim_so_it_vends(tmp_path: Path) -> None:
    """Reclamation is a write, and a dataset with superseded versions has something to reclaim."""
    uri = _dataset(tmp_path, "versioned", fragments=2)

    assert _may_write_anything(uri, {}, cleanup_enabled=True, optimize_indices_enabled=False) is True


def test_an_unreadable_dataset_says_MAY_WRITE(tmp_path: Path) -> None:
    """The load-bearing direction: uncertainty must never silently stop maintaining a dataset.

    A wasted credential is a cost; a dataset that quietly stops being compacted and reclaimed is the
    failure this whole service exists to prevent.
    """
    assert _may_write_anything(str(tmp_path / "does-not-exist"), {}, cleanup_enabled=True, optimize_indices_enabled=True) is True
