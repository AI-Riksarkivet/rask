"""An orphan Lance will reclaim itself and one Lance can never enumerate are different findings.

THE GATE IS ONLY DEFENSIBLE IF THE TWO ARE TOLD APART. `purge.py::report_is_clean` counts
`orphan_files` in `report.total`, so the trash purge cannot run while any orphan is reported — and most
orphans clear themselves. Measured on the deployed estate 2026-09-18: the count went 1011 -> 32 inside
ten minutes as the ordinary sweep reclaimed 979 files at the +7-day anniversary of the writes that
produced them. Blocking the purge on a number that falls on its own is a control that fires for a week
and then stops meaning anything; blocking it on the residue that never falls is the real signal.

WHAT DECIDES WHICH: **the listing floor, not the 7-day rule.** Before any age test applies,
`cleanup_old_versions` clamps its unreferenced-file listing to the commit timestamp of the EARLIEST
RETAINED MANIFEST (lance v11.0.0 `rust/lance/src/dataset/cleanup.rs:332-341`, applied to `_versions/`,
`_transactions/`, `data/` and `_deletions/` at :721-731). A file newer than that manifest is never
listed, so no `older_than` and no `delete_unverified=True` can reach it. The 7-day rule
(`UNVERIFIED_THRESHOLD_DAYS = 7`, :345) is a LATER filter over files that were already listed.

MEASURED HERE rather than read: with `delete_unverified=True` bypassing the age filter, the flip is at
the manifest's own timestamp to the second — offsets -3600/-60/-1/0 s are REMOVED and +1/+60/+3600 s are
KEPT. That is why a dataset collapsed to ONE live version can hold orphans forever: every file it wrote
after that surviving commit is above the floor, and nothing Lance offers will enumerate it.

THE UNIT TRAP, measured on this host: `versions()[i]["timestamp"]` is NAIVE LOCAL
(`2026-09-19 00:04:01`) while `pyarrow.fs.FileInfo.mtime` is TZ-AWARE UTC (`2026-09-18 22:04:01+00:00`)
— the same instant, two hours apart as written. Comparing the datetimes directly raises or inverts by
the host's offset, so both sides go through `.timestamp()`.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from pathlib import Path

import lance
import pyarrow as pa
import pyarrow.fs as pafs

from maintenance.services.orphans import scan_dataset
from service_kit.lancekit.versions import committed_at


def _dataset_with_orphan(offset_s: float) -> tuple[str, str]:
    """A one-version dataset plus an unreferenced txn at ``offset_s`` from its manifest's commit."""
    tmp = Path(tempfile.mkdtemp())
    uri = tmp / "t.lance"
    lance.write_dataset(pa.table({"id": pa.array([1], pa.int64())}), str(uri), mode="create")
    floor = committed_at(lance.dataset(str(uri)).versions()[0]).timestamp()
    orphan = uri / "_transactions" / "99-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.txn"
    orphan.write_bytes(b"leftover from a commit that never landed")
    os.utime(orphan, (floor + offset_s, floor + offset_s))
    return str(uri), str(uri)


def test_an_orphan_BELOW_the_floor_is_lance_s_to_reclaim() -> None:
    """Lance lists it, so the 7-day rule is the only thing holding it and time alone clears it."""
    uri, prefix = _dataset_with_orphan(-60)

    scan = scan_dataset(pafs.LocalFileSystem(), uri, prefix=prefix)

    assert scan.checked
    assert len(scan.orphans) == 1
    assert scan.orphans[0].reclaimable_by_lance is True


def test_an_orphan_ABOVE_the_floor_is_reclaimable_by_NOTHING() -> None:
    """The residue that justifies a reclaimer: no `older_than` and no flag will ever enumerate it."""
    uri, prefix = _dataset_with_orphan(+60)

    scan = scan_dataset(pafs.LocalFileSystem(), uri, prefix=prefix)

    assert scan.checked
    assert len(scan.orphans) == 1
    assert scan.orphans[0].reclaimable_by_lance is False


def test_the_classification_survives_a_host_that_is_not_UTC() -> None:
    """The trap this test exists for: the two clocks are reported in different frames.

    A naive-local manifest timestamp compared against an aware-UTC file mtime is wrong by the host
    offset, which on this host is two hours — enough to flip every orphan within two hours of a commit
    into the wrong class, silently and in only one direction.
    """
    uri, prefix = _dataset_with_orphan(-60)
    version_ts = lance.dataset(uri).versions()[0]["timestamp"]
    info = pafs.LocalFileSystem().get_file_info(f"{prefix}/_transactions")

    assert isinstance(version_ts, datetime)
    assert version_ts.tzinfo is None, "the manifest timestamp gained a tzinfo — re-derive the comparison"
    assert info.mtime.tzinfo is not None, "FileInfo.mtime lost its tzinfo — re-derive the comparison"
    assert scan_dataset(pafs.LocalFileSystem(), uri, prefix=prefix).orphans[0].reclaimable_by_lance is True
