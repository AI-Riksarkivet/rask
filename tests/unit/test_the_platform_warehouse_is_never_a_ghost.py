"""The estate's own default warehouse has no registry record BY DESIGN, and must never be reported as drift.

[[LH-055]]. `reconcile` compares the OpenFGA tuple store against the control-root registries and reports
a warehouse that has tuples and no record as a ghost. `warehouse:lance_catalog` is exactly that shape —
it is the platform's own structural root, minted by the bootstrap hook rather than by the warehouse API
— so it is excluded.

WHAT THIS GATES IS THE EXCLUSION'S KEY, not the exclusion. Keying it on the estate root's TYPE
(`{root_id} if root_type == "warehouse"`) names this warehouse only while `fga_root_object` and
`fga_default_warehouse_object` happen to hold the same value. Once the estate coordinate is
`estate:rask`, `root_type` is no longer `"warehouse"` and the default warehouse becomes a permanent
ghost on every 300s reconcile tick. A report that always carries a known-good finding is a report
nobody reads, which is how the finding that matters gets missed.

MEASURED, not hypothetical: the live sweep's ghost scan reads a real `warehouse:lance_catalog` carrying
17 tuples and no registry record (2026-09-22).
"""

from __future__ import annotations

from maintenance.services.reconcile import DEFAULT_FGA_WAREHOUSE_OBJECT, Sources, TupleScan, build_report


#: The platform warehouse's bare id, DERIVED rather than spelled out. The object is
#: `warehouse:lance_catalog` with an UNDERSCORE, so a hyphenated literal makes every `not in` assertion
#: here true whatever the detector does — a vacuous pass that only a mutation check finds, because the
#: test itself stays green. Measured 2026-09-22: that is exactly what a typed literal did.
PLATFORM_ID = DEFAULT_FGA_WAREHOUSE_OBJECT.split(":", 1)[1]
#: A tenant warehouse with tuples and no record — a REAL ghost, and the control every case below needs.
TENANT_ID = "acme-bucket"


def _ghost_ids(root: str) -> set[str]:
    """The ghost-warehouse ids `build_report` returns for a store holding both, with no registry records."""
    sources = Sources(
        tuples=TupleScan(counts_by_type={"warehouse": dict.fromkeys((PLATFORM_ID, TENANT_ID), 3)}),
        project_records=[],
        warehouse_records=[],
        bindings=[],
    )
    report = build_report(sources, warehouses_enabled=True, platform_buckets=set(), fga_root_object=root)
    return {g.id for g in (report.ghost_warehouses or [])}


def test_the_default_warehouse_is_excluded_when_the_root_is_the_estate() -> None:
    """The case the split creates, and the one with no local reproduction before the repoint."""
    ghosts = _ghost_ids("estate:rask")
    assert TENANT_ID in ghosts, "the ghost detector found nothing — every assertion here would be vacuous"
    assert PLATFORM_ID not in ghosts, (
        "the platform's own default warehouse is reported as a ghost once `fga_root_object` names an "
        f"`estate:` — every reconcile tick would carry this finding forever. Reported: {sorted(ghosts)}"
    )


def test_the_exclusion_survives_a_warehouse_shaped_root() -> None:
    """Both settings may still name a `warehouse:` (they do today), and neither may be a ghost."""
    ghosts = _ghost_ids(DEFAULT_FGA_WAREHOUSE_OBJECT)
    assert TENANT_ID in ghosts, "the ghost detector found nothing — this case would be vacuous"
    assert PLATFORM_ID not in ghosts
