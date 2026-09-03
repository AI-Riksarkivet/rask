"""Both execution lanes decide the signing credential through the same function.

The serial sweep and the queued executor must not drift on this. A queued unit and a serially-executed
one already share `execute_unit` so that "execution moved onto a queue" cannot quietly also change
what maintenance DOES; the credential a rewrite is signed with is part of what it does. If one lane
consulted `write_options_for` and the other passed `settings.storage_options()` straight through, then
turning the work topic on or off would silently change the estate's security posture — and the two
configurations are meant to differ only in where the work runs.

They share it TODAY because the choice lives in `maintain_one_item`, which both lanes reach through
`execute_unit`. That is the property worth pinning: not that each lane calls `write_options_for`, but
that neither lane can choose a credential of its own. A future edit that hoisted the decision into
`run_sweep` for "efficiency" would leave the queued lane silently on the ambient key.

Asserted structurally rather than by driving a sweep, because the failure is a MISSING call: a lane
that never asks is indistinguishable, from any output, from one that asked and was told to use the
ambient credential.
"""

from __future__ import annotations

import ast
import pathlib


_SWEEP = pathlib.Path(__file__).resolve().parents[1] / "src" / "maintenance" / "services" / "sweep.py"
_WORK = pathlib.Path(__file__).resolve().parents[1] / "src" / "maintenance" / "api" / "work.py"


def _calls(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                names.add(fn.id)
            elif isinstance(fn, ast.Attribute):
                names.add(fn.attr)
    return names


def test_the_credential_is_chosen_where_both_lanes_reach_it() -> None:
    assert "write_options_for" in _calls(_SWEEP), "nothing in sweep.py decides the signing credential"


def test_the_choice_lives_in_the_shared_unit_executor_not_the_serial_driver() -> None:
    """`maintain_one_item` is what the queued executor calls; `run_sweep` is the serial driver alone.
    The decision must sit in the former, or turning the work topic on changes the security posture."""
    tree = ast.parse(_SWEEP.read_text())
    holders = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "write_options_for" for c in ast.walk(node))
    }
    assert holders == {"maintain_one_item"}, f"the credential choice sits in {holders or 'nothing'}, not in the shared unit executor"


def test_the_queued_lane_reaches_it_through_that_executor() -> None:
    """Referenced, not called: the route hands `execute_unit` to `run_in_threadpool`, so it appears as
    a bare name rather than in call position."""
    names = {node.id for node in ast.walk(ast.parse(_WORK.read_text())) if isinstance(node, ast.Name)}
    assert "execute_unit" in names, "handle_unit no longer goes through execute_unit, so the shared choice is bypassed"


def test_the_vend_is_not_hoisted_above_the_cadence_stamp() -> None:
    """The one placement error that is invisible in every output.

    `execute_unit` does two things: `maintain_one_item` (the rewrite) and `_stamp_cadence`, which
    writes under `<control_root>/_policies/state/`. A table-scoped credential cannot reach that path,
    so vending one level up — at `execute_unit` rather than inside `maintain_one_item` — makes the
    stamp 403. And `_policy_skip_reason` reads an absent or unreadable stamp as "maintain", so the
    cadence silently stops pacing and every policied dataset is compacted on every tick. Nothing goes
    red: the compaction still succeeds, the metrics still move, the log still says SCOPED.
    """
    tree = ast.parse(_SWEEP.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "execute_unit":
            direct = [c for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "write_options_for"]
            assert not direct, "execute_unit vends directly — the cadence stamp it also performs cannot use a table-scoped credential"
            return
    raise AssertionError("execute_unit not found — this gate is asserting nothing")
