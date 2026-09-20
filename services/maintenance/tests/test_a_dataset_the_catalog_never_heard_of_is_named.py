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
from maintenance.services.reconcile import UnregisteredDataset, _unregistered_datasets


def test_a_dataset_with_no_table_record_is_named() -> None:
    """THE DEFECT: 300 live rows that no category counted."""
    found = _unregistered_datasets(
        discovered=["s3://lance-catalog/m2proof_silver$m2-proof-1788537252"],
        registered={"acme-bronze$events"},
        trashed=set(),
    )

    assert [f.table_id for f in found] == ["m2proof_silver$m2-proof-1788537252"]
    assert found[0].location == "s3://lance-catalog/m2proof_silver$m2-proof-1788537252"


def test_a_REGISTERED_dataset_is_not_named() -> None:
    """The control. The catalog lays a table out as `<uuid8>_<ns>$<name>`, so the id has to be
    recovered from the location before the comparison means anything — a detector that compared raw
    leaves would report every table in the estate."""
    found = _unregistered_datasets(
        discovered=["s3://acme-wh/4750a5b9_acme-bronze$events"],
        registered={"acme-bronze$events"},
        trashed=set(),
    )

    assert found == []


def test_a_TRASHED_dataset_is_not_named() -> None:
    """A dropped table's bytes stay on disk under a trash record that names them, and its table record
    is gone by definition. Counting those would report every ordinary drop as unregistered — the
    category would be loudest exactly when the estate was behaving correctly."""
    found = _unregistered_datasets(
        discovered=["s3://acme-wh/dead1234_gone$table"],
        registered=set(),
        trashed={"s3://acme-wh/dead1234_gone$table"},
    )

    assert found == []


def test_a_location_that_names_no_table_is_not_named() -> None:
    """`table_id_from_location` answers None for a directory that is not an identifier. Reporting one
    as an unregistered TABLE would assert a table exists where only a prefix does."""
    found = _unregistered_datasets(
        discovered=["s3://acme-wh/some-directory", "s3://acme-wh/4750a5b9_$events"],
        registered=set(),
        trashed=set(),
    )

    assert found == []


def test_findings_are_ordered_and_deduplicated() -> None:
    """A report an operator reads across ticks must not reshuffle, and the same dataset discovered
    twice under one walk is one finding."""
    found = _unregistered_datasets(
        discovered=[
            "s3://b/ff11aa22_zeta$t",
            "s3://b/aa11bb22_alpha$t",
            "s3://b/ff11aa22_zeta$t",
        ],
        registered=set(),
        trashed=set(),
    )

    assert [f.table_id for f in found] == ["alpha$t", "zeta$t"]


def test_the_finding_carries_both_the_id_and_where_to_look() -> None:
    """An id alone cannot be acted on — the whole point is that no record says where it lives."""
    found = _unregistered_datasets(discovered=["s3://lance-catalog/aa11bb22_ns$t"], registered=set(), trashed=set())

    assert found == [UnregisteredDataset(table_id="ns$t", location="s3://lance-catalog/aa11bb22_ns$t")]


def test_the_repair_pass_REFUSES_this_category_by_name() -> None:
    """Real bytes. A pass that "repaired drift" by acting on it would destroy live data to close a
    registration gap — the same rule that makes `ungoverned_tables` a named refusal."""
    from maintenance.services.repair import _REFUSED

    assert "unregistered_datasets" in _REFUSED, f"the repair pass would fall through to a default for it: {sorted(_REFUSED)}"
    assert "regist" in _REFUSED["unregistered_datasets"].lower(), _REFUSED["unregistered_datasets"]


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


def test_a_COMPLETE_table_listing_lets_the_category_report(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control: without it the guard test below passes by never reporting anything."""
    report = _category(monkeypatch, mod.Sources(tables=[]))

    assert report.counts.get("unregistered_datasets") == 1
    assert [f.table_id for f in report.unregistered_datasets] == ["ns$live"]


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
