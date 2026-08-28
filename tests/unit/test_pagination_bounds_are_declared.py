"""A paginated route must declare its real bound in the signature, not bury it in the body.

open_fastapi-audit — "No shared `PaginationParams` dependency or `Page[Item]` envelope: eight
page-parameter vocabularies, seven ceilings, and six routes whose real bound is invisible in
OpenAPI".

THIS GATES THE HALF THAT ACTUALLY FAILS. The finding's headline is the missing shared dep, and that
lands separately (`service_kit.pagination`). But the measured harm is the second half: six routes
clamp in the handler or the service layer — `min(max(limit, 0), 200)`, `max(1, min(limit, _MAX_LIMIT))`
— so the OpenAPI the gateway aggregates, and the frontend generates a typed client from, advertises an
UNBOUNDED integer while the real bound sits three call frames away.

That is not cosmetic. It is precisely how the genuinely unbounded routes went unnoticed: a caller
reading the schema cannot tell a bounded route from an unbounded one, so `ingest limit`,
`viewer graph limit` and `catalog history limit` all read identically when only the last is actually
uncapped.

TWO CLASSES, held to different rules, because the reference does:

* **Size** (`limit`, `page_size`, `per_page`, `n`) — how much comes back. `Field(le=…)` is the
  reference's own answer to "page-size with no upper bound"; without it `?limit=999999` is a
  memory/bandwidth request the schema says is legal.
* **Position** (`page`, `offset`) — how far in. `ge` only, here: bounding DEPTH is the `MAX_OFFSET`
  guard, which is its own finding and its own shape (a computed refusal with a message pointing at
  keyset, not an `le` that would cap the corpus size).

The names are a LIST and not derived, deliberately: the vocabulary being fragmented across eight
spellings is the finding, so the list is the inventory of what to converge, and a route inventing a
ninth spelling should be added here rather than silently exempt.
"""

from __future__ import annotations

import ast
import pathlib

import pytest


REPO = pathlib.Path(__file__).resolve().parents[2]

SIZE_PARAMS = frozenset({"limit", "page_size", "per_page", "n"})
POSITION_PARAMS = frozenset({"page", "offset"})

#: The Lance Namespace spec's own wire contract, which the finding says explicitly not to retrofit:
#: `page_token`/`limit` on the catalog's list endpoints is the spec shape, not local drift.
SPEC_SHAPED = frozenset({"list_tables", "list_namespaces"})


def _routes() -> list[tuple[pathlib.Path, ast.FunctionDef | ast.AsyncFunctionDef]]:
    found = []
    for path in (REPO / "services").rglob("*.py"):
        if "/tests/" in path.as_posix():
            continue
        tree = ast.parse(path.read_text(errors="ignore"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if any(
                isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in {"get", "post", "put", "patch", "delete"}
                for d in node.decorator_list
            ):
                found.append((path, node))
    return found


def _query_kwargs(annotation: ast.expr | None) -> dict[str, ast.expr] | None:
    """The `Query(...)` keywords inside `Annotated[..., Query(...)]`, or None if there is no Query."""
    if annotation is None or not isinstance(annotation, ast.Subscript):
        return None
    if not (isinstance(annotation.value, ast.Name) and annotation.value.id == "Annotated"):
        return None
    parts = annotation.slice.elts if isinstance(annotation.slice, ast.Tuple) else [annotation.slice]
    for part in parts:
        if isinstance(part, ast.Call) and isinstance(part.func, ast.Name) and part.func.id == "Query":
            return {kw.arg: kw.value for kw in part.keywords if kw.arg}
    return None


def _paging_params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[str, ast.expr | None]]:
    args = [*fn.args.args, *fn.args.kwonlyargs]
    return [(a.arg, a.annotation) for a in args if a.arg in SIZE_PARAMS | POSITION_PARAMS]


_SITES = [(path, fn, name, annotation) for path, fn in _routes() if fn.name not in SPEC_SHAPED for name, annotation in _paging_params(fn)]

assert _SITES, "no paginated route parameter was found — this gate would pass vacuously"


@pytest.mark.parametrize(
    ("path", "fn", "name", "annotation"),
    _SITES,
    ids=[f"{p.stem}:{f.name}:{n}" for p, f, n, _ in _SITES],
)
def test_the_bound_is_in_the_signature(path: pathlib.Path, fn: ast.FunctionDef, name: str, annotation: ast.expr | None) -> None:
    """What the schema says must be what the route enforces."""
    where = f"{path.relative_to(REPO)}::{fn.name}({name})"
    kwargs = _query_kwargs(annotation)
    assert kwargs is not None, (
        f"{where} is a bare parameter with no `Query(...)`, so OpenAPI advertises an unbounded integer "
        "— a caller cannot tell this route from a genuinely uncapped one"
    )
    assert "ge" in kwargs, f"{where} declares no lower bound"
    if name in SIZE_PARAMS:
        assert "le" in kwargs, (
            f"{where} declares no `le`, so `?{name}=999999` is legal by the schema. If the real ceiling is "
            "applied in the body, move it here — the reference's answer to page-size with no upper bound."
        )
