"""932 orphan files across an unknown number of datasets is a number nobody can act on.

[[LH-094]]. Measured on the live estate 2026-09-16: the reconcile tick reports `orphan_files: 932` and
names ten of them. Ten file paths out of 932 identifies neither the scale of any one dataset's problem
nor how many datasets are involved, and the per-dataset counts the scan already computes
(`DatasetOrphanScan.orphans`) are logged nowhere on the success path — only `orphan_scan_skipped`,
`orphan_scan_unreadable` and `orphan_listing_failed` ever name a dataset.

THE DISTRIBUTION IS THE ACTIONABLE SHAPE, and this row is the proof. `orphan_files` went 0 -> 932 in a
single tick, and whether those are newly-VISIBLE (the discovery narrowing that took `incomplete` 65 ->
3 shipped in the same image) or newly-CREATED (the 0 was measured six days earlier, over an estate
that has run e2e traffic since) decides whether that is a fix working or a regression. One number
cannot tell them apart; "44 datasets, the largest holding 300" versus "one dataset holding 932" tells
them apart immediately.

BOUNDED LIKE `_drift_names`, and for the same reason: a drifting estate can carry thousands of
datasets and one WARNING must not become the report. Largest first, because that is the order an
operator works in, and the tail is COUNTED rather than dropped — a truncation that hides how much it
hid is the failure this whole summary exists to end.
"""

from __future__ import annotations

from maintenance.api.routes import _orphans_by_dataset
from maintenance.services.orphans import OrphanFile
from maintenance.services.reconcile import ReconcileReport


def _report(*files: OrphanFile) -> ReconcileReport:
    report = ReconcileReport(checked_at="2026-09-16T14:30:00+00:00")
    report.orphan_files = list(files)
    report.counts["orphan_files"] = len(files)
    return report


def _orphan(dataset: str, name: str) -> OrphanFile:
    return OrphanFile(dataset=dataset, path=f"data/{name}.lance", kind="data", size_bytes=1)


def test_a_clean_scan_reports_nothing() -> None:
    assert _orphans_by_dataset(_report()) == {}


def test_each_dataset_carries_its_own_count() -> None:
    report = _report(_orphan("s3://b/one", "a"), _orphan("s3://b/one", "b"), _orphan("s3://b/two", "c"))

    assert _orphans_by_dataset(report) == {"s3://b/one": 2, "s3://b/two": 1}


def test_the_largest_holder_comes_first() -> None:
    """An operator works the biggest one first, and a dict that arrives in discovery order buries it."""
    report = _report(_orphan("s3://b/small", "a"), *[_orphan("s3://b/big", str(i)) for i in range(5)])

    assert list(_orphans_by_dataset(report)) == ["s3://b/big", "s3://b/small"]


def test_a_long_tail_is_counted_rather_than_dropped() -> None:
    """The truncation says how much it hid. `orphan_files: 932` with ten names is exactly the failure
    this replaces; a bounded list that does not admit its bound repeats it one level up."""
    report = _report(*[_orphan(f"s3://b/ds{i:03}", "x") for i in range(40)])

    summarised = _orphans_by_dataset(report)

    assert len(summarised) <= 11, f"one WARNING became the report: {len(summarised)} entries"
    hidden = [k for k in summarised if "more" in k]
    assert hidden, f"40 datasets were summarised into {len(summarised)} entries with no note of the rest: {summarised}"
    assert summarised[hidden[0]] == 40 - (len(summarised) - 1), "the tail's own count is wrong, so the total cannot be reconstructed"


def test_the_total_survives_the_truncation() -> None:
    """Whatever is shown, the numbers must still add up to what `counts['orphan_files']` claims —
    otherwise the summary and the count disagree and a reader cannot tell which lied."""
    report = _report(*[_orphan(f"s3://b/ds{i:03}", "x") for i in range(40)])

    assert sum(_orphans_by_dataset(report).values()) == report.counts["orphan_files"]
