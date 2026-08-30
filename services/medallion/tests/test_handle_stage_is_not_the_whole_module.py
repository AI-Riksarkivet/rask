"""`handle_stage` is a handler, not the module (MED-004).

It reached 919 lines — 75% of `transform.py` — carrying lane routing, tenant resolution, authz,
compute dispatch, lineage emission, the quality gate and four failure taxonomies in one body. That
is not a style complaint. It is the direct cause of two other findings in the same audit: MED-005
(four copies of the FAIL block, because no reviewer could see all four at once) and MED-001 (the
project semantics buried hundreds of lines from the head that produces them). The four exception
paths had already drifted apart in what they recorded — one bumped `record_quality_blocked`, three
did not — which is exactly what a body nobody can hold in view produces.

It is also why the module had ONE entry point: every branch inside it — the lane discriminator, the
root resolution, the `from_uri` confinement, the gate — could only be exercised by driving a whole
stage trigger through a Dapr client, an FGA client, an object store and a catalog.

Two ceilings, and the second is the one that stops this recurring: the handler is bounded, AND no
seam it delegates to is allowed to become the next 900-line body.
"""

from __future__ import annotations

import ast
from pathlib import Path


_TRANSFORM = Path(__file__).resolve().parents[1] / "src" / "medallion" / "services" / "transform.py"

#: A handler that reads as guard clauses and calls. Generous against the estate's own 20-50 line
#: rule, because this one carries a genuinely long prose record of WHY each step is ordered as it is.
MAX_HANDLER_LINES = 160

#: No seam may become the next god function. Same number, applied to every top-level def in the
#: module — including `handle_stage`, so the two ceilings cannot drift apart.
MAX_FUNCTION_LINES = 160

#: The seams the recommendation names, each independently callable. Their presence is what makes the
#: branches testable without a Dapr client, an object store and a catalog.
_SEAMS = (
    "_preflight",
    "_authorize",
    "_resolve_roots",
    "_confine_from_uri",
    "_run_compute",
    "_evaluate_promotion",
    "_report_hold",
    "_report_success",
)


def _module() -> ast.Module:
    return ast.parse(_TRANSFORM.read_text())


def _defs(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}


def _lines(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    assert node.end_lineno is not None
    return node.end_lineno - node.lineno + 1


def test_handle_stage_is_a_handler_not_the_module() -> None:
    tree = _module()
    handler = _defs(tree)["handle_stage"]
    span = _lines(handler)
    module_lines = len(_TRANSFORM.read_text().splitlines())
    assert span <= MAX_HANDLER_LINES, f"handle_stage is {span} lines — split it along the seams its own numbered comments already mark"
    assert span * 2 <= module_lines, f"handle_stage is {span} of {module_lines} lines; a handler that IS its module has no seams to test"


def test_the_seams_are_callable_on_their_own() -> None:
    from medallion.services import transform

    missing = [name for name in _SEAMS if not callable(getattr(transform, name, None))]
    assert not missing, f"handle_stage still owns these steps inline: {missing}"


def test_no_seam_becomes_the_next_god_function() -> None:
    oversized = {name: _lines(node) for name, node in _defs(_module()).items() if _lines(node) > MAX_FUNCTION_LINES}
    assert not oversized, f"top-level functions over {MAX_FUNCTION_LINES} lines: {oversized}"
