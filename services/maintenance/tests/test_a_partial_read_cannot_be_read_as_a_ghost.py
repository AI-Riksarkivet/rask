"""A category whose input was read INCOMPLETELY may not justify a revoke.

[[LH-061]]. The repair pass is safe because it only ever revokes tuples on an object that is GONE, and
"gone" is derived by subtraction: a tuple whose id appears in no registry record. That inference holds
only while the record listing is COMPLETE. If the listing is merely partial, every record it failed to
read becomes an object the pass believes does not exist.

THE GUARD THAT EXISTS DOES NOT COVER THIS, which is why this suite is separate from the one asserting
what the pass may touch. `_run_category` refuses a category whose source reported an ERROR
(`tables_error`, `tuples_error`), so an OpenFGA or catalog OUTAGE correctly yields an UNAVAILABLE
category and no findings. A PARTIAL read is a different signal on a different channel: `_tables_across`
returns its unreadable roots alongside the rows it did get, and `_build_sources` files them as
`IncompleteScan(source="catalog:tables:<root>")` while `tables_error` stays None. The category then
runs, against a record set missing exactly those roots.

SO THE FAILURE IS: one warehouse root whose manifest cannot be read for one tick — a transient S3 503,
a credential rotation, a partially-written manifest — and every table under it is classified
`ghost_tables`. Armed, the pass revokes authorization for all of them. The bytes survive and every
grant does not, and the pass is all-or-nothing per object, so recovery means re-granting each tuple by
hand from registries that (measured 2026-09-19: 0 of 97 warehouse records carry `created_by`) do not
record who held them. That is precisely the harm `repair.py`'s own header reserves its strictest
default for — "a wrong revoke is felt by a person holding a grant".

THE SAME SHAPE REACHES THE OTHER TWO REVOCABLE CATEGORIES through `_registry_source`, which appends an
`IncompleteScan` for every record it had to skip. A projects registry that skipped a record makes that
project a `ghost_project`.

REFUSED BY NAME, NOT SILENTLY DROPPED — the contract `_REFUSED` already keeps. An operator reading a
tick that revoked nothing must be able to see that the pass considered the category and declined it
because an input was partial, rather than conclude the estate is clean.
"""

from __future__ import annotations

from maintenance.services import repair
from maintenance.services.reconcile import GhostObject, IncompleteScan, ReconcileReport


def _report(*incomplete: IncompleteScan) -> ReconcileReport:
    """Ghosts in all three revocable categories, plus whatever partial reads the case supplies."""
    return ReconcileReport(
        checked_at="2026-09-20T00:00:00Z",
        ghost_projects=[GhostObject(kind="project", id="p1", fga_object="project:p1", tuples=3)],
        ghost_warehouses=[GhostObject(kind="warehouse", id="w1", fga_object="warehouse:w1", tuples=2)],
        ghost_tables=[GhostObject(kind="table", id="ns$t1", fga_object="table:ns$t1", tuples=4)],
        incomplete=list(incomplete),
    )


def test_a_COMPLETE_report_still_plans_every_revoke() -> None:
    """The control. Without it every assertion below could pass by refusing everything always."""
    planned, refused = repair.plan_repair(_report())

    assert {p.fga_object for p in planned} == {"project:p1", "warehouse:w1", "table:ns$t1"}
    assert refused == {}


def test_an_unreadable_TABLE_root_does_not_make_its_tables_ghosts() -> None:
    """The live path: `catalog:tables:<root>` is an IncompleteScan, never an error, so `ghost_tables`
    runs against a record set missing every table under that root."""
    planned, refused = repair.plan_repair(_report(IncompleteScan(source="catalog:tables:s3://acme-wh", reason="manifest unreadable: 503")))

    assert "table:ns$t1" not in {p.fga_object for p in planned}, (
        "a table under a root the scan could not read was planned for revoke — a transient manifest "
        "failure would strip authorization from every table in that warehouse"
    )
    assert "ghost_tables" in refused, f"the refusal is not reported, so the tick reads as clean: {refused}"
    assert "s3://acme-wh" in refused["ghost_tables"], f"the refusal does not name the partial source: {refused['ghost_tables']}"


def test_the_OTHER_categories_still_run_when_only_tables_were_partial() -> None:
    """A partial read invalidates the category it feeds and no other. Refusing all three on any
    incompleteness would make the pass unusable on an estate that always carries one — which is the
    state this estate is actually in."""
    planned, _ = repair.plan_repair(_report(IncompleteScan(source="catalog:tables:s3://acme-wh", reason="manifest unreadable: 503")))

    assert {p.fga_object for p in planned} == {"project:p1", "warehouse:w1"}


def test_a_skipped_PROJECT_record_does_not_make_that_project_a_ghost() -> None:
    """`_registry_source` files a skipped record the same way, and the inference is identically wrong."""
    planned, refused = repair.plan_repair(_report(IncompleteScan(source="registry:projects", reason="1 record unreadable")))

    assert "project:p1" not in {p.fga_object for p in planned}
    assert "ghost_projects" in refused


def test_a_skipped_WAREHOUSE_record_does_not_make_that_warehouse_a_ghost() -> None:
    planned, refused = repair.plan_repair(_report(IncompleteScan(source="registry:warehouses", reason="1 record unreadable")))

    assert "warehouse:w1" not in {p.fga_object for p in planned}
    assert "ghost_warehouses" in refused


def test_an_UNRELATED_partial_read_refuses_nothing() -> None:
    """The precision check. The storage scans produce most of this estate's IncompleteScans and feed
    none of the revocable categories, so treating incompleteness as a global stop would refuse every
    tick for a reason that cannot affect the inference."""
    planned, refused = repair.plan_repair(
        _report(
            IncompleteScan(source="storage:datasets", reason="depth limit reached"),
            IncompleteScan(source="storage:buckets", reason="listing truncated"),
            IncompleteScan(source="catalog:namespaces:s3://acme-wh", reason="manifest unreadable"),
        )
    )

    assert {p.fga_object for p in planned} == {"project:p1", "warehouse:w1", "table:ns$t1"}
    assert refused == {}


def test_the_ARMED_pass_honours_the_refusal_too() -> None:
    """`plan_repair` is not the only door — `repair_drift_sync` re-plans, and a guard that lived only
    in the planner would be bypassed by the path that actually deletes."""
    from maintenance.core.config import MaintenanceSettings

    settings = MaintenanceSettings.model_validate({"MAINTENANCE_DRIFT_REPAIR_ENABLED": True, "MAINTENANCE_DRIFT_REPAIR_DRY_RUN": False})
    seen: list[str] = []

    def _revoke(obj: str) -> list[object]:
        seen.append(obj)
        return []

    out = repair.repair_drift_sync(
        settings,
        report=_report(IncompleteScan(source="catalog:tables:s3://acme-wh", reason="manifest unreadable: 503")),
        revoke=_revoke,
    )

    assert "table:ns$t1" not in seen, f"the armed path revoked a table its record listing never read: {seen}"
    assert "ghost_tables" in out.refused
