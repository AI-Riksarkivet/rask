"""The estate's own backup snapshots must not be walked as if they held governed datasets.

[[LH-094]]. `_CONTROL_PREFIXES` skips the control-plane registries because "no dataset ever lives under
them, and probing them is wasted S3 round-trips on the hot discovery path". `_backups` — written by
`scripts/control_root_backup.py` as `_backups/control/<timestamp>/…` — is exactly that kind of
directory and was not in the list.

THE COST IS NOT THE ROUND TRIPS. A backup snapshot nests deeper than `discovery_max_depth`, so the walk
records an `IncompleteScan`; `report_is_clean` refuses to certify an estate with anything incomplete;
and the #79 purge is gated on `report_is_clean`. So the estate's own backups were blocking
reclamation. Measured on the deployed release 2026-09-16, once the incomplete list was made to say WHY:

    storage:lance-catalog: depth limit reached at s3://lance-catalog/_backups/control/20260914T163435Z
    storage:lance-catalog: depth limit reached at s3://lance-catalog/_backups/control/20260914T164046Z
    storage:lance-catalog: depth limit reached at s3://lance-catalog/_backups/control/20260914T164519Z

Three entries, all backups, against **932 orphan files across 8 datasets** the purge could not touch.

THE DEPTH BOUND ITSELF STAYS LOUD, which is the distinction this test has to keep. A governed dataset
nested too deep is a real coverage gap and must still be reported — the `truncated` field exists
because that case was once silent. What is not a coverage gap is failing to reach into a directory no
dataset may live in.
"""

from __future__ import annotations

from pathlib import Path

import pyarrow.fs as pafs

from maintenance.services.optimize import discover_datasets


def _tree(root: Path, *relative: str) -> None:
    for path in relative:
        (root / path).mkdir(parents=True, exist_ok=True)


def test_a_backup_snapshot_records_no_truncation(tmp_path: Path) -> None:
    """The consequence, driven: a snapshot nested past the bound must not produce an incomplete note."""
    bucket = tmp_path / "lance-catalog"
    _tree(bucket, "_backups/control/20260914T163435Z/registry/deep/deeper", "real_ns$tbl/_versions")

    found = discover_datasets(pafs.LocalFileSystem(), str(bucket), max_depth=2)

    assert not found.truncated, f"a backup snapshot was reported as unscanned coverage: {found.truncated}"
