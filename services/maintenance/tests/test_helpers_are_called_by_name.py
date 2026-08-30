"""No helper in this service may be called by argument POSITION where the position is ambiguous (MAINT-16).

This service's helpers had grown to four, five and six positional-or-keyword parameters, and several
carried two parameters of the SAME type side by side: `compact_one(uri, options, older_than,
retain_versions, target_rows_per_fragment)` takes two adjacent `int | None`s, `_ghosts` takes two
adjacent `set[str]`s, `_orphan_buckets` takes `claimed` and `platform` the same way, and
`record_trash_purge` takes the purged and refused tallies as two `dict[str, int]`. Swapping any of those
pairs type-checks, passes every type-checker in the estate, and silently changes what a RECLAIMER does —
a retention count read as a fragment size, a platform bucket list read as the claimed one.

The rule is mechanical because the hazard is: two parameters a caller cannot tell apart at the call site
must be keyword-only. Route handlers are exempt — FastAPI resolves their parameters by annotation, not
by position, and the signature is the dependency declaration.
"""

from __future__ import annotations

import ast
from pathlib import Path


_SRC = Path(__file__).resolve().parents[1] / "src" / "maintenance"

#: FastAPI endpoint signatures ARE the dependency declaration — nothing calls them positionally.
_EXEMPT_FILES = {"api/routes.py"}

#: More than this many positional-or-keyword parameters and a call site stops being readable.
_MAX_POSITIONAL = 3


def _functions() -> list[tuple[str, int, ast.FunctionDef | ast.AsyncFunctionDef]]:
    found = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel in _EXEMPT_FILES:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                found.append((rel, node.lineno, node))
    return found


def _positional(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    return [a for a in (*node.args.posonlyargs, *node.args.args) if a.arg != "self"]


def test_no_helper_takes_two_positional_parameters_of_the_same_type() -> None:
    offenders = []
    for rel, line, node in _functions():
        seen: dict[str, str] = {}
        for arg in _positional(node):
            annotation = ast.unparse(arg.annotation) if arg.annotation else "<unannotated>"
            if annotation in seen:
                offenders.append(f"{rel}:{line} {node.name}({seen[annotation]}, …, {arg.arg}) — both {annotation}")
            seen[annotation] = arg.arg
    assert offenders == [], "silently swappable arguments:\n  " + "\n  ".join(offenders)


def test_no_helper_takes_more_than_three_positional_parameters() -> None:
    offenders = [f"{rel}:{line} {node.name} takes {len(_positional(node))}" for rel, line, node in _functions() if len(_positional(node)) > _MAX_POSITIONAL]
    assert offenders == [], "positional-parameter pile-ups:\n  " + "\n  ".join(offenders)
