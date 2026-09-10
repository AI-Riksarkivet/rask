"""A dataset the orphan method does not APPLY to is not a half-finished scan.

the lakehouse register, row H10 (drained 2026-09-10; in git history).

MEASURED LIVE 2026-09-08, and the constancy is what gives it away — § H2 recorded the middle number a
day earlier:

    2026-09-07   total 611   incomplete 490
    2026-09-08   total 615   incomplete 490     orphan_files 602

`incomplete` is IDENTICAL across a day in which `total` moved. Classifying the 490 by reason shape on
the same estate says why:

    419   unsupported manifest reader feature flags: 16 (base_paths (shallow clone / multi-base))
     70   depth limit reached at <prefix> — datasets under it were not scanned
      1   a dataset that could not be opened

The 419 are refusals by SHAPE. `_unscannable_reason` is right to refuse them — subtract-the-referenced-
set would name a live clone's files as garbage, and the reclaimer would delete them — but the refusal
is a permanent property of the manifest, so no later tick clears it. `purge.report_is_clean` blocks on
``report.incomplete``, so the gate could never open on this estate no matter how clean it got. Trash
accumulates forever and nothing is red.

THE DISTINCTION IS ONE THE CODE ALREADY MAKES ONE LAYER UP. `CategorySkipped.coverage_gap` separates
"the rule does not apply here" from "the rule applies and we chose not to run it", precisely so a skip
of the first kind cannot block the purge forever. An incomplete scan needs the same split:

  * **STRUCTURAL** — an unsupported feature flag, a shallow clone, a branch, a committed overlay. The
    method does not apply. Nothing was missed, so it must not block.
  * **FAILED** — an unreadable manifest, a listing that raised, a version ceiling. The method DOES
    apply and could not be run. That is a partial answer and must keep blocking, which is the whole
    reason `report_is_clean` has the arm.

The 70 depth-limit notes stay in `incomplete` on purpose: a dataset nested below the walk's bound was
never opened, which is a real coverage gap and a real answer the report owes.

`report_is_clean` is deliberately UNCHANGED. What changes is that a correct exclusion stops being
reported as a partial scan. The per-refusal coverage — that every arm which refuses by shape sets
`structural` — lives beside those refusals in `tests/unit/test_orphan_files.py`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from maintenance.core.config import MaintenanceSettings
from maintenance.services import reconcile as reconcile_mod
from maintenance.services.optimize import Discovery
from maintenance.services.orphans import OrphanReport
from maintenance.services.purge import report_is_clean
from maintenance.services.reconcile import IncompleteScan, ReconcileReport


class _NoBuckets:
    """The S3 read half, so the store categories complete and only the orphan seam is under test."""

    def list_buckets(self) -> dict[str, Any]:
        return {"Buckets": []}


def _report() -> ReconcileReport:
    return ReconcileReport(checked_at="2026-09-08T00:00:00+00:00")


def test_the_purge_gate_opens_on_an_estate_whose_only_finding_is_an_exclusion() -> None:
    """The closure of § H10, at the level the finding was measured at.

    `report_is_clean` is untouched by the fix, so this passes only because exclusions no longer reach
    `incomplete`. Written against the gate rather than the field so it stays red if a later change
    routes them back.
    """
    report = _report()
    report.excluded_datasets = [f"s3://b/t{i}.lance: base_paths (shallow clone / multi-base)" for i in range(419)]

    assert report_is_clean(report) is None, "419 correct exclusions must not keep the purge unreachable"


def test_a_scan_that_TRIED_and_failed_still_closes_the_gate() -> None:
    """The other half, and the one that makes the test above mean something: without it, "the gate
    opens" would be satisfied equally well by deleting the gate."""
    report = _report()
    report.incomplete = [IncompleteScan(source="storage:datasets", reason="s3://b/t.lance: manifest unreadable")]

    blocked = report_is_clean(report)
    assert blocked is not None and "INCOMPLETE" in blocked


def test_the_depth_limit_still_closes_the_gate() -> None:
    """70 of the estate's 490 notes are this, and they are NOT exclusions — a dataset below the walk's
    bound was never opened, so the report genuinely cannot certify that part of the file layer."""
    report = _report()
    report.incomplete = [IncompleteScan(source="storage:b", reason="depth limit reached at s3://b/deep/ — datasets under it were not scanned")]

    assert report_is_clean(report) is not None, "a real coverage gap must keep blocking"


def test_the_scan_s_exclusions_reach_their_own_field_and_no_other(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wiring between the two halves, which nothing else covers: delete the one assignment in
    `_orphan_category` and every test above still passes.

    `counts` is asserted on as well because it is the tempting place to put this and the wrong one —
    it feeds `report.total` and `report_is_clean`'s "drifting" message, so an exclusion there would be
    reported as drift.
    """
    excluded = ["s3://b/t.lance: base_paths (shallow clone / multi-base)"]
    # Both seams stubbed so the test is hermetic: unstubbed, discovery reaches a real object store and
    # its failure lands in `incomplete`, which is the very field under assertion.
    monkeypatch.setattr(reconcile_mod, "discover_datasets", lambda *_a, **_kw: Discovery(uris=["s3://b/t.lance"]))
    monkeypatch.setattr(
        reconcile_mod,
        "scan_datasets",
        lambda *_a, **_kw: OrphanReport(datasets_excluded=len(excluded), excluded=list(excluded)),
    )
    settings = MaintenanceSettings(
        s3_bucket="lance-catalog",
        s3_access_key_id="unit",
        s3_secret_access_key=SecretStr("unit"),
        orphan_scan_enabled=True,
    )
    (tmp_path / "control").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)

    report = asyncio.run(
        reconcile_mod.reconcile(
            settings,
            None,
            warehouses_enabled=False,
            control_root=f"file://{tmp_path / 'control'}",
            namespace_root=f"file://{tmp_path / 'data'}",
            bucket_client=_NoBuckets(),
        )
    )

    assert report.excluded_datasets == excluded
    assert not report.incomplete, f"an exclusion must not be reported as a partial scan: {[n.reason for n in report.incomplete]}"
    assert "excluded" not in report.counts and "excluded_datasets" not in report.counts
