"""A15's other half: the run ceiling the chart declared and no code enforced.

`chart/values.yaml` set `RASK_INGEST_MAX_RUN_HOURS: "24"`, and `tests/unit/test_gates_a15_a18.py`
asserted `maintenance.olderThanDays * 24 >= max_run_hours` and passed. That gate certifies a real
property — version GC must keep more history than a run can take, or it deletes the version a live
run is committing against — but it was certifying a relation with only ONE side implemented:
`grep MAX_RUN_HOURS services/ingest/src` returned nothing.

A green gate over an unenforced relation is worse than no gate. It reads as a guarantee, and the
failure it exists to prevent is silent: GC reclaims a version mid-run and the commit fails, or worse,
succeeds against a base that has moved.

These tests pin the enforcement, and the ZERO-means-unbounded default, which is not a detail — this
plane advertises million-unit harvests, so a live default would kill the legitimate long run the
ceiling exists to protect.
"""

from __future__ import annotations

import importlib

import pytest


def _reload_workflow(monkeypatch: pytest.MonkeyPatch, hours: str | None):
    """Re-import with the env set, because the ceiling is read at module scope."""
    from ingest import workflow

    if hours is None:
        monkeypatch.delenv("RASK_INGEST_MAX_RUN_HOURS", raising=False)
    else:
        monkeypatch.setenv("RASK_INGEST_MAX_RUN_HOURS", hours)
    return importlib.reload(workflow)


def test_the_ceiling_DEFAULTS_TO_UNBOUNDED(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero, and it must stay zero in code.

    The deployment opts in. A code default of 24h would silently fail every harvest longer than a
    day — which this plane explicitly supports — and it would do so by design rather than by
    accident, which is worse.
    """
    workflow = _reload_workflow(monkeypatch, None)

    assert workflow.MAX_RUN_HOURS == 0


def test_the_ceiling_is_READ_from_the_env_the_chart_sets(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact variable name matters: the chart writes `RASK_INGEST_MAX_RUN_HOURS` and gate A15
    reads it out of `values.yaml`. A mismatch here reproduces the original defect — a gate asserting
    against a value the code never sees."""
    workflow = _reload_workflow(monkeypatch, "24")

    assert workflow.MAX_RUN_HOURS == 24.0


def test_an_EMPTY_env_value_is_unbounded_not_a_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    """`kubectl set env FOO=` leaves an empty string, and `float("")` raises. A ceiling that crashes
    the module on an empty value would take the whole service down at import for a config typo."""
    workflow = _reload_workflow(monkeypatch, "")

    assert workflow.MAX_RUN_HOURS == 0


def test_the_deadline_uses_a_DURABLE_dapr_timer_not_an_in_process_one() -> None:
    """A13 forbids in-process polling loops and permits Dapr's own durable timers by name.

    Asserted structurally, because the difference is invisible at a glance and decides whether the
    ceiling survives a pod death: `ctx.create_timer` is runtime-managed — the workflow SUSPENDS and
    the runtime wakes it — while `asyncio.sleep` in workflow code would both break replay determinism
    and vanish with the pod, silently removing the ceiling.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "ingest" / "workflow.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))

    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "ingest_run")
    calls = {node.func.attr for node in ast.walk(fn) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}

    assert "create_timer" in calls, "the run deadline is not a durable Dapr timer"

    sleeps = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "sleep"]
    assert not sleeps, "an in-process sleep inside a workflow function breaks replay determinism and dies with the pod"


def test_a_timed_out_run_does_NOT_fall_through_to_finalize() -> None:
    """The consequence that makes this a correctness fix rather than a nicety.

    Committing whatever drained before the deadline would publish a PARTIAL harvest and mark it
    complete — a dataset nobody asked for, indistinguishable downstream from a whole one. The
    deadline path emits the terminal lineage event and RETURNS; the staged fragments stay staged, and
    a re-run converges on the same rows because unit ids are content-derived.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "ingest" / "workflow.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "ingest_run")

    # The timeout branch is the `if winner is deadline:` body. It must contain a `return` and must not
    # call `finalize`.
    branch = next(
        (
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name) and node.test.left.id == "winner"
        ),
        None,
    )
    assert branch is not None, "no `winner is deadline` branch — the deadline is not acted on"

    names = {n.id for n in ast.walk(branch) if isinstance(n, ast.Name)}
    assert any(isinstance(n, ast.Return) for n in ast.walk(branch)), "the timeout branch does not return — it falls through to finalize"
    assert "finalize" not in names, "the timeout branch calls finalize — that commits a PARTIAL harvest as if it were whole"


# ── the status must actually REACH the door ──────────────────────────────────


def test_a_workflow_that_FAILS_BY_POLICY_is_not_reported_COMPLETE() -> None:
    """The latent defect the deadline made reachable.

    A workflow can fail by POLICY — return a FAILED outcome rather than raising. The timeout path
    does exactly that: it refuses to commit a partial harvest, records the reason, and returns. To
    Dapr that is a workflow which RETURNED, so `runtime_status` is COMPLETED.

    `merge_workflow_state` promoted the outcome's status only when it was COMPLETE or
    COMPLETE_WITH_ERRORS, so FAILED was silently dropped and the door answered COMPLETE — a run that
    deliberately failed reported as success, which is the worst direction for this error to point.
    """
    from ingest.runs import RunRecord, merge_workflow_state

    record = RunRecord(run_id="r", project="p", dataset="d", kind="local-dir")
    state = {
        "runtime_status": "COMPLETED",  # the workflow RETURNED — it did not crash
        "serialized_output": {"status": "FAILED", "errors": {"run": "exceeded the 24.0h ceiling"}, "committed_version": None},
    }

    merged = merge_workflow_state(record, state)

    assert merged.status == "FAILED"
    assert "ceiling" in merged.errors.get("run", "")


def test_an_ENGINE_failure_still_wins_over_a_partial_output() -> None:
    """The refinement must only ever run on a NORMAL return.

    A crashed or operator-terminated instance is authoritative: whatever happens to sit in a partial
    output payload must not be able to talk the status back up to COMPLETE.
    """
    from ingest.runs import RunRecord, merge_workflow_state

    record = RunRecord(run_id="r", project="p", dataset="d", kind="local-dir")

    crashed = merge_workflow_state(record, {"runtime_status": "FAILED", "serialized_output": {"status": "COMPLETE"}})
    killed = merge_workflow_state(record, {"runtime_status": "TERMINATED", "serialized_output": {"status": "COMPLETE"}})

    assert crashed.status == "FAILED"
    assert killed.status == "FAILED"


# ── the unit ceiling ─────────────────────────────────────────────────────────


def test_the_unit_ceiling_DEFAULTS_TO_UNBOUNDED(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same rule as the hour ceiling: the deployment opts in.

    A live default would refuse the million-unit harvests this plane exists for, which is the
    opposite of protecting them.
    """
    monkeypatch.delenv("RASK_INGEST_MAX_UNITS", raising=False)
    workflow = _reload_workflow(monkeypatch, None)

    assert workflow.MAX_UNITS == 0


def test_the_unit_ceiling_is_read_from_its_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RASK_INGEST_MAX_UNITS", "500")
    workflow = _reload_workflow(monkeypatch, None)

    assert workflow.MAX_UNITS == 500


def test_the_ceiling_refuses_BEFORE_any_unit_is_published() -> None:
    """WHERE the refusal sits is the whole value of it.

    The case is a mis-pointed source: `s3-prefix` with an empty prefix lists a whole bucket, which
    the registry explicitly invites. Refusing after the fan-out would already have published millions
    of queue messages and spawned thousands of child workflows — the exact cost the ceiling exists to
    avoid. So the check must precede `call_child_workflow` in the parent's body.
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "ingest" / "workflow.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "ingest_run")

    def _line_of(pred) -> int | None:
        return next((n.lineno for n in ast.walk(fn) if pred(n)), None)

    ceiling = _line_of(lambda n: isinstance(n, ast.Name) and n.id == "MAX_UNITS")
    fanout = _line_of(lambda n: isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "call_child_workflow")

    assert ceiling is not None, "MAX_UNITS is never consulted in the parent workflow"
    assert fanout is not None
    assert ceiling < fanout, "the unit ceiling is checked AFTER the fan-out — by then the units are already published"


def test_the_refusal_is_a_POLICY_failure_not_a_raise() -> None:
    """Raising inside `enumerate_chunks` would burn four activity retries RE-LISTING the source
    before failing, and the operator would read a crash rather than a limit. Returning a FAILED
    outcome reports the reason once — and only reaches the door because the status merge now carries
    a returned FAILED through (`runs.merge_workflow_state`)."""
    from ingest.runs import RunRecord, merge_workflow_state

    record = RunRecord(run_id="r", project="p", dataset="d", kind="s3-prefix")
    state = {
        "runtime_status": "COMPLETED",
        "serialized_output": {"status": "FAILED", "errors": {"run": "source enumerated 900000 units, over the 1000 ceiling (RASK_INGEST_MAX_UNITS)."}},
    }

    merged = merge_workflow_state(record, state)

    assert merged.status == "FAILED"
    assert "ceiling" in merged.errors["run"]
