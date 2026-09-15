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


def test_a_GOVERNED_table_is_not_reported() -> None:
    """The half that keeps the category worth reading — a healthy estate must report zero."""
    found = reconcile._ungoverned_tables([("db1$users", "s3://wh")], governed={"db1$users": 2})

    assert found == []


def test_a_table_whose_bucket_maps_to_ZERO_is_reported() -> None:
    """Absent and zero are the same fact arriving two ways, and only one of them is obvious."""
    found = reconcile._ungoverned_tables([("db1$users", "s3://wh")], governed={"db1$users": 0})

    assert [f.table for f in found] == ["db1$users"]


def test_the_finding_names_the_ROOT_so_an_operator_knows_where_to_look() -> None:
    """With several roots in play, the table id alone does not say which bucket holds it."""
    found = reconcile._ungoverned_tables(
        [("a$t", "s3://wh-one"), ("b$t", "s3://wh-two")],
        governed={},
    )

    assert {(f.table, f.root) for f in found} == {("a$t", "s3://wh-one"), ("b$t", "s3://wh-two")}


def test_the_table_scan_keeps_the_WHOLE_id() -> None:
    """`_tables` must not trim the way the namespace scan does, or every table reads as ungoverned.

    A table is recorded as `<namespace><delimiter><table>` and that whole string is the catalog id the
    FGA object is named after. `_top_level_namespaces` deliberately drops everything after the first
    delimiter — reusing that shape here would produce ids matching no tuple at all, and the category
    would fire on a perfectly governed estate.
    """
    import inspect

    # The SIGNATURE, not the source: `_top_level_namespaces` takes a `delimiter` precisely so it can
    # trim, and the table scan must not be able to. A source-text check matches this function's own
    # docstring, which explains the trap — and passed against a body that had the trap.
    assert "delimiter" in inspect.signature(reconcile._top_level_namespaces).parameters, "the premise moved: the namespace scan no longer trims"
    assert "delimiter" not in inspect.signature(reconcile._tables).parameters, "the table scan can trim its ids — every table would read as ungoverned"


def test_it_degrades_with_OPENFGA_rather_than_reporting_the_whole_estate() -> None:
    """Without the tuple scan every table looks ungoverned, so the outage must make it UNAVAILABLE.

    Pinned here as well as in `test_reconcile_report`'s outage test, because the failure is silent and
    spectacular: a report naming every table in the estate as drift, produced by a healthy catalog and
    a dead authorization server.
    """
    import inspect

    source = inspect.getsource(reconcile.run_reconcile) if hasattr(reconcile, "run_reconcile") else ""
    if not source:
        import linecache

        linecache.checkcache(reconcile.__file__)
        source = inspect.getsource(reconcile)

    assert "inputs=(sources.tables_error, sources.tuples_error)" in source, "the category does not guard on the tuple scan"
