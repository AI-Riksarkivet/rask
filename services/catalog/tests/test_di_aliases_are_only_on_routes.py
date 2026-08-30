"""catalog-api-15 — an ``Annotated[..., Depends(...)]`` alias appears only where FastAPI resolves it.

``CurrentToken`` is ``Annotated[IDToken | None, Depends(authenticate)]``. On a route parameter that is
a contract FastAPI honours. On a plain helper the endpoint calls itself it is decoration: the
``Depends`` is inert, nothing authenticates, and the annotation tells a reader the opposite of what
happens — the value is whatever the caller passed. Thirteen non-route helpers carried one, and the two
that mattered (``_access_list``, ``_access_mutate``) sit on the authorization surface, where "this
parameter is the verified token" is exactly the claim you must not make loosely.

Helpers state the PLAIN type they actually receive (``IDToken | None``, ``Settings``,
``UserStateStore``). Functions registered through ``Depends(...)`` are exempt — FastAPI does resolve
those, so the aliases there are real.
"""

from __future__ import annotations

import ast
import pathlib


_ENDPOINTS = pathlib.Path(__file__).resolve().parents[1] / "src" / "catalog" / "api" / "v1" / "endpoints"

#: The `Annotated[..., Depends(...)]` aliases the catalog injects with.
_ALIASES = {
    "CurrentToken",
    "SettingsDep",
    "NamespaceDep",
    "StorageOptionsDep",
    "FgaClientDep",
    "LineageEmitterDep",
    "ControlEmitterDep",
    "VendorDep",
    "UserStateStoreDep",
    "EstateFgaClient",
}


def _offences(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text())
    # Names handed to `Depends(...)` anywhere in the module: FastAPI resolves these, aliases are real.
    injected = {
        node.args[0].id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Depends" and node.args and isinstance(node.args[0], ast.Name)
    }
    found: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        decorated = ast.dump(ast.Module(body=[ast.Expr(d) for d in fn.decorator_list], type_ignores=[]))
        if "router" in decorated or fn.name in injected:
            continue
        aliases = [
            f"{a.arg}: {a.annotation.id}" for a in [*fn.args.args, *fn.args.kwonlyargs] if isinstance(a.annotation, ast.Name) and a.annotation.id in _ALIASES
        ]
        if aliases:
            found.append(f"{path.name}:{fn.lineno} {fn.name}({', '.join(aliases)})")
    return found


def test_the_walk_sees_the_endpoint_plane() -> None:
    modules = [p for p in _ENDPOINTS.glob("*.py") if p.name != "__init__.py"]
    assert len(modules) > 10, f"only {len(modules)} endpoint modules — the walk is not seeing the plane"


def test_the_alias_set_is_still_what_the_catalog_injects_with() -> None:
    """Guards the gate itself: a renamed alias must not silently empty the check."""
    dependencies = (_ENDPOINTS.parents[1] / "dependencies.py").read_text()
    live = {name for name in _ALIASES if f"{name} = Annotated[" in dependencies}
    assert len(live) >= 6, f"only {sorted(live)} of the alias set still exist in api/dependencies.py — update the gate"


def test_no_non_route_helper_annotates_a_parameter_with_a_Depends_alias() -> None:
    offences = [o for path in sorted(_ENDPOINTS.glob("*.py")) for o in _offences(path)]
    assert not offences, (
        "Depends aliases on functions FastAPI never resolves — annotate the plain type the caller "
        "actually passes (IDToken | None, Settings, UserStateStore):\n  " + "\n  ".join(offences)
    )
