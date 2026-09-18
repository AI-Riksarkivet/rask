"""An operator must be able to see which orphans will clear themselves and which never will.

The classification is only worth computing if it reaches a reader. `orphan_files: 32` says nothing
about whether the estate has a problem or a clock: measured 2026-09-18, the count fell 1011 -> 32 in
ten minutes as the ordinary sweep reclaimed 979 files at the +7-day anniversary of their writes, so the
same number means "wait" on one tick and "act" on another.

The split is `OrphanFile.reclaimable_by_lance`, decided by the LISTING FLOOR: `cleanup_old_versions`
clamps its unreferenced-file listing to the earliest retained manifest's commit timestamp
(`rust/lance/src/dataset/cleanup.rs:332-341`) before the 7-day rule filters what was listed, so a file
above that instant is unreachable at any `older_than` and with `delete_unverified=True`.

`unknown` is its own bucket rather than folded into either. A floor that could not be read is "we could
not tell", and reporting it as "Lance will take it" is the same conflation `checked=False` versus
`orphans=[]` exists to prevent, on the category that decides whether the trash purge may run.
"""

from __future__ import annotations

from maintenance.api.routes import _orphans_by_reclaimability
from maintenance.services.orphans import OrphanFile
from maintenance.services.reconcile import ReconcileReport


def _empty() -> ReconcileReport:
    """`checked_at` is required, and that is the report saying WHEN it looked — not a field to default.

    It is a STRING on this model (an ISO stamp), which a `datetime` does not satisfy.
    """
    return ReconcileReport(checked_at="2026-09-19T00:00:00Z")


def _report(*flags: bool | None) -> ReconcileReport:
    report = _empty()
    report.orphan_files = [
        OrphanFile(dataset=f"s3://b/d{i}", path=f"_transactions/{i}.txn", kind="transactions", reclaimable_by_lance=flag) for i, flag in enumerate(flags)
    ]
    return report


def test_the_three_classes_are_counted_separately() -> None:
    """Two of them mean opposite things to an operator and the third means neither."""
    assert _orphans_by_reclaimability(_report(True, True, False, None)) == {
        "lance_will_reclaim": 2,
        "beyond_lance_listing_floor": 1,
        "unknown": 1,
    }


def test_a_tick_with_no_orphans_reports_nothing() -> None:
    """An empty key on a clean tick reads like a category nobody looked at — the same rule
    `orphans_by_dataset` already follows."""
    assert _orphans_by_reclaimability(_empty()) == {}


def test_the_counts_reconcile_with_the_total() -> None:
    """A reader must be able to check the split against `counts["orphan_files"]` rather than trust it."""
    report = _report(True, False, None, False, True)

    assert sum(_orphans_by_reclaimability(report).values()) == len(report.orphan_files)
