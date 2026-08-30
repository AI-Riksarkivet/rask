"""catalog-api-03 (primary site) — the Arrow create door ROUTES; the orchestration lives in services/.

``schemas.py``'s own header states the intent for this plane — "the endpoints stay routing-only" — and
catalog-api-05 moved every wire model out for exactly that reason. The multi-step work stayed. ``POST
/v1/table/{id}/create`` ran the whole governed create INLINE in the handler: shape guards, the parent
and trash round trips, the format guard, the lineage-metadata inject, the pre-existence probe and its
owner-tier drop gate, the data-plane write, the ACL revoke, the seed-with-compensation, the schema
read-back, the lineage emit and the control emit — fourteen awaits and fifty statements, against a
module median of two.

That is where the compensation rules live, and a rule that can only be exercised through an HTTP door
is a rule nothing can unit-test: ``test_compensation_matrix_never_drops_a_replaced_or_kept_table``
already had to reach into a private helper because "the Overwrite-of-existing arm can't be driven in
this harness".

Calibrated to this module, not to a taste: every other handler in ``data.py`` is at four awaits or
fewer, and the two that come closest (``merge_insert``, ``commit_fragments``) already delegate their
work to ``catalog.services``.
"""

from __future__ import annotations

import ast
import pathlib


_DATA = pathlib.Path(__file__).resolve().parents[1] / "src" / "catalog" / "api" / "v1" / "endpoints" / "data.py"

#: What a routing-only handler spends: its own guards plus the delegated call. Every non-create handler
#: in this module already sits at or below it.
_BUDGET = 4


def _handlers() -> list[tuple[str, int, int]]:
    tree = ast.parse(_DATA.read_text())
    return [
        (fn.name, sum(1 for n in ast.walk(fn) if isinstance(n, ast.Await)), sum(1 for n in ast.walk(fn) if isinstance(n, ast.stmt)))
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
        and "router" in ast.dump(ast.Module(body=[ast.Expr(d) for d in fn.decorator_list], type_ignores=[]))
    ]


def test_the_walk_sees_the_data_plane_doors() -> None:
    names = {name for name, _, _ in _handlers()}
    assert {"create_table", "merge_insert_into_table", "query_table"} <= names, sorted(names)


def test_no_data_route_orchestrates() -> None:
    over = [f"{name}: {awaits} awaits, {stmts} statements" for name, awaits, stmts in _handlers() if awaits > _BUDGET]
    assert not over, (
        f"data-plane routes running more than {_BUDGET} awaits of orchestration — the steps belong in "
        "catalog.services, where they can be driven without an HTTP door:\n  " + "\n  ".join(over)
    )


def test_the_governed_create_is_reachable_without_a_route() -> None:
    """The point of the move: the compensation/seed/emit sequence must be callable directly."""
    from catalog.services.table_create import create_governed_table

    assert callable(create_governed_table)
