"""Authorization tuples on a table the catalog no longer has are REPORTED as their own class.

[[LH-148]]. A warehouse cascade delete destroyed child tables through the native `drop_namespace`
and revoked only the namespace's tuples, so every table it destroyed kept its own — fixed in
`warehouses.py`, but the residue stays until something clears it.

IT WAS VISIBLE ONLY UNDER THE WRONG NAME. The lineage reconcile computes
`unknown_to_graph = governed - graph`, and `governed_tables` is "the table ids carrying at least one
authorization tuple" — so an orphaned tuple with no dataset behind it lands there and pages through
`LineageProvenanceLostOnWrite`, whose words are "a write lost its provenance". Measured 2026-09-20:
121 of that metric's 126 were exactly this, and no drift category named them. Maintenance had
`ghost_projects` and `ghost_warehouses` — an FGA object no registry record claims — and nothing for
tables.

THE EXACT INVERSE OF `ungoverned_tables`, which is why it reuses both its inputs and `_ghosts`: one
asks for a table with no tuples, this asks for tuples with no table, and neither can be answered
without the tuple scan. Without that guard an OpenFGA outage would report the whole estate.

Reported, never gating: the purge cannot reclaim a tuple, so gating storage reclamation on one
would stop the estate for a reason the purge can never resolve.
"""

from __future__ import annotations

from maintenance.services.reconcile import _ghosts


def test_a_tuple_holding_table_the_catalog_lacks_is_named() -> None:
    found = _ghosts("table", {"trackans1$gone": 4}, record_ids={"acme$live"}, exclude=set())

    assert [(g.id, g.tuples) for g in found] == [("trackans1$gone", 4)]
    assert found[0].fga_object == "table:trackans1$gone"


def test_a_live_table_with_tuples_is_not_named() -> None:
    """The control: an ordinary governed table is the normal case and must never read as drift."""
    assert _ghosts("table", {"acme$live": 2}, record_ids={"acme$live"}, exclude=set()) == []


def test_the_count_rides_the_finding() -> None:
    """How MANY tuples outlived the table is what says whether it was one grant or a whole tenant."""
    found = _ghosts("table", {"a$x": 1, "b$y": 9}, record_ids=set(), exclude=set())

    assert {g.id: g.tuples for g in found} == {"a$x": 1, "b$y": 9}
