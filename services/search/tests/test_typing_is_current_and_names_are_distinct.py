"""PEP 695 type parameters, and no parameter shadowed by a local (VS-23).

open_python-audit VS-23, two small things in the search service's typing:

* `encoders/base.py` declared three module-level `TypeVar`s (`_Resp`, `_In`, `_Out`) used by exactly
  two methods, one each. A module-level TypeVar reads as a shared type variable — something several
  signatures relate to — when in fact each belongs to one method and nothing else may reference it.
  PEP 695 says that in the signature, and the runtime agrees: a function declared with type
  parameters carries them on `__type_params__`, which is what this pins (a comment could not).

* `target.py`'s `resolve_target(handle, table)` rebound `table` from the alignments capability's
  `"table.column"` split, so from that line on the identifier no longer meant the searchable table
  the CALLER asked for. Nothing reads it afterwards today — which is precisely the state in which
  the next reader adds a line that does.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from search.services import target
from search.services.encoders import base


def test_the_transport_methods_declare_their_own_type_parameters() -> None:
    assert base.VLLMTransport.post.__type_params__, (
        "`post` has no PEP 695 type parameters — its response type variable lives at module scope, where it reads as shared"
    )
    assert base.VLLMTransport.map.__type_params__, "`map` has no PEP 695 type parameters — its in/out variables live at module scope"


def test_no_module_level_type_variables_remain() -> None:
    leftovers = [name for name, value in vars(base).items() if type(value).__name__ == "TypeVar"]
    assert not leftovers, f"{leftovers} are still module-level TypeVars, so the ones on the methods are duplicates rather than a replacement"


def test_the_bound_survived_the_move() -> None:
    """The response variable is `bound=BaseModel` — a plain type parameter would drop the constraint."""
    from pydantic import BaseModel

    (resp,) = base.VLLMTransport.post.__type_params__
    assert resp.__bound__ is BaseModel


def _bound_names(node: ast.expr) -> list[str]:
    """Names an assignment target actually REBINDS — `x` and `a, b`, never `x.attr` or `x[k]`."""
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Tuple | ast.List):
        return [n for element in node.elts for n in _bound_names(element)]
    if isinstance(node, ast.Starred):
        return _bound_names(node.value)
    return []


def test_resolve_target_does_not_rebind_the_table_it_was_asked_for() -> None:
    """Scoped to this function, and the scoping is a decision rather than laziness.

    An estate-wide "no parameter is ever reassigned" gate was written first and measured: it
    condemns six sites that are the same value being transformed under its own name —
    `image = image.convert("RGB")`, `spec = spec.model_copy(update=...)`. Those are idiomatic and
    renaming them would make the code worse. The defect VS-23 names is the OTHER kind: `table` stops
    meaning "the searchable table the caller asked for" and starts meaning "the alignments
    capability's table half", a different value of a different provenance under one name. That
    distinction is not machine-decidable, so the gate names the function the finding names.
    """
    tree = ast.parse(Path(inspect.getfile(target)).read_text())
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "resolve_target")
    params = {a.arg for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)}
    rebound = sorted({name for inner in ast.walk(fn) if isinstance(inner, ast.Assign) for t in inner.targets for name in _bound_names(t) if name in params})
    assert not rebound, f"resolve_target() rebinds {rebound} — after that line the name no longer means the table the caller selected"
