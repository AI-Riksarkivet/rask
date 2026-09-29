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

Both ceilings used to be module-level `os.getenv` reads branched on INSIDE the orchestrator, which is
a replay-determinism break in its own right — that is F12b, and `test_replay_hygiene.py` owns it.
What survives here is the ENFORCEMENT: that the ceilings exist, that they default to unbounded, and
that the deadline path refuses to commit a partial harvest.
"""

from __future__ import annotations


# ── the unit ceiling ─────────────────────────────────────────────────────────


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
        # `ast.walk` yields bare `AST`, which carries no position — only the positioned subclasses
        # do. Narrowing to those is what makes `.lineno` readable, and it excludes nothing either
        # predicate below can match: both are looking for expressions.
        return next((n.lineno for n in ast.walk(fn) if isinstance(n, ast.stmt | ast.expr) and pred(n)), None)

    ceiling = _line_of(lambda n: isinstance(n, ast.Attribute) and n.attr == "max_units")
    fanout = _line_of(lambda n: isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "call_child_workflow")

    assert ceiling is not None, "the unit ceiling is never consulted in the parent workflow"
    assert fanout is not None
    assert ceiling < fanout, "the unit ceiling is checked AFTER the fan-out — by then the units are already published"
