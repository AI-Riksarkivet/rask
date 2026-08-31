"""A filtered listing must EXHAUST the backend, not read page one and paginate what it got.

THE SHAPE. `list_namespaces` and `list_tables` make ONE unpaginated `native.call` and then apply the
estate's own keyset cursor to the filtered names. Applying the cursor after the filter is CORRECT and
deliberate — a backend `limit` truncates before the filter, so `limit=10` could answer 2 and hand out a
cursor that skips everything the filter removed.

What is not correct is reading only the first page. Against a backend that genuinely paginates, the
route answers page one, filters it, and hands back a cursor that looks complete — so a caller sees a
short list with no indication anything was dropped. Silent truncation of an authorization-filtered
listing is the worst version of this: the missing rows look exactly like rows the caller may not read.

`_collect_descendants` in the same module already does it correctly, looping to `_MAX_LIST_PAGES` and
logging when the ceiling is hit with a token outstanding. The two walkers over the same tree disagreed,
and the destructive one was the careful one.

I introduced the second instance myself when fixing the sibling-name disclosure — mirroring
`list_tables` faithfully, including this.
"""

from __future__ import annotations

import ast
import pathlib


ENDPOINTS = pathlib.Path(__file__).resolve().parents[3] / "services/catalog/src/catalog/api/v1/endpoints"

#: Routes that filter a listing per item and therefore must drain the backend themselves.
FILTERED_LISTINGS = {"list_namespaces", "list_tables"}


def _calls_native_list_in_a_loop(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Whether every `native.call(..., "list_*")` in this function sits inside a loop."""
    # THE WHOLE CALL, not just its func. These go through `run_in_threadpool(native.call, ns, "list_x",
    # req)`, so `node.func` is the threadpool helper and a check on it finds nothing — this test passed
    # vacuously on its first run, which is the exact shape `test_checks_are_reachable` exists to catch.
    listing_calls: list[ast.AST] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        rendered = ast.unparse(node)
        if "native.call" in rendered and ('"list_' in rendered or "'list_" in rendered):
            listing_calls.append(node)
    if not listing_calls:
        return True
    in_loop: set[int] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.For | ast.While):
            for inner in ast.walk(node):
                in_loop.add(id(inner))
    return all(id(call) in in_loop for call in listing_calls)


def test_a_filtered_listing_exhausts_the_backend() -> None:
    offenders: list[str] = []
    for path in sorted(ENDPOINTS.rglob("*.py")):
        for fn in ast.walk(ast.parse(path.read_text())):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if fn.name not in FILTERED_LISTINGS:
                continue
            if not _calls_native_list_in_a_loop(fn):
                offenders.append(f"{path.name}::{fn.name}")

    assert not offenders, (
        "these routes filter a listing per item but read only the backend's FIRST page, so a paginating "
        "backend silently drops rows the caller may read:\n  " + "\n  ".join(offenders)
    )
