"""Every `branch` query parameter on a catalog route says what that door does with one.

`branch` is the parameter this estate has been burned by twice — `ff9604be` fixed a branch-targeted
`drop_table_index` that destroyed MAIN's index and answered 200 — and the doors do NOT agree about it:
some honour the ref, some refuse it, and which is which is a property of what the verb can reach. A
caller reading the OpenAPI saw the same bare `string` on all fourteen.

DERIVED FROM THE SIGNATURES, not written door by door, for the same reason
`test_the_maintenance_doors_refuse_a_branch_they_cannot_honour` is derived from the mounted routes: a
fifteenth verb inherits this without an edit. The convention itself is the `fastapi` skill's
(`references/core-conventions.md` — "Always prefer the `Annotated` style for parameter and dependency
declarations"), and it was already the estate's in five doors before this gate existed.

The description must be NON-EMPTY, never merely present. A `Query()` with no description satisfies the
letter of the convention and leaves the OpenAPI exactly as uninformative as the bare parameter it
replaced.
"""

from __future__ import annotations

import ast
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
_ENDPOINTS = REPO / "services/catalog/src/catalog/api/v1/endpoints"


def _route_handlers(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Functions carrying an `@router.<verb>` decorator — the mounted doors, not their helpers."""
    handlers = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) and target.value.id == "router":
                handlers.append(node)
                break
    return handlers


def _branch_params() -> list[tuple[str, str, ast.expr | None]]:
    """``(module, handler, annotation)`` for every route handler declaring `branch`."""
    found = []
    for path in sorted(_ENDPOINTS.glob("*.py")):
        tree = ast.parse(path.read_text())
        for handler in _route_handlers(tree):
            for arg in handler.args.args + handler.args.kwonlyargs:
                if arg.arg == "branch":
                    found.append((path.name, handler.name, arg.annotation))
    return found


def test_the_walk_finds_the_doors() -> None:
    """Without this the assertions below could hold over an empty set."""
    assert len(_branch_params()) >= 10, "the route walk stopped finding `branch` doors — the gate would pass vacuously"


def test_every_branch_parameter_is_annotated() -> None:
    """A bare `str | None = None` renders as an undocumented `string` in the OpenAPI."""
    bare = [f"{module}::{handler}" for module, handler, annotation in _branch_params() if annotation is None or "Annotated" not in ast.unparse(annotation)]

    assert not bare, f"these doors declare `branch` without `Annotated[..., Query(...)]`: {bare}"


def test_every_branch_parameter_carries_a_NON_EMPTY_description() -> None:
    """The point is what the door DOES with a ref, which an empty `Query()` does not say."""
    undocumented = []
    for module, handler, annotation in _branch_params():
        rendered = ast.unparse(annotation) if annotation is not None else ""
        if "description=" not in rendered or 'description=""' in rendered:
            undocumented.append(f"{module}::{handler}")

    assert not undocumented, f"these doors annotate `branch` but say nothing about it: {undocumented}"
