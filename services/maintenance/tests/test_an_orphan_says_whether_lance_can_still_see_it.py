"""An orphan Lance will reclaim itself and one Lance can never enumerate are different findings.

THE GATE IS ONLY DEFENSIBLE IF THE TWO ARE TOLD APART. `purge.py::report_is_clean` counts
`orphan_files` in `report.total`, so the trash purge cannot run while any orphan is reported — and most
orphans clear themselves. Measured on the deployed estate 2026-09-18: the count went 1011 -> 32 inside
ten minutes as the ordinary sweep reclaimed 979 files at the +7-day anniversary of the writes that
produced them. Blocking the purge on a number that falls on its own is a control that fires for a week
and then stops meaning anything; blocking it on the residue that never falls is the real signal.

WHAT DECIDES WHICH DEPENDS ON THE DIRECTORY. `cleanup_old_versions` clamps its listing of `_versions/`,
`_transactions/` and `_deletions/` to the commit timestamp of the EARLIEST RETAINED MANIFEST, before the
7-day unverified rule filters what was listed (lance v13.0.0 `rust/lance/src/dataset/cleanup.rs:756-778`,
`CleanupInspection::listing_unmodified_since` at :337-357). A transaction or deletion file newer than
that manifest is never listed, so no `older_than` and no `delete_unverified=True` can reach it. `data/`
is listed through `max(floor, now - 7 days)`, or in full with `delete_unverified=True` (lance #8944), and
`_indices/` in full, so an unreferenced data or index file is Lance's to reclaim wherever it sits.

MEASURED HERE rather than read: with `delete_unverified=True` bypassing the age filter, a transaction
file's flip is at the manifest's own timestamp to the second (pylance 11.0.0: offsets -3600/-60/-1/0 s
REMOVED, +1/+60/+3600 s KEPT), while a data file above the floor is removed on pylance 13.0.0 (the second
test below). That is why a dataset collapsed to ONE live version can hold transaction and deletion
orphans forever, and its data-file orphans only until they are seven days old.

THE UNIT TRAP, measured on this host: `versions()[i]["timestamp"]` is NAIVE LOCAL
(`2026-09-19 00:04:01`) while `pyarrow.fs.FileInfo.mtime` is TZ-AWARE UTC (`2026-09-18 22:04:01+00:00`)
— the same instant, two hours apart as written. Comparing the datetimes directly raises or inverts by
the host's offset, so both sides go through `.timestamp()`.
"""

from __future__ import annotations

import datetime as dt
import os
import tempfile
import time
from pathlib import Path

import lance
import pyarrow as pa
import pyarrow.fs as pafs

from maintenance.services.orphans import scan_dataset
from service_kit.lancekit.versions import committed_at


def _dataset_with_orphan(offset_s: float, rel: str = "_transactions/99-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.txn") -> tuple[str, str]:
    """A one-version dataset plus an unreferenced file at ``rel``, ``offset_s`` from its manifest's commit."""
    tmp = Path(tempfile.mkdtemp())
    uri = tmp / "t.lance"
    lance.write_dataset(pa.table({"id": pa.array([1], pa.int64())}), str(uri), mode="create")
    floor = committed_at(lance.dataset(str(uri)).versions()[0]).timestamp()
    orphan = uri / rel
    orphan.parent.mkdir(parents=True, exist_ok=True)
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


def test_a_data_file_ABOVE_the_floor_is_still_lance_s_to_reclaim() -> None:
    """lance #8944: `data/` is listed past the floor, so time alone clears an unreferenced data file.

    The scan's answer is checked against Lance itself: the same file is removed by a cleanup that
    bypasses the age filter, which a transaction or deletion file above the floor survives.
    """
    uri, prefix = _dataset_with_orphan(0.5, rel="data/stranded.lance")
    orphan = Path(uri) / "data" / "stranded.lance"
    # Above the floor and in the past by the time cleanup runs, the estate's shape: an object rewritten
    # after the commit that last referenced it. A future mtime stays unlisted for an unrelated reason.
    time.sleep(1.5)

    scan = scan_dataset(pafs.LocalFileSystem(), uri, prefix=prefix)

    assert [(o.path, o.kind, o.reclaimable_by_lance) for o in scan.orphans] == [("data/stranded.lance", "data", True)]
    lance.dataset(uri).cleanup_old_versions(older_than=dt.timedelta(seconds=0), delete_unverified=True)
    assert not orphan.exists(), "Lance kept a data file above the floor, so the classification is wrong for this pylance"
