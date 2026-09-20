"""Trash pointing at a bucket no warehouse claims is REPORTED, not silent.

[[LH-102]]. A trash record names the location of a dropped table so it can be undropped inside the
retention window. Deleting its warehouse removes the registry entry that made that location
maintainable — so the record outlives it, promising a recovery the estate cannot perform and holding
a purge slot it can never use.

Measured live 2026-09-20: a full non-destructive census of the expired queue read
`due=877 would_purge=8 refused=869`, every one of the 869 refused `outside the maintained estate`,
across 45 deleted warehouses. None of it appeared in the drift report — the reconcile had nine
categories and trash was in none of them, so the only surface that knew was a purge refusal nobody
reads.

REPORTED, NEVER GATING, and that is not timidity. The gate's job is to prove the storage state is
understood BEFORE the purge spends its delete permission; gating on this would deadlock, because the
purge is the thing that cannot reclaim these and no amount of waiting changes that. It joins
`NON_GATING_CATEGORIES` beside the other two that are counted, named, and excluded from `total`.

Keyed on the BUCKET rather than the warehouse id, because the two are separate fields on a warehouse
record — keying on the id would report a record as orphaned while it is perfectly reachable.
"""

from __future__ import annotations

from maintenance.services.reconcile import _orphaned_trash


def _record(canonical_id: str, location: str) -> dict[str, str]:
    return {"id": canonical_id, "kind": "table", "location": location}


def test_trash_in_an_unclaimed_bucket_is_reported() -> None:
    found = _orphaned_trash([_record("ns$gone", "s3://dead-wh/abc_ns$gone")], claimed={"live-wh"})

    assert [o.id for o in found] == ["ns$gone"]
    assert found[0].location == "s3://dead-wh/abc_ns$gone"


def test_trash_in_a_claimed_bucket_is_not_reported() -> None:
    """The control: a live warehouse's trash is ordinary, recoverable, and must not be named as drift."""
    found = _orphaned_trash([_record("ns$fine", "s3://live-wh/abc_ns$fine")], claimed={"live-wh"})

    assert found == []


def test_the_bucket_decides_not_the_warehouse_id() -> None:
    """A warehouse may name a bucket that is not its own id; keying on the id would misreport it."""
    found = _orphaned_trash([_record("ns$fine", "s3://tenant-bucket/abc_ns$fine")], claimed={"tenant-bucket"})

    assert found == []


def test_a_location_naming_no_bucket_is_left_alone() -> None:
    """Malformed is not orphaned, and a report whose value is that it does not guess must not guess."""
    found = _orphaned_trash([_record("ns$odd", "/not/an/s3/uri"), _record("ns$empty", "")], claimed=set())

    assert found == []
