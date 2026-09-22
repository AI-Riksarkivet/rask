"""A catalog record pointing at a location that holds nothing is reported as drift.

[[LH-192]]. The reconcile report compares the catalog against AUTHZ in both directions —
`_ungoverned_tables` ("catalog tables carrying NO authorization tuples at all") and its stated inverse
`_ghosts` — but against STORAGE in only one: `_unregistered_datasets` finds bytes with no record, and
nothing finds a record with no bytes.

MEASURED ON THE LIVE ESTATE 2026-09-22, and it is the cascade HEAD:
`describe bronze$events` answers 200 with `location = s3://lance-catalog/medallion/bronze`, and that
path does not exist. So a policy set on it polices no bytes, a protection record guards nothing, and an
FGA grant keys off a table whose data is not there — while the drift report ran clean over it.

THE CHECK COSTS NOTHING NEW, which is why it belongs on the walk that already exists rather than in a
probe of its own. `_orphan_category` already holds both sides: `discover_datasets` gives it every
dataset URI in every scannable bucket, and `lance_docs/namespace.md:968-976` records `location` as a
column of `__manifest` that the reconciler's own read simply does not select.

DECLARED-ONLY TABLES ARE EXCLUDED BY THE SCHEMA, not by an allowlist. The spec makes `location`
nullable and present "only for tables" with storage, and a declared-only table is "reserved, no storage
yet" (`GET /v1/table`'s `include_declared=false`). A record with no location has nothing to resolve, so
it cannot be a finding — which is what keeps this category from reporting every reservation as a defect.

BOTH SPELLINGS OF A LOCATION ARE RESOLVED. The spec calls it a "relative path to the table directory
within the root", while the live catalog answers an ABSOLUTE `s3://…` for a table registered through
`register_written_dataset`. A detector that understood only one would either miss every explicitly
registered table or mis-resolve every ordinary one.
"""

from __future__ import annotations

from maintenance.services.reconcile import _absent_datasets


ROOT = "s3://lance-catalog"


def test_a_record_whose_location_is_absent_is_reported() -> None:
    found = _absent_datasets(
        registered={"bronze$events": (ROOT, "s3://lance-catalog/medallion/bronze")},
        discovered={"s3://lance-catalog/bronze/pages"},
        trashed=set(),
    )
    assert [f.table for f in found] == ["bronze$events"]
    assert found[0].location == "s3://lance-catalog/medallion/bronze"


def test_a_record_whose_bytes_are_present_is_not() -> None:
    """The control. Without it the assertion above passes on a detector that reports everything."""
    assert (
        _absent_datasets(
            registered={"bronze$pages": (ROOT, "s3://lance-catalog/bronze/pages")},
            discovered={"s3://lance-catalog/bronze/pages"},
            trashed=set(),
        )
        == []
    )


def test_a_RELATIVE_location_resolves_against_its_root() -> None:
    """`lance_docs/namespace.md:974` — "Relative path to the table directory within the root"."""
    assert _absent_datasets(registered={"ns$t": (ROOT, "a1b2c3d4_ns$t")}, discovered={f"{ROOT}/a1b2c3d4_ns$t"}, trashed=set()) == []
    missing = _absent_datasets(registered={"ns$t": (ROOT, "a1b2c3d4_ns$t")}, discovered=set(), trashed=set())
    assert [f.location for f in missing] == [f"{ROOT}/a1b2c3d4_ns$t"], "a relative location must be reported RESOLVED, or nobody can go and look"


def test_a_DECLARED_ONLY_table_carries_no_location_and_is_never_a_finding() -> None:
    """The false positive the schema itself rules out: `location` is nullable, "only for tables"."""
    assert _absent_datasets(registered={"ns$reserved": (ROOT, None)}, discovered=set(), trashed=set()) == []


def test_a_DROPPED_table_whose_bytes_await_the_purge_is_not_drift() -> None:
    """Same exclusion `_unregistered_datasets` makes, in the other direction: a trashed table's record
    is gone by definition while its bytes remain, so neither side of the pair is a defect."""
    assert (
        _absent_datasets(
            registered={"ns$dropped": (ROOT, "s3://lance-catalog/gone")},
            discovered=set(),
            trashed={"s3://lance-catalog/gone"},
        )
        == []
    )
