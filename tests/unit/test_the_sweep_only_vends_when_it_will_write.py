"""A 900-second write credential must not be minted for a dataset that writes nothing (LH-134).

MEASURED, which is why this exists: the store accumulates one STS identity record per vend and prunes
none — 280 per minute on the live estate, and the previous store became unable to restart at 107,485
of them (its IAM bootstrap walks the whole prefix inside a 5-second disk timeout and then gives up
permanently). The sweep vended `tier=write` for every PLANNED dataset, before anything had decided the
dataset needed rewriting, against an estate whose own history is that almost nothing is ever rewritten
(`fragments_removed_total=0` across 785 ticks).

`credentials.py` argues NO CACHE because maintenance vends once per dataset that has something to expire, and
this check is what makes that true: a dataset expiry will not write to is not vended for.

THE CHECK IS CONSERVATIVE BY CONSTRUCTION, and that direction is the whole safety argument. Skipping a
vend for a dataset that WOULD have been maintained means a dataset silently stops being maintained —
far worse than a wasted credential. So every uncertain answer, including an unreadable dataset, must
say "may write".

The probe carries a second answer now — the dataset's declared id, read off the same open ([[LH-141]]).
That half is asserted in `test_the_sweep_vends_for_the_dataset_it_is_holding`; this module stays about
the saving, so a change to either question fails the file that argues for it.

A file no version references in the table's own data directory is a write too ([[LH-286]]): Lance's cleanup leaves
it, the pass deletes it once it is past Lance's unverified-file age, and a delete signed by anything but the table's
vend is the ambient key acting on tenant bytes. A table overwritten once and never written again has no reclaimable
version left to ask for a vend, so the stranded file must ask on its own.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import lance
import pyarrow as pa
import pytest

from maintenance.services.expiry import ExpiryPolicy
from maintenance.services.sweep import _probe_before_vending
from service_kit.lakehouse.base_registry import BaseOrigin, BaseRole, RecordedBase
from service_kit.lakehouse.objectfs import StorageOptions


#: Keep the newest version, so every superseded one is reclaimable.
KEEP_ONE = ExpiryPolicy(older_than=None, retain_versions=1)


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


def test_cleanup_enabled_on_a_dataset_with_history_may_reclaim_so_it_vends(tmp_path: Path) -> None:
    """Reclamation is a write, and a dataset with superseded versions has something to reclaim."""
    uri = _dataset(tmp_path, "versioned", fragments=2)

    assert _probe_before_vending(uri, StorageOptions(), policy=KEEP_ONE, directories=[], now=datetime.now(UTC)).may_write is True


def test_an_unreadable_dataset_says_MAY_WRITE(tmp_path: Path) -> None:
    """The load-bearing direction: uncertainty must never silently stop maintaining a dataset.

    A wasted credential is a cost; a dataset that quietly stops being compacted and reclaimed is the
    failure this whole service exists to prevent.
    """
    assert _probe_before_vending(str(tmp_path / "does-not-exist"), StorageOptions(), policy=KEEP_ONE, directories=[], now=datetime.now(UTC)).may_write is True


@pytest.mark.parametrize(("days", "vends"), [pytest.param(0, False, id="younger-than-the-age"), pytest.param(8, True, id="past-the-age")])
def test_a_base_file_no_version_references_vends_once_it_is_past_the_age(tmp_path: Path, days: int, vends: bool) -> None:
    uri = str(tmp_path / "table")
    directory = str(tmp_path / "base" / "data" / "dir")
    rows = pa.table({"id": pa.array([1], pa.int64())})
    ds = lance.write_dataset(
        rows, uri, enable_stable_row_ids=True, initial_bases=[lance.DatasetBasePath(directory, is_dataset_root=False, name="data")], target_bases=["data"]
    )
    lance.write_dataset(rows, ds, mode="overwrite", target_bases=["data"])
    lance.dataset(uri).cleanup_old_versions(older_than=timedelta(0))
    assert len(lance.dataset(uri).versions()) == 1, "precondition: no version is left to reclaim, only the base file the overwrite stranded"
    recorded = RecordedBase(path=directory, role=BaseRole.DATA, origin=BaseOrigin.CREATE)

    probe = _probe_before_vending(uri, StorageOptions(), policy=KEEP_ONE, directories=[recorded], now=datetime.now(UTC) + timedelta(days=days))

    assert probe.may_write is vends
