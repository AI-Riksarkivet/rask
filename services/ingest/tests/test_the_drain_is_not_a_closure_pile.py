"""ingest-flow-10 — the drain's batch is an OBJECT, not five `nonlocal` names and a loop-local coroutine.

`Worker.drain_chunk` ran ~170 lines and held two nested closures over seven mutable names, and one of
them — `handle` — was defined INSIDE the fetch loop, so a fresh coroutine function was built on every
iteration and every reader had to re-derive which names it shared with the batch it was mutating.

That is not a style complaint here. The file already carries the scar: `held` has a comment
explaining that it is "a container that is never REASSIGNED — `flush` rebinds `pending_msgs` to a
fresh list, so a heartbeat closing over that name would keep renewing the previous batch while the
current one expires". A rule that has to be remembered at every rebinding site is the shape asking
for an object.

Both gates are package-wide rather than aimed at `drain_chunk` by name: a gate that names its target
stops covering the next one.
"""

from __future__ import annotations

import ast
from pathlib import Path

from ingest import config as config_mod


SRC = Path(config_mod.__file__).parent


def _sources() -> list[tuple[str, ast.Module]]:
    return [(p.name, ast.parse(p.read_text(encoding="utf-8"))) for p in sorted(SRC.glob("*.py"))]


def test_no_function_is_defined_inside_a_loop() -> None:
    """A `def` in a loop body rebuilds the function object every iteration and hides which names it
    closes over — the two questions a reader of a fetch loop least wants to be asking."""
    offenders = [
        f"{name}:{fn.lineno} {fn.name} (loop at line {loop.lineno})"
        for name, tree in _sources()
        for loop in ast.walk(tree)
        if isinstance(loop, ast.For | ast.AsyncFor | ast.While)
        for fn in ast.walk(loop)
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
    ]
    assert offenders == [], f"{offenders} are defined inside a loop body. Lift them to a method or a module function."


def test_no_closure_rebinds_more_than_one_enclosing_name() -> None:
    """ONE `nonlocal` is an accumulator; five is an object wearing a closure.

    `flush` declared `nonlocal pending, pending_msgs, pending_bytes, pending_parts, pending_tokens` —
    five parallel lists rebound together, with their invariant (positional parallelism) stated only in
    a comment. Nothing could enforce it, and the one time it slipped, the batch committed its units
    twice (`test_partial_ack_duplication.py`).
    """
    offenders = [
        f"{name}:{fn.lineno} {fn.name} rebinds {len(names)} enclosing names: {names}"
        for name, tree in _sources()
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
        for names in [sorted({n for node in ast.walk(fn) if isinstance(node, ast.Nonlocal) for n in node.names})]
        if len(names) > 1
    ]
    assert offenders == [], f"{offenders}. Hold the state on an object whose methods own its invariants."
