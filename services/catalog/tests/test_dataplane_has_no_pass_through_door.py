"""CAT-CORE-17 — the data plane has no function whose entire body forwards its own arguments onward.

``create_table`` was 36 lines that did nothing: nine parameters, a docstring, a comment, and a single
``return _create_table_direct(...)`` handing every one of them straight through (two renamed on the way,
which is the only thing the layer added and is itself a hazard — the caller's ``allow_external_blobs``
arrives as ``allow_external``). Two names for one operation means a reader has to open both to learn
that the second is the whole of it, and the docstrings had already drifted apart.

Generic rather than name-checked: any data-plane function whose body is a docstring plus one ``return
f(...)`` passing only its own parameters, unchanged, is the same defect. ``_version``-style bodies
(``return f(...).attr``) do real work with the result and are not flagged.
"""

from __future__ import annotations

import ast
import pathlib


_DATAPLANE = pathlib.Path(__file__).resolve().parents[1] / "src" / "catalog" / "services" / "dataplane.py"


def _is_pass_through(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    body = [n for n in fn.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    if len(body) != 1 or not isinstance(body[0], ast.Return) or not isinstance(body[0].value, ast.Call):
        return False
    call = body[0].value
    params = {a.arg for a in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]}
    supplied = [*call.args, *[kw.value for kw in call.keywords]]

    def forwarded(node: ast.expr) -> bool:
        # A bare reference to one of this function's own parameters — or that same reference with an
        # empty-literal fallback (``bases or []``), which is a default the callee could carry itself.
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or) and len(node.values) == 2:
            head, tail = node.values
            if isinstance(tail, ast.List | ast.Dict | ast.Constant) and not getattr(tail, "elts", None) and not getattr(tail, "keys", None):
                node = head
        return isinstance(node, ast.Name) and node.id in params

    # Anything genuinely computed, reordered or literal means the layer is doing something.
    return bool(supplied) and all(forwarded(a) for a in supplied)


def test_the_walk_sees_the_data_plane() -> None:
    fns = [n for n in ast.walk(ast.parse(_DATAPLANE.read_text())) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    assert len(fns) > 30, f"only {len(fns)} functions parsed — the walk is not seeing dataplane.py"


def test_no_data_plane_function_is_a_pure_pass_through() -> None:
    offences = [
        f"dataplane.py:{fn.lineno} {fn.name}"
        for fn in ast.walk(ast.parse(_DATAPLANE.read_text()))
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef) and _is_pass_through(fn)
    ]
    assert not offences, "data-plane functions that only forward their own arguments — inline the callee:\n  " + "\n  ".join(offences)
