"""The drift report emits a series per CHECKED category, and none for the ones it could not check.

[[LH-099]]'s neighbour. The reconcile's drift report decides whether the purge may run at all and is
the estate's own answer to "is the storage state understood" — and it reached a log line and nothing
else. `metrics.py` carried ten recorders for the sweep, the purge and credential tiers, and not one
for drift, so no alert could fire on it and no dashboard could show it.

Measured live 2026-09-20: `orphaned_trash: 989`, a number visible only to someone reading pod logs.

ONLY WHAT WAS CHECKED, which is the report's own discipline carried onto the wire. `counts` omits a
category that was unavailable or skipped rather than zeroing it, precisely so a 0 can never read as
"clean" — and undoing that on the series an alert fires from would put the lie exactly where it does
the most damage. A category that WAS checked and found nothing still emits its 0: that is the
difference between "checked, clean" and "did not look", and it is the whole point.

A GAUGE, not a counter: drift is a level that rises and falls, and `delta()` over a counter would read
a repaired estate as no change at all.
"""

from __future__ import annotations

from typing import Any

import pytest

from maintenance.core import metrics


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, dict[str, Any]]]:
    seen: list[tuple[int, dict[str, Any]]] = []
    monkeypatch.setattr(metrics._drift_items, "set", lambda value, attrs: seen.append((value, attrs)))  # noqa: SLF001
    return seen


def test_every_checked_category_gets_a_series(recorded: list[tuple[int, dict[str, Any]]]) -> None:
    metrics.record_drift({"orphaned_trash": 989, "orphan_buckets": 3})

    assert sorted(recorded) == [(3, {"category": "orphan_buckets"}), (989, {"category": "orphaned_trash"})]


def test_a_checked_but_clean_category_still_emits_zero(recorded: list[tuple[int, dict[str, Any]]]) -> None:
    """ "Checked and clean" must be distinguishable from "did not look", which is why 0 is emitted."""
    metrics.record_drift({"orphan_buckets": 0})

    assert recorded == [(0, {"category": "orphan_buckets"})]


def test_an_unchecked_category_emits_nothing(recorded: list[tuple[int, dict[str, Any]]]) -> None:
    """THE DISCIPLINE: `counts` omits what it could not check, and the wire must not invent a zero."""
    metrics.record_drift({})

    assert recorded == []
