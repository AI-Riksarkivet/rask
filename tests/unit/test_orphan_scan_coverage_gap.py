"""#128a — a skip that means "we did not look" must not certify the estate.

THE DEFECT. `report_is_clean` is the gate the #79 purge waits on, and it treats every
`CategorySkipped` as harmless. Its docstring defends that:

    "the shipped configuration skips ``orphan_files`` … so treating a skip as drift would make the
    purge unreachable in every real deployment"

That was true, and the reasoning is CIRCULAR: it was only true because `orphan_scan_enabled` defaults
False AND the chart shipped no lever to turn it on. So the scan could never run anywhere, the skip was
permanent, and the purge reclaimed bytes on the strength of a report whose file layer was never
inspected. That is the difference between *we looked and it was clean* and *we didn't look* — and the
report printed the same thing for both.

THE TWO HALVES, AND WHY THEY LAND TOGETHER. Blocking on the skip without a lever would make the purge
unreachable, exactly as the docstring says. Adding the lever without blocking leaves the default
deployment certifying an estate it never scanned. Neither half is shippable alone.

NOT EVERY SKIP IS A GAP. `unbound_namespaces` is skipped when warehouses are off, and that is a rule
that genuinely does not apply — there is nothing to find, and blocking on it would be wrong. The
distinction is now carried as data (`CategorySkipped.coverage_gap`) rather than inferred from the
category name, so a future skip has to state which kind it is.
"""

from __future__ import annotations

from maintenance.services.purge import report_is_clean
from maintenance.services.reconcile import CategorySkipped, ReconcileReport


def _clean() -> ReconcileReport:
    """A report with nothing wrong: no findings, nothing unavailable, nothing partial."""
    return ReconcileReport(checked_at="2026-08-15T00:00:00Z")


def test_a_report_with_NOTHING_wrong_is_clean() -> None:
    """The control. Without this the other assertions could pass because the gate always blocks."""
    assert report_is_clean(_clean()) is None


def test_a_skip_that_means_WE_DID_NOT_LOOK_blocks_the_purge() -> None:
    """THE defect. The orphan scan being off is a coverage gap, not a clean bill of health.

    The purge deletes bytes. Letting it run on a report that never opened a dataset means reclaiming
    against a file layer nobody inspected — and the report said "clean" while meaning "unexamined".
    """
    report = _clean()
    report.skipped.append(CategorySkipped(category="orphan_files", reason="scan is off", coverage_gap=True))

    blocker = report_is_clean(report)

    assert blocker is not None, "a report that never scanned the file layer certified the estate"
    assert "orphan_files" in blocker, "the blocker must NAME the category nobody looked at"


def test_a_skip_that_means_THE_RULE_DOES_NOT_APPLY_still_passes() -> None:
    """The other half, and the reason this is a flag rather than a blanket rule.

    `unbound_namespaces` is skipped when warehouses are off: there are no bindings, so there is
    nothing to find and nothing was missed. Blocking here would make the purge unreachable on every
    single-bucket deployment for a question that has no answer to give.
    """
    report = _clean()
    report.skipped.append(CategorySkipped(category="unbound_namespaces", reason="warehouses are off"))

    assert report_is_clean(report) is None


def test_coverage_gap_DEFAULTS_FALSE_so_a_new_skip_cannot_silently_block() -> None:
    """The default is the safe direction for the gate's availability, and it is a deliberate choice.

    A new category skipped for a does-not-apply reason must not start blocking every purge in the
    estate the day it is added. The cost is that a genuine gap must SAY so — which is the flag's whole
    job, and is enforced by the orphan-scan test above rather than by the default.
    """
    assert CategorySkipped(category="x", reason="y").coverage_gap is False


def test_the_gate_reports_EVERY_gap_not_just_the_first() -> None:
    """An operator who fixes the named gap and re-runs must not discover a second one, then a third.

    A blocker naming one of three gaps turns one decision into three round trips through a scan that
    opens every dataset in the estate.
    """
    report = _clean()
    report.skipped.append(CategorySkipped(category="orphan_files", reason="off", coverage_gap=True))
    report.skipped.append(CategorySkipped(category="deep_prefixes", reason="off", coverage_gap=True))

    blocker = report_is_clean(report)

    assert blocker is not None
    assert "orphan_files" in blocker and "deep_prefixes" in blocker


def test_findings_still_outrank_a_coverage_gap_in_the_message() -> None:
    """Real drift is the more actionable answer, so it must not be masked by the gap message.

    A report with both should say what it FOUND — the operator can act on that immediately, whereas
    the gap is a configuration change.
    """
    report = _clean()
    report.total = 3
    report.counts["orphan_files"] = 3
    report.skipped.append(CategorySkipped(category="deep_prefixes", reason="off", coverage_gap=True))

    blocker = report_is_clean(report)

    assert blocker is not None
    assert "NOT clean" in blocker, "a report with real findings should report the findings first"
