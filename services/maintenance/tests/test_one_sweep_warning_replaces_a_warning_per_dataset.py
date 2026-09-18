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

import logging
from datetime import timedelta
from pathlib import Path

import lance
import pyarrow as pa
import pytest

from maintenance.services.optimize import DatasetResult, compact_one, summarize_refusals
from service_kit.lakehouse.base_refs import BaseRefs


def _referenced_root(tmp_path: Path) -> str:
    src = str(tmp_path / "parent.lance")
    lance.write_dataset(pa.table({"id": pa.array([1, 2, 3], pa.int64())}), src, mode="create")
    return src


def test_a_refused_dataset_does_not_WARN(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """One refusal is DEBUG; the sweep's own line is where the operator hears about it."""
    src = _referenced_root(tmp_path)

    with caplog.at_level(logging.DEBUG, logger="maintenance.services.optimize"):
        result = compact_one(src, {}, timedelta(seconds=0), protected=BaseRefs(protected={src.lstrip("/")}))

    assert result.refused is not None
    refusals = [r for r in caplog.records if r.msg == "maintenance_refused_protected_base"]
    assert len(refusals) == 1, "the refusal stopped being logged at all"
    assert refusals[0].levelno == logging.DEBUG, "a permanent per-dataset fact is still shouting"


def test_the_refusal_says_WHICH_GATE_refused_it(tmp_path: Path) -> None:
    """One WARNING replacing 116 is only an improvement if it distinguishes the two causes.

    "Another dataset resolves its files through this root" and "this manifest sets a flag this pass
    cannot rewrite" are acted on differently — the first is someone else's clone, the second is a
    pylance upgrade away from being supported — and a bare count merges them.
    """
    src = _referenced_root(tmp_path)

    result = compact_one(src, {}, timedelta(seconds=0), protected=BaseRefs(protected={src.lstrip("/")}))

    assert result.refused_by == "protected_base"


def test_the_sweep_counts_refusals_by_gate() -> None:
    """The one line's payload: how many, and by which gate."""
    results = [
        DatasetResult(uri="a", refused="x", refused_by="protected_base"),
        DatasetResult(uri="b", refused="y", refused_by="protected_base"),
        DatasetResult(uri="c", refused="z", refused_by="manifest_flags"),
        DatasetResult(uri="d"),
    ]

    assert summarize_refusals(results) == {"protected_base": 2, "manifest_flags": 1}


def test_a_sweep_that_refused_NOTHING_says_nothing() -> None:
    """An empty breakdown, so the caller can stay silent rather than WARN a zero.

    `record_refused` emits its zero deliberately (a whitelist that stops matching must be visible as a
    number), and that is a METRIC. A log line has the opposite property: one that fires every tick
    saying nothing happened is the noise this test exists to prevent.
    """
    assert summarize_refusals([DatasetResult(uri="a"), DatasetResult(uri="b")]) == {}
