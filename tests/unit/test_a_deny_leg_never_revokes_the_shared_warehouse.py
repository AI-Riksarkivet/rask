"""An e2e deny leg must not revoke `owner` on the warehouse the LIVE cascade shares.

[[LH-152]]. The medallion's FGA gates are proven by REVOKING the deployed cascade's own tuples and
watching the cascade stop. `_owner_tuples` deletes `owner` at three levels — table, namespace and
WAREHOUSE — because owner outranks the writer/validator rung the deny aims at, so a revoke that misses
one measures an ungated cascade.

THE WAREHOUSE LEVEL IS THE PROBLEM, AND IT IS NOT FIXED BY `try`/`finally`. Its sibling gate
(`test_an_e2e_revoke_is_always_inside_its_restore`) closes every ORDINARY exit and says plainly that it
cannot close the crash window: a SIGKILL between the delete and the restore strips the estate
permanently. While `WAREHOUSE` resolves to the shared platform default, that residual risk is taken
against the estate everything else runs on.

THE FILE ALREADY KNOWS HOW TO AVOID IT. `WAREHOUSE` is resolved from `LANCE_E2E_FGA_WAREHOUSE`, then
`LANCE_E2E_WAREHOUSE`, then a project-derived `warehouse:<project>-wh` — and only falls back to
`warehouse:lance_catalog`, which is the platform's own. Pointed at a probe warehouse the same legs
prove the same property while the worst case costs a throwaway tenant.

SO THE GUARD IS A PRECONDITION, NOT A REWRITE: a deny leg refuses to start when the warehouse it would
strip is the shared one. Failing rather than SKIPPING, deliberately — this estate has been bitten by
`pytest.skip` standing in for a gate ("a silent hole, since a skipped test reads as a passing suite",
`.dagger/charts.go`), and a leg that quietly does not run is how the cascade's authz stops being
proven at all.

A STATIC GATE, for the reason its sibling gives: the suite is `-m e2e`, needs a deployed stack and is
deselected from `make test`, so what a commit can get wrong is the file's SHAPE.
"""

from __future__ import annotations

import ast
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SUITE = REPO / "tests/e2e-py/test_governed_union_e2e.py"

#: The helper that walks up to the warehouse level. A leg calling it is a leg that can strip the estate.
_WAREHOUSE_REVOKE = "_owner_tuples"
#: The precondition that refuses to run against the platform's own warehouse.
_GUARD = "refuse_shared_warehouse"


def _called_names(node: ast.AST) -> set[str]:
    return {n.func.id for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


def test_every_leg_that_revokes_a_warehouse_owner_refuses_the_shared_one() -> None:
    """The invariant. A leg may strip a probe warehouse; it may not strip the platform's."""
    tree = ast.parse(SUITE.read_text(encoding="utf-8"))
    offenders: list[str] = []
    walkers = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        called = _called_names(node)
        if _WAREHOUSE_REVOKE not in called:
            continue
        walkers += 1
        if _GUARD not in called:
            offenders.append(f"{node.name} (line {node.lineno})")

    assert walkers, f"no function in {SUITE.name} calls `{_WAREHOUSE_REVOKE}` — the walk proves nothing"
    assert offenders == [], (
        f"{offenders} revoke `owner` up to the WAREHOUSE and never call `{_GUARD}()`. A crash between "
        "the delete and the restore strips the estate the whole medallion runs on — point the leg at a "
        "probe warehouse (LANCE_E2E_WAREHOUSE / LANCE_E2E_PROJECT) instead."
    )


def test_the_guard_refuses_the_platform_warehouse_and_admits_a_probe_one() -> None:
    """The behaviour, not just its presence — a guard that cannot refuse is not a guard.

    Driven over the real function rather than a re-implementation, so a change to what counts as
    "shared" is caught here instead of being described twice.
    """
    import importlib.util
    import sys

    # The suite's own directory goes on the path first: it imports siblings (`promotion_review`) by
    # bare name, which resolve relative to it and not to `tests/unit`. Loading the real module rather
    # than re-implementing the guard is the point — a stand-in cannot see a change to what counts as
    # "shared".
    sys.path.insert(0, str(SUITE.parent))
    try:
        spec = importlib.util.spec_from_file_location("_e2e_governed_union", SUITE)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SUITE.parent))

    guard = getattr(module, _GUARD)
    # The platform's own warehouse — the default when nothing is configured.
    with __import__("pytest").raises(Exception):
        guard("warehouse:lance_catalog")
    # A probe tenant's warehouse — admitted, because stripping it costs a throwaway.
    guard("warehouse:probe-wh")
