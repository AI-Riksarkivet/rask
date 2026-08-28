"""The FAIL-run build+publish is a single seam, not copy-pasted through ``handle_stage``.

MED-005: four near-identical ``build_run_event(... event_type="FAIL" ...)`` +
``publish_lineage_with_outbox(...)`` pairs lived inline in ``handle_stage``. The emit is one
contract; four copies drift. This pins that the ``event_type="FAIL"`` literal no longer appears
inside ``handle_stage`` (it lives in the shared helper the four sites now call).
"""

from __future__ import annotations

import ast
from pathlib import Path


_TRANSFORM = Path(__file__).resolve().parents[1] / "src" / "medallion" / "services" / "transform.py"


def _handle_stage_node(tree: ast.Module) -> ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "handle_stage":
            return node
    raise AssertionError("handle_stage not found in transform.py")


def test_fail_event_type_literal_is_not_copy_pasted_in_handle_stage() -> None:
    tree = ast.parse(_TRANSFORM.read_text())
    handle_stage = _handle_stage_node(tree)
    fail_literals = [
        kw
        for call in ast.walk(handle_stage)
        if isinstance(call, ast.Call)
        for kw in call.keywords
        if kw.arg == "event_type" and isinstance(kw.value, ast.Constant) and kw.value.value == "FAIL"
    ]
    assert len(fail_literals) <= 1, f"handle_stage still builds {len(fail_literals)} FAIL run events inline; factor them into one helper"
