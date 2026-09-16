"""A sweep tick stops BETWEEN work items when its budget is spent, and says what it did not reach.

[[LH-101]]. The tick executes every planned dataset in one serial pass, so on a large estate the tail
is maintained only if the tick happens to have time left — and nothing records which datasets it never
got to, which is the silent starvation the row names. The dataset shuffle at `sweep.py:764` rotates
WHICH datasets sit behind that point; it does not bound the pass.

THE BOUNDARY IS BETWEEN ITEMS, NEVER INSIDE ONE. A budget that cut mid-compaction would leave a rewrite
half-done, which is worse than an unmaintained dataset — the row states this and it is the whole reason
the check sits in the loop rather than in `execute_unit`.

ZERO MEANS UNLIMITED AND IS THE DEFAULT, so an estate that never sets it behaves exactly as before. The
value is an operator decision (too small and a large estate never finishes a pass, too large and it is
decorative), and the row is explicit that inferring one would be guessing.

STOPPING SILENTLY WOULD BE THE ORIGINAL DEFECT WEARING A SETTING. The point of bounding the tick is to
make the tail visible, so exhausting the budget logs what was executed and what remains.
"""

from __future__ import annotations

from typing import Any

from maintenance.services import sweep as sweep_mod


class _Clock:
    """A monotonic stand-in: each read advances by one second.

    Budgets below are deliberately chosen OFF the boundary (2.5, not 2.0). Whether a pass that has spent
    exactly its budget may start one more item is an arbitrary choice, and a test that pinned it would
    be asserting a coin-flip as if it were a contract.
    """

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        self.t += 1.0
        return self.t


def test_a_spent_budget_stops_the_pass_between_items() -> None:
    executed: list[str] = []

    def run(item: Any) -> str:
        executed.append(item)
        return f"did-{item}"

    out = list(sweep_mod.execute_within_budget(["a", "b", "c", "d"], run=run, budget_seconds=2.5, monotonic=_Clock()))

    assert executed == ["a", "b"], f"the pass should stop between items once the budget is spent, ran {executed}"
    assert out == ["did-a", "did-b"]


def test_zero_budget_means_unlimited() -> None:
    """The default. An estate that never sets a budget must behave exactly as it did before."""
    executed: list[str] = []
    out = list(sweep_mod.execute_within_budget(["a", "b", "c"], run=executed.append, budget_seconds=0.0, monotonic=_Clock()))

    assert executed == ["a", "b", "c"]
    assert len(out) == 3


def test_exhausting_the_budget_says_what_it_did_not_reach(caplog: Any) -> None:
    """A tick that stops silently is the starvation this row exists to end, with a setting attached."""
    import logging

    with caplog.at_level(logging.WARNING, logger="maintenance.services.sweep"):
        list(sweep_mod.execute_within_budget(["a", "b", "c", "d"], run=lambda i: i, budget_seconds=2.5, monotonic=_Clock()))

    exhausted = [r for r in caplog.records if r.message == "sweep_budget_exhausted"]
    assert exhausted, f"stopping early logged nothing: {[r.message for r in caplog.records]}"
    assert getattr(exhausted[0], "executed", None) == 2
    assert getattr(exhausted[0], "remaining", None) == 2


def test_a_budget_never_cuts_inside_a_work_item() -> None:
    """The item that was started finishes. Only the NEXT one is refused."""
    started: list[str] = []
    finished: list[str] = []

    def run(item: str) -> str:
        started.append(item)
        finished.append(item)
        return item

    list(sweep_mod.execute_within_budget(["a", "b", "c"], run=run, budget_seconds=2.5, monotonic=_Clock()))

    assert started == finished, "a work item was started and not finished — the budget cut inside one"
