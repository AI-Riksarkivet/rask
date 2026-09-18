"""The purge waits on the orphans that will never clear, not on the ones a clock clears.

`report_is_clean` counts `orphan_files` in `report.total`, so the trash purge cannot run while any
orphan is reported. Measured 2026-09-18, that number fell 1011 -> 32 in ten minutes as the ordinary
sweep reclaimed 979 files at the +7-day anniversary of the writes that made them. A gate that blocks on
the 979 blocks for a week and then releases on its own — which is not a control, it is a delay — while
the 32 are the real finding and would have been invisible behind them.

THE SPLIT IS THE LISTING FLOOR: `cleanup_old_versions` clamps its unreferenced-file listing to the
earliest retained manifest's commit timestamp (`rust/lance/src/dataset/cleanup.rs:332-341`) before the
7-day rule (:345) filters what was listed, so a file above that instant is unreachable at any
`older_than` and with `delete_unverified=True`. Observed on release 181, the deployed estate answers
`{'lance_will_reclaim': 0, 'beyond_lance_listing_floor': 32, 'unknown': 0}` — so today the narrowing
changes no verdict, and that is exactly when it should land rather than under the pressure of a number
someone wants to go green.

UNKNOWN GATES. A floor that could not be read is "we could not tell", and the purge deletes bytes on the
strength of this report — the same rule that makes an UNAVAILABLE category block rather than pass.
"""

from __future__ import annotations

from maintenance.services.orphans import OrphanFile
from maintenance.services.purge import report_is_clean
from maintenance.services.reconcile import NON_GATING_CATEGORIES, ReconcileReport


def _report(*flags: bool | None) -> ReconcileReport:
    """A report shaped the way `reconcile` shapes one — the SAME arithmetic, not a restatement of it.

    `total` and `orphan_files_blocking` are derived here by the production rule rather than asserted
    into place: a fixture that computes the gating number itself would pass whatever the gate did.
    """
    report = ReconcileReport(checked_at="2026-09-19T00:00:00Z")
    report.orphan_files = [
        OrphanFile(dataset=f"s3://b/d{i}", path=f"_transactions/{i}.txn", kind="transactions", reclaimable_by_lance=flag) for i, flag in enumerate(flags)
    ]
    report.counts["orphan_files"] = len(report.orphan_files)
    report.orphan_files_blocking = sum(1 for orphan in report.orphan_files if orphan.reclaimable_by_lance is not True)
    report.total = sum(count for name, count in {**report.counts, "orphan_files": report.orphan_files_blocking}.items() if name not in NON_GATING_CATEGORIES)
    return report


def test_orphans_lance_will_take_do_not_block_the_purge() -> None:
    """979 of these cleared themselves inside ten minutes. Waiting on them buys nothing."""
    assert report_is_clean(_report(True, True, True)) is None


def test_an_orphan_BEYOND_the_floor_blocks_it() -> None:
    """No `older_than`, `retain_versions` or `delete_unverified` will ever reclaim this one."""
    blocked = report_is_clean(_report(True, False, True))

    assert blocked is not None
    assert "orphan_files" in blocked
    assert "1 finding" in blocked, "the message counted the self-clearing orphans too"


def test_an_UNCLASSIFIED_orphan_blocks_it() -> None:
    """The purge deletes bytes on this report. "We could not tell" is not "nothing to see"."""
    blocked = report_is_clean(_report(True, None))

    assert blocked is not None
