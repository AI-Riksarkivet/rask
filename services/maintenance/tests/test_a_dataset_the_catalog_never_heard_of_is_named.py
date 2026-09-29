"""A dataset sitting in a maintained bucket that no catalog record names gets its own category.

[[LH-176]]. Measured live 2026-09-19 and still true 2026-09-20:
`s3://lance-catalog/m2proof_silver$m2-proof-1788537252` holds 300 rows across one live version,
`GET /v1/table/m2proof_silver$m2-proof-1788537252` answers 404, and `m2proof` is in none of the 93
registered projects. **Not one drift category counts it**, and the two that sound like they should
each miss it for a structural reason:

* `ungoverned_tables` compares REGISTERED tables against FGA tuples, so a dataset in neither set is
  outside its domain by construction;
* `orphan_buckets` is per BUCKET and `lance-catalog` is claimed by a real warehouse.

`orphan_files` counted some of its files, which is the misleading part — that category answers "which
files inside a known dataset are unreferenced", so it described the residue of a thing the report
never said existed.

THE INVERSE OF `ungoverned_tables`, AND THAT IS THE POINT. There the catalog knows a table and
authorization does not; here STORAGE holds a dataset and the catalog does not. Both are real bytes
with a governance gap, and neither may ever be resolved by deleting the data — which is why this
category is refused by name in `repair.py` rather than left to fall through a default.

THE DETECTOR IS A SUBTRACTION, so it carries the same caution [[LH-061]]'s repair pass now does: a
discovered dataset counts as unregistered only because it is ABSENT from the table listing. A trash
location is excluded explicitly — a dropped table's bytes are still on disk under a trash record that
names them, so counting those would report every ordinary drop as an unregistered dataset.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from maintenance.core.config import MaintenanceSettings
from maintenance.services import reconcile as mod
from maintenance.services.reconcile import _unregistered_datasets


@pytest.mark.parametrize(
    ("discovered", "registered", "trashed", "named"),
    [
        # THE DEFECT: 300 live rows that no category counted.
        pytest.param(
            ["s3://lance-catalog/m2proof_silver$m2-proof-1788537252"],
            {"acme-bronze$events"},
            set(),
            [("m2proof_silver$m2-proof-1788537252", "s3://lance-catalog/m2proof_silver$m2-proof-1788537252")],
            id="no-table-record",
        ),
        # The control. The catalog lays a table out as `<uuid8>_<ns>$<name>`, so the id has to be recovered
        # from the location before the comparison means anything: a detector that compared raw leaves would
        # report every table in the estate.
        pytest.param(["s3://acme-wh/4750a5b9_acme-bronze$events"], {"acme-bronze$events"}, set(), [], id="registered"),
        # A dropped table's bytes stay on disk under a trash record that names them, and its table record is
        # gone by definition. Counting those would make the category loudest exactly when the estate is
        # behaving correctly.
        pytest.param(["s3://acme-wh/dead1234_gone$table"], set(), {"s3://acme-wh/dead1234_gone$table"}, [], id="trashed"),
        # `table_id_from_location` answers None for a directory that is not an identifier; reporting one as
        # an unregistered TABLE would assert a table exists where only a prefix does.
        pytest.param(["s3://acme-wh/some-directory", "s3://acme-wh/4750a5b9_$events"], set(), set(), [], id="not-a-table-identifier"),
        # A report an operator reads across ticks must not reshuffle, and one dataset discovered twice in a
        # walk is one finding.
        pytest.param(
            ["s3://b/ff11aa22_zeta$t", "s3://b/aa11bb22_alpha$t", "s3://b/ff11aa22_zeta$t"],
            set(),
            set(),
            [("alpha$t", "s3://b/aa11bb22_alpha$t"), ("zeta$t", "s3://b/ff11aa22_zeta$t")],
            id="ordered-and-deduplicated",
        ),
    ],
)
def test_a_dataset_with_no_table_record_is_named(discovered: list[str], registered: set[str], trashed: set[str], named: list[tuple[str, str]]) -> None:
    found = _unregistered_datasets(discovered=discovered, registered=registered, trashed=trashed)

    assert [(f.table_id, f.location) for f in found] == named


# --------------------------------------------------------------------------- #
# The guard. Written because a mutation proved the suite above could not see it: deleting the
# `tables_error` check left 308 tests green.
# --------------------------------------------------------------------------- #


def _category(monkeypatch: pytest.MonkeyPatch, sources: mod.Sources) -> mod.ReconcileReport:
    """Drive `_orphan_category` with storage stubbed, so only the guard is under test."""
    monkeypatch.setattr(mod, "fs_and_base", lambda _uri, _opts: (object(), ""))
    monkeypatch.setattr(mod, "_scannable_buckets", lambda _r, _s, _src: ["lance-catalog"])
    monkeypatch.setattr(
        mod,
        "discover_datasets",
        lambda _fs, _bucket, max_depth=3: SimpleNamespace(uris=["s3://lance-catalog/aa11bb22_ns$live"], truncated=[]),
    )
    monkeypatch.setattr(
        mod, "scan_datasets", lambda _fs, _datasets, _opts: SimpleNamespace(orphans=[], incomplete=[], excluded=[], bytes_by_dataset={}, files_by_dataset={})
    )

    settings = MaintenanceSettings(s3_bucket="lance-catalog", s3_access_key_id="unit", s3_secret_access_key=SecretStr("unit"), orphan_scan_enabled=True)
    report = mod.ReconcileReport(checked_at="2026-09-20T00:00:00Z")
    mod._orphan_category(report, settings, sources)
    return report


def test_a_CATALOG_OUTAGE_reports_UNAVAILABLE_rather_than_the_whole_estate(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE GUARD. The finding is an ABSENCE from the table listing, so an unread listing makes every
    dataset in the estate look unregistered — the loudest possible way to report nothing. UNAVAILABLE
    leaves the category out of `counts` entirely, because a 0 there would read as "checked, none
    found" about a store that was never read."""
    report = _category(monkeypatch, mod.Sources(tables_error="catalog unreachable: connection refused"))

    assert report.unregistered_datasets == [], "a catalog outage reported live datasets as unregistered"
    assert "unregistered_datasets" not in report.counts, "an unread category must not carry a count"
    assert [u.reason for u in report.unavailable if u.category == "unregistered_datasets"], (
        f"the category is silently absent rather than reported unavailable: {[(u.category, u.reason) for u in report.unavailable]}"
    )
