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


@pytest.mark.parametrize("gate", ["manifest_flags", "protected_base", "invalid_ref", "vend_denied"])
def test_a_refusal_carries_the_gate(gate: str, added: list[tuple[int, dict[str, Any] | None]]) -> None:
    metrics.record_refused(1, gate)

    assert added == [(1, {"refused_by": gate})], f"the gate did not reach the series: {added}"


def test_the_zero_is_emitted_and_carries_NO_gate(added: list[tuple[int, dict[str, Any] | None]]) -> None:
    """The always-emit property is load-bearing — a whitelist that starts refusing the whole estate
    must be visible from the FIRST tick — and it must not invent a gate to do it."""
    metrics.record_refused(0, None)

    assert added == [(0, None)], f"the zero was dropped or labelled: {added}"


def test_a_refusal_with_NO_gate_still_counts(added: list[tuple[int, dict[str, Any] | None]]) -> None:
    """An unattributed refusal must not vanish. It lands in the empty-label series, where
    `sum(rate(...))` still sees it and `sum by (refused_by)` shows it as unattributed rather than as
    nothing at all."""
    metrics.record_refused(1, None)

    assert added == [(1, None)], f"an unattributed refusal was dropped: {added}"


def test_the_sweep_passes_the_gate_through() -> None:
    """The wiring, not the recorder: `_maintain_one`'s caller reads `refused_by` off the result.

    Asserted on the source because the alternative is driving a whole sweep to observe one argument,
    and what a commit can get wrong here is dropping the second argument.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "src/maintenance/services/sweep.py").read_text(encoding="utf-8")

    assert "record_refused(1 if result.refused else 0, result.refused_by)" in source, (
        "the sweep calls record_refused without the gate, so every refusal lands unattributed"
    )
    assert 'refused_by="vend_denied"' in source, "the credential-vend refusal names no gate, so a missing grant reads as an unsupported manifest"


def test_a_ZERO_never_carries_a_gate_even_when_one_is_offered(added: list[tuple[int, dict[str, Any] | None]]) -> None:
    """The reachable shape the other zero leg cannot see.

    `optimize.py` sets `refused_by` on a result independently of `refused`, and the sweep passes both
    through — so `(0, "manifest_flags")` is a call this code can receive. Labelling that zero would
    create a series asserting the gate refused nothing, which reads as "this gate is clean" on a tick
    where it was never consulted. Without this leg the guard is untestable: every other case has
    `refused_by` already None.
    """
    metrics.record_refused(0, "manifest_flags")

    assert added == [(0, None)], f"a zero was attributed to a gate that refused nothing: {added}"
