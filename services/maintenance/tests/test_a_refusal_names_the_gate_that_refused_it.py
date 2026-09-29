"""A refused dataset must reach the counter with the GATE that refused it.

Measured on the deployed estate 2026-09-24: **45.3-46.5% of every sweep is refused**, steady across
fourteen hours at ~600 datasets a tick. A standing condition that large has to be actionable, and the
unlabelled series could not say which of four things it was — somebody else's clone
(`protected_base`, true forever), a manifest feature a pylance upgrade would support
(`manifest_flags`), an unparseable branch directory (`invalid_ref`) or a missing grant
(`vend_denied`). Each has a different owner and a different fix.

THE BREAKDOWN ALREADY EXISTED AND STOPPED SHORT OF THE METRIC. `DatasetResult.refused_by` carries it
and `summarize_refusals` counts by it, into the sweep's one WARNING and the response body — the same
shape `compaction.bytes.reclaimed` had before it reached a series.

THE ZERO STAYS UNLABELLED, and that is not an oversight: the caller cannot know which gates exist on a
tick that refused nothing, and a `refused_by="none"` would put a value in the label set that names no
gate. `sum(rate(...))` is unaffected; `sum by (refused_by)` reads the breakdown.
"""

from __future__ import annotations

from typing import Any

import pytest

from maintenance.core import metrics


@pytest.fixture
def added(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, dict[str, Any] | None]]:
    seen: list[tuple[int, dict[str, Any] | None]] = []
    monkeypatch.setattr(metrics._refused, "add", lambda amount, attributes=None, **_: seen.append((amount, attributes)))
    return seen


@pytest.mark.parametrize("gate", ["manifest_flags"])
def test_a_refusal_carries_the_gate(gate: str, added: list[tuple[int, dict[str, Any] | None]]) -> None:
    metrics.record_refused(1, gate)

    assert added == [(1, {"refused_by": gate})], f"the gate did not reach the series: {added}"
