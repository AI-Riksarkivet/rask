"""An e2e leg that revokes the LIVE estate's authz must restore it on every exit.

[[LH-152]]. `tests/e2e-py/test_governed_union_e2e.py` proves the medallion's FGA gates enforce by
REVOKING the deployed cascade's own tuples and watching the cascade stop. The subjects are real —
`user:service-bronze-to-silver` and `user:service-silver-to-gold` — and the objects include `owner` on
the SHARED warehouse, so between the delete and the restore the deployed estate cannot run its own
medallion.

THE DELETE USED TO SIT ABOVE THE `try`, WITH THE PRECONDITION ASSERT BETWEEN THEM. That assert exists
to catch a revoke that did not take; when it fired it exited the function with the tuples deleted and
no `finally` in scope, so the one check written to protect the leg was also the one guaranteed to strip
the estate permanently. Both sub-phases had it.

A STATIC GATE, because the behavioural one cannot run here: this suite is `-m e2e`, needs a deployed
stack, and is deselected from `make test`. Reading the file is what a commit can get wrong, so reading
the file is what this checks — every `_tuples(..., deletes=...)` must sit inside a `try` whose
`finally` writes tuples back.

IT DOES NOT CLAIM TO CLOSE THE CRASH WINDOW. A SIGKILL between the delete and the restore still strips
the estate, and no in-process construct can prevent that. What it closes is every ORDINARY exit —
assertion, exception, early return — which is the population that actually occurs.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TypeIs


REPO = Path(__file__).resolve().parents[2]
SUITE = REPO / "tests/e2e-py/test_governed_union_e2e.py"


def _is_tuples_call(node: ast.AST, keyword: str) -> TypeIs[ast.Call]:
    """A `_tuples(..., <keyword>=...)` call — the estate's only authz-mutating helper in this suite.

    `TypeIs` rather than `bool`, so the narrowing reaches the call sites: `ast.walk` is typed to yield
    `AST`, which declares no `lineno`, and every use here reports a LINE NUMBER to the operator.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
    return name == "_tuples" and any(k.arg == keyword for k in node.keywords)


def _restores(handler: list[ast.stmt]) -> bool:
    return any(_is_tuples_call(n, "writes") for stmt in handler for n in ast.walk(stmt))


def _teardown_of_its_own_grant(tree: ast.Module) -> set[int]:
    """Revokes that UNDO a grant the same fixture made — cleanup, not a revoke of the estate's state.

    The two look identical at the call and are opposites in effect. `alice` writes a broad `reader`
    tuple at setup and deletes it after `yield`; that delete removes something the suite ADDED, so
    failing to reach it widens access rather than stripping it, and pytest runs a generator fixture's
    teardown even when the test fails. What this gate is for is the other shape: deleting a tuple the
    DEPLOYED estate already held, where the same miss breaks the live cascade.

    Recognised structurally — a function that both writes and yields — rather than by fixture name, so
    a second fixture of the same shape is covered by existing rather than by being remembered.
    """
    exempt: set[int] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        body = list(ast.walk(fn))
        if not any(isinstance(n, ast.Yield) for n in body):
            continue
        if not any(_is_tuples_call(n, "writes") for n in body):
            continue
        exempt |= {n.lineno for n in body if _is_tuples_call(n, "deletes")}
    return exempt


def test_every_revoke_sits_inside_a_try_that_restores() -> None:
    tree = ast.parse(SUITE.read_text(encoding="utf-8"))

    guarded: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try) or not _restores(node.finalbody):
            continue
        guarded += [n.lineno for stmt in node.body for n in ast.walk(stmt) if _is_tuples_call(n, "deletes")]

    every = [n.lineno for n in ast.walk(tree) if _is_tuples_call(n, "deletes")]
    assert every, "no authz revoke found in the governed-union suite — this gate would pass vacuously"

    unguarded = sorted(set(every) - set(guarded) - _teardown_of_its_own_grant(tree))
    assert not unguarded, (
        f"these revokes of the LIVE cascade's tuples are not inside a try/finally that restores them "
        f"(lines {unguarded} of {SUITE.relative_to(REPO)}). Any exit between the delete and the restore "
        f"— an assertion, an exception, a KeyboardInterrupt — leaves the deployed estate unable to run "
        f"its own medallion."
    )


def test_the_suite_still_revokes_at_all() -> None:
    """The control. The gate above is satisfied trivially by a suite that stopped revoking, which would
    mean the deny legs stopped proving the gates enforce."""
    tree = ast.parse(SUITE.read_text(encoding="utf-8"))
    assert len([n for n in ast.walk(tree) if _is_tuples_call(n, "deletes")]) >= 2, (
        "fewer than two revokes: the writer-deny and validator-deny sub-phases are what this suite is for"
    )
