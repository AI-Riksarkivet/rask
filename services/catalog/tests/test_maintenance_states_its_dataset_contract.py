"""CAT-CORE-15 — the maintenance doors state which dataset surface they need, instead of ``Any``.

Every op here took ``ds: Any``. ``Any`` is not "a Lance dataset": it is "no contract", and these are the
DESTRUCTIVE doors — ``run_gc`` reclaims versions, ``compact_now`` rewrites fragments. A caller handing
the wrong handle (a namespace, a describe response, a branch checkout) gets no diagnosis from the type
checker at all, and a reader cannot learn from the signature what the fakes in the unit tier must
provide. The module's own docstring already promises "pure over a Lance dataset handle, so both are
unit-testable with a fake ``ds``" — a structural Protocol says exactly that; ``Any`` says nothing.

Named parameters only: ``dict[str, Any]`` returns are genuine JSON, a local bound to an untyped pylance
result is not the defect, and ``**kwargs: Any`` is the correct annotation for a splat (the estate says so
in ``services/warehouses.py``' own ``create_bucket`` note).
"""

from __future__ import annotations

import ast
import pathlib


_MODULE = pathlib.Path(__file__).resolve().parents[1] / "src" / "catalog" / "services" / "maintenance.py"


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    return [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]


def test_the_walk_sees_the_maintenance_ops() -> None:
    fns = [n for n in ast.walk(ast.parse(_MODULE.read_text())) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    assert len(fns) >= 6, f"only {len(fns)} functions parsed — the walk is not seeing catalog maintenance"


def test_no_maintenance_op_takes_a_bare_Any_parameter() -> None:
    offences = [
        f"maintenance.py:{fn.lineno} {fn.name}({arg.arg}: Any)"
        for fn in ast.walk(ast.parse(_MODULE.read_text()))
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
        for arg in _params(fn)
        if isinstance(arg.annotation, ast.Name) and arg.annotation.id == "Any"
    ]
    assert not offences, "maintenance parameters typed `Any` — state the surface with a Protocol:\n  " + "\n  ".join(offences)
