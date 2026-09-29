"""The refusal is per-sweep news, not per-dataset news.

MEASURED on the deployed estate 2026-09-18, one tick: 116 `maintenance_refused_protected_base` lines
at WARNING, every one of them the same permanent fact about a dataset the sweep will refuse again on
the next tick and the one after. It was roughly half of every WARN the estate emitted. A log level is a
claim about how much attention a line deserves, and a line that repeats unchanged forever at WARNING
spends the operator's attention on nothing — which is how the WARN that does matter gets missed.

The per-dataset fact is not deleted, it is DEMOTED: `refusals` in the sweep summary still names every
refused dataset with its reason, `compaction_datasets_refused_total` still counts them, and DEBUG still
prints the line for whoever is chasing one dataset. What changes is that a fact about 116 datasets is
reported once, with the breakdown that says which gate refused them.
"""

from __future__ import annotations

from maintenance.services.optimize import DatasetResult, summarize_refusals


def test_the_sweep_counts_refusals_by_gate() -> None:
    """The one line's payload: how many, and by which gate."""
    results = [
        DatasetResult(uri="a", refused="x", refused_by="protected_base"),
        DatasetResult(uri="b", refused="y", refused_by="protected_base"),
        DatasetResult(uri="c", refused="z", refused_by="manifest_flags"),
        DatasetResult(uri="d"),
    ]

    assert summarize_refusals(results) == {"protected_base": 2, "manifest_flags": 1}
