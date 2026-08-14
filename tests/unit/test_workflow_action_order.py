"""Changing the NUMBER or ORDER of a live workflow's activities is a DEPLOY-COUPLED change.

THE HAZARD. Dapr replays a workflow against its recorded history and compares, position by position,
the action the code produces against the action history holds. A mismatch raises
`_get_wrong_action_name_error` — and it is raised inside `process_event`, OUTSIDE the generator, so a
workflow's own `except` cannot catch it. The instance goes terminal FAILED. Dapr's own guidance is
blunt: "Changing the number or order of workflow tasks causes a workflow's history to no longer match
the workflow code and may result in runtime errors."

THIS HAS HAPPENED TWICE IN THIS REPO, and neither was noticed at review time:

  * `bd191c08` inserted `resolve_limits` ahead of `ensure_dataset` and changed that activity's return
    type, so any instance in flight across that deploy diverges.
  * §2.4's own fix inserted `terminate_chunks` before `emit_terminal` on both abandonment paths —
    self-reported in open_dapr.md §4.2 after the fact.

WHAT THIS GATE DOES, AND DELIBERATELY DOES NOT DO. It does not prevent the change: sometimes the
sequence must change, and §2.4's was a genuine fix. It makes the change VISIBLE — a snapshot the
author has to update by hand, which is the moment they read this docstring and decide how to deploy
it. An invisible hazard becomes a deliberate act with a deploy note.

WHY A SNAPSHOT RATHER THAN A RULE. There is no rule that distinguishes a safe insertion from an
unsafe one — safety depends on whether an in-flight instance can be at that position, which is a
property of the RUNNING ESTATE, not of the code. So the gate cannot decide; it can only make sure a
human does.

THE REAL FIX IS A VERSIONING SEAM, and the estate has none: `is_patched`, `continue_as_new` and named
workflow versions appear nowhere under `services/` or `packages/`, though the SDK exposes them. Until
one exists, "drain before deploying" is the only safe answer and this gate is what prompts asking the
question. See open_dapr.md §4.2.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]

#: Every workflow BODY in the estate, as `(module, function)`. Listed rather than discovered so a new
#: workflow cannot land without joining the gate — the same reason `testpaths` is explicit.
WORKFLOW_BODIES = (
    ("services/ingest/src/ingest/workflow.py", "ingest_run"),
    ("services/ingest/src/ingest/workflow.py", "chunk_run"),
    ("services/flows/src/flows/workflow.py", "flow_run_workflow"),
    ("services/medallion/src/medallion/workflow.py", "stage_run"),
)

#: The committed snapshot. Regenerate with `RASK_UPDATE_WORKFLOW_SNAPSHOT=1 uv run pytest
#: tests/unit/test_workflow_action_order.py` and READ THE DOCSTRING ABOVE before committing the diff.
SNAPSHOT = Path(__file__).parent / "workflow_action_order.json"


def _actions(module: str, function: str) -> list[str]:
    """The ordered action names a workflow body yields, as source alone can see them.

    Counts `call_activity`, `call_child_workflow`, `create_timer`, `wait_for_external_event` and
    `when_all`/`when_any` — every construct that consumes a history position. Order is SOURCE order,
    which is not execution order for branches; that is fine and is the point. The snapshot's job is to
    change whenever the set of positions a replay could land on changes, not to simulate a run.
    """
    tree = ast.parse((REPO / module).read_text(encoding="utf-8"))
    target = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == function),
        None,
    )
    assert target is not None, f"{function} not found in {module} — it was renamed, which is itself a replay break"

    # SORTED BY SOURCE POSITION. `ast.walk` is breadth-first, so walking it directly produces an order
    # that has nothing to do with the code — which would make every diff unreadable and hide WHERE an
    # insertion landed, the one thing a reader needs to judge whether in-flight runs can be there.
    found: list[tuple[int, int, str]] = []
    for node in ast.walk(target):
        if not isinstance(node, ast.Call):
            continue
        call = ast.unparse(node.func)
        if call.endswith(("ctx.call_activity", "ctx.call_child_workflow", "ctx.create_timer", "ctx.wait_for_external_event")):
            verb = call.rsplit(".", 1)[-1]
            # The activity NAME matters as much as the count: swapping two same-arity activities keeps
            # the length identical and still diverges on replay.
            named = ast.unparse(node.args[0]) if node.args else next((ast.unparse(k.value) for k in node.keywords if k.arg == "workflow"), "")
            found.append((node.lineno, node.col_offset, f"{verb}({named})" if named else verb))
        elif call.endswith(("wf.when_all", "wf.when_any")):
            found.append((node.lineno, node.col_offset, call.rsplit(".", 1)[-1]))
    return [label for _lineno, _col, label in sorted(found)]


def _current() -> dict[str, list[str]]:
    return {f"{module}::{function}": _actions(module, function) for module, function in WORKFLOW_BODIES}


def test_the_activity_sequence_matches_the_committed_snapshot() -> None:
    """THE GATE. A diff here is not a failure — it is a question: can an in-flight run be at one of
    the positions that moved, and if so, does this deploy need a drain first?"""
    import os

    current = _current()

    if os.environ.get("RASK_UPDATE_WORKFLOW_SNAPSHOT"):  # pragma: no cover — the regeneration path
        SNAPSHOT.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip("snapshot regenerated — read this module's docstring before committing the diff")

    assert SNAPSHOT.exists(), "the workflow action snapshot is missing — regenerate with RASK_UPDATE_WORKFLOW_SNAPSHOT=1"
    recorded = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    for name, actions in current.items():
        before = recorded.get(name)
        assert before is not None, (
            f"{name} is a NEW workflow body with no snapshot. Add it deliberately — a new workflow is safe, "
            f"but confirm it is new rather than renamed: a rename is a replay break for every in-flight instance."
        )
        assert actions == before, (
            f"THE ACTIVITY SEQUENCE OF {name} CHANGED.\n"
            f"  was: {before}\n"
            f"  now: {actions}\n\n"
            f"Dapr replays against recorded history and raises `_get_wrong_action_name_error` when the code "
            f"produces a different action than history holds at that position — outside the generator, so no "
            f"error boundary catches it, and the instance goes terminal FAILED.\n\n"
            f"This is not necessarily wrong. Decide, then act:\n"
            f"  1. Can an in-flight instance be at a position that moved? (Positions AFTER the change are safe; "
            f"an instance that has not reached them yet replays the new code cleanly.)\n"
            f"  2. If yes, the deploy needs in-flight runs DRAINED first, or a versioning seam — the estate has "
            f"none today (open_dapr.md §4.2).\n"
            f"  3. Then regenerate: RASK_UPDATE_WORKFLOW_SNAPSHOT=1 uv run pytest {Path(__file__).name}"
        )

    stale = set(recorded) - set(current)
    assert not stale, f"these workflow bodies are in the snapshot but no longer exist: {sorted(stale)} — a rename is a replay break"


def test_the_gate_covers_every_registered_workflow() -> None:
    """A workflow registered with the runtime but absent from `WORKFLOW_BODIES` is ungated, and the
    hand-kept list is exactly where that drift happens."""
    registered: set[str] = set()
    for module, _ in WORKFLOW_BODIES:
        tree = ast.parse((REPO / module).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "WORKFLOWS" for t in node.targets):
                registered |= {ast.unparse(e) for e in getattr(node.value, "elts", [])}

    gated = {function for _, function in WORKFLOW_BODIES}
    missing = registered - gated
    assert not missing, f"registered with the runtime but not gated: {sorted(missing)} — add them to WORKFLOW_BODIES"
