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

import pytest

from maintenance.services.reconcile import ReconcileReport, Sources, _absent_datasets, _registration_drift


ROOT = "s3://lance-catalog"


@pytest.mark.parametrize(
    ("registered", "discovered", "trashed", "absent"),
    [
        pytest.param(
            {"bronze$events": (ROOT, "s3://lance-catalog/medallion/bronze")},
            {"s3://lance-catalog/bronze/pages"},
            set(),
            [("bronze$events", "s3://lance-catalog/medallion/bronze")],
            id="location-absent",
        ),
        # The control: without it the case above passes on a detector that reports everything.
        pytest.param({"bronze$pages": (ROOT, "s3://lance-catalog/bronze/pages")}, {"s3://lance-catalog/bronze/pages"}, set(), [], id="bytes-present"),
        # `lance_docs/namespace.md:974` — "Relative path to the table directory within the root". Found under
        # its root it is present, and missing it is reported RESOLVED, or nobody can go and look.
        pytest.param({"ns$t": (ROOT, "a1b2c3d4_ns$t")}, {f"{ROOT}/a1b2c3d4_ns$t"}, set(), [], id="relative-location-present"),
        pytest.param({"ns$t": (ROOT, "a1b2c3d4_ns$t")}, set(), set(), [("ns$t", f"{ROOT}/a1b2c3d4_ns$t")], id="relative-location-absent"),
        # The false positive the schema itself rules out: `location` is nullable, "only for tables".
        pytest.param({"ns$reserved": (ROOT, None)}, set(), set(), [], id="declared-only-table"),
        # A trashed table's record is gone by definition while its bytes remain, so neither side of the pair
        # is a defect: the same exclusion `_unregistered_datasets` makes, in the other direction.
        pytest.param({"ns$dropped": (ROOT, "s3://lance-catalog/gone")}, set(), {"s3://lance-catalog/gone"}, [], id="dropped-awaiting-the-purge"),
    ],
)
def test_a_record_is_drift_only_when_its_resolved_location_is_absent(
    registered: dict[str, tuple[str, str | None]], discovered: set[str], trashed: set[str], absent: list[tuple[str, str]]
) -> None:
    found = _absent_datasets(registered=registered, discovered=discovered, trashed=trashed)
    assert [(f.table, f.location) for f in found] == absent


# --------------------------------------------------------------------------- #
# The WALKED-BUCKET boundary: what was looked at, vs what happened to be found
# --------------------------------------------------------------------------- #


def _drift(registered: dict[str, tuple[str, str | None]], datasets: list[str], walked: list[str]) -> ReconcileReport:
    report = ReconcileReport(checked_at="now")
    sources = Sources(tables=[], table_locations=registered, trash=[])
    _registration_drift(report, sources, [(uri, uri.removeprefix("s3://")) for uri in datasets], walked_buckets=walked, declared_roots=())
    return report


def test_an_EMPTY_bucket_that_was_walked_still_reports_its_absent_records() -> None:
    """The live run's own finding. Deriving "walked" from the discovered URIs makes the answer depend on
    whether a bucket happened to hold anything: the same missing record is a FINDING in a bucket with one
    dataset and merely UNKNOWN in an empty one. Emptiness is the strongest possible evidence of absence,
    so that is exactly backwards."""
    report = _drift({"ns$t": ("s3://wh", "s3://wh/dead")}, datasets=[], walked=["wh"])
    assert [f.table for f in report.absent_datasets] == ["ns$t"]
    assert report.counts["absent_datasets"] == 1
    assert report.incomplete == []


def test_a_bucket_NOBODY_walked_is_unknown_rather_than_clean() -> None:
    """The other side of the same boundary, and the reason the filter exists at all."""
    report = _drift({"ns$t": ("s3://elsewhere", "s3://elsewhere/dead")}, datasets=[], walked=["wh"])
    assert report.absent_datasets == []
    assert report.counts["absent_datasets"] == 0
    assert [i.reason for i in report.incomplete] == ["ns$t is registered at s3://elsewhere/dead, outside every scanned bucket — its bytes were not looked for"]
