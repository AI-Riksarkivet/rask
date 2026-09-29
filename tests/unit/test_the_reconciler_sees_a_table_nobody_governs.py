"""A catalog table with no authorization tuples is DRIFT, and until now nothing in the estate said so.

[[LH-164]]. Every `table` relation in `model.fga` resolves through a direct tuple or `X from parent`,
so a table carrying none denies EVERY principal — including the identity that created it. The
reconciler compared the registry against storage and against FGA at the project and warehouse rungs
and was blind at the table rung, so the first door that ever asked such a table a permission question
was the maintenance sweep's `can_maintain`. A governance hole surfaced as a compaction statistic, on
2026-09-15, in a category that already carried 315 refusals which were correct.

NO TUPLES AT ALL is the test, and it is deliberately narrower than "missing its parent edge". That is
the shape actually measured, it is unambiguous from the single whole-store scan the reconciler already
performs, and it cannot be produced by a partial grant — whereas "has an owner but no parent" would
need that scan to carry relations as well as counts. A narrower detector that never cries wolf beats a
broader one nobody trusts.

NON-GATING, by the module's own rule rather than by preference: a category gates the #79 purge only if
it is a STORAGE fact with a door that clears it. This is an authz fact — the bytes are intact and the
purge cannot touch them differently for it — and no endpoint clears it directly. `NON_GATING_CATEGORIES`
already argues that an unreachable gate is not a safety property; this obeys that argument instead of
re-opening it.
"""

from __future__ import annotations

from maintenance.services import reconcile


def test_the_category_is_declared_and_does_not_gate() -> None:
    assert "ungoverned_tables" in reconcile.CATEGORIES
    assert "ungoverned_tables" in reconcile.NON_GATING_CATEGORIES


def test_a_table_with_no_tuples_is_reported() -> None:
    """THE DEFECT: five of these were live and nothing in the product named them."""
    found = reconcile._ungoverned_tables([("lakehouse$silver$features", "s3://lakehouse-wh")], governed={})

    assert [(f.table, f.root) for f in found] == [("lakehouse$silver$features", "s3://lakehouse-wh")]
