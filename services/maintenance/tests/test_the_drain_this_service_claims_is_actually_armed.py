"""The drain `retry_when_draining` needs is armed on SIGTERM, and the index lane spends the same budget.

Two defects found by adversarially re-verifying [[LH-183]] the same day it shipped, and both are in
that work rather than around it.

ONE · THE CLAIM WAS IN THE CODE AND THE WIRING WAS NOT. `rewrite_slot.retire_this_worker` states that
SIGTERM to self is chosen because "`arm_drain_on_sigterm` flips `app.state.shutting_down` so the next
delivery is answered RETRY instead of started". Five services call `arm_drain_on_sigterm`; maintenance
was not one of them. A flag set in the lifespan's `finally` flips at UNWIND — after uvicorn has stopped
serving — so the admission gate it feeds refuses nothing, and a retiring worker keeps accepting units
it is about to abandon. The service arms the signal now, which is the behaviour `retire_this_worker`
depends on: SIGTERM to self is only a graceful retirement if the drain is armed to meet it.

TWO · A BUDGET THAT BOUNDS ONE LANE BOUNDS NOTHING. The retirement counts committed rewrites and is
checked in `handle_unit`. `api/index_work.py` is the OTHER subscription on the same process — an index
build over a whole vector column, on an `ackWait` an order of magnitude longer than the work queue's —
and it never asks. A worker that spends its budget on compactions and then receives only index units
runs past its ceiling, which is the outcome the budget exists to prevent. Same shape as the in-pod
rewrite path that had to be added to the count for the same reason.
"""

from __future__ import annotations

import ast
from pathlib import Path


_SRC = Path(__file__).resolve().parents[1] / "src" / "maintenance"


def _calls(path: Path) -> set[str]:
    """Every plain function name called anywhere in the module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return (
        {node.func.id for node in ast.walk(tree) if isinstance(node.func, ast.Name)}
        if False
        else {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    )


def test_the_service_arms_the_drain_it_documents() -> None:
    """A flag flipped in the lifespan's `finally` flips at UNWIND, which is after uvicorn has stopped
    serving — so the admission gate it feeds can never refuse a delivery. The five siblings that carry
    a sidecar-delivered route all arm it on the SIGNAL instead."""
    assert "arm_drain_on_sigterm" in _calls(_SRC / "service.py"), (
        "maintenance documents `arm_drain_on_sigterm` in `rewrite_slot.retire_this_worker` and never "
        "calls it — `retry_when_draining` on `on_unit` refuses nothing, and a retiring worker keeps "
        "accepting units it is about to abandon"
    )


def test_EVERY_subscription_lane_checks_the_retirement_budget() -> None:
    """The budget is a property of the PROCESS, so every lane that can keep it alive must ask.

    Walked rather than listed: a third subscription added later is covered the day it appears, which
    is the property that a hand-kept pair of module names cannot have.
    """
    # EXECUTING lanes only, and the code draws that line itself: `work` and `index_work` both refuse to
    # register unless `settings.execute_work`, while `arrival` gates on the work TOPIC alone because it
    # publishes units rather than running them. A publisher accumulates no rewrite retention, so
    # retiring on it would restart the planner for work the workers did. Derived from that guard rather
    # than from a list of two module names, so a third EXECUTING lane is covered the day it appears.
    lanes = {}
    for path in sorted((_SRC / "api").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "execute_work" in text and "subscribe" in text:
            lanes[path.name] = _calls(path)
    assert len(lanes) >= 2, f"the walk found {len(lanes)} executing lane(s) — it proves nothing"
    missing = sorted(name for name, calls in lanes.items() if "should_retire" not in calls)
    assert missing == [], (
        f"{missing} subscribe to a lane and never check the retirement budget — a worker that spends "
        "its budget on one lane and then receives only the other runs past its ceiling ([[LH-183]])"
    )
