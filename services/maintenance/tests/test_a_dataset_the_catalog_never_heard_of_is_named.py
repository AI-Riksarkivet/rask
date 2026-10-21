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
discovered dataset counts as unregistered only because no `__manifest` row names its location, or the table it is a
branch of ([[LH-520]]): a location names no table, so nothing is recovered from it. A trash
location is excluded explicitly — a dropped table's bytes are still on disk under a trash record that
names them, so counting those would report every ordinary drop as an unregistered dataset.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from maintenance.core.config import MaintenanceSettings
from maintenance.services import reconcile as mod
from maintenance.services.naming import TableIds
from maintenance.services.reconcile import _unregistered_datasets
from service_kit.lakehouse.base_refs import normalise


_TABLE = "s3://acme-wh/0d9c6f3e-7b2a-4c1e-9f5d-2a8b3c4d5e6f"


@pytest.mark.parametrize(
    ("discovered", "registered", "trashed", "named"),
    [
        # THE DEFECT: 300 live rows that no category counted.
        pytest.param(
            ["s3://lance-catalog/m2proof_silver$m2-proof-1788537252"],
            [_TABLE],
            set(),
            ["s3://lance-catalog/m2proof_silver$m2-proof-1788537252"],
            id="no-table-record",
        ),
        # The control: a table is registered when a `__manifest` row names its location, and its branch, which discovery
        # lists as a dataset of its own, through the table's row ([[LH-520]]). No name is read off a location.
        pytest.param([_TABLE, f"{_TABLE}/tree/work"], [_TABLE], set(), [], id="registered-with-its-branch"),
        # A dropped table's bytes stay on disk under a trash record that names them, and its table record is
        # gone by definition. Counting those would make the category loudest exactly when the estate is
        # behaving correctly.
        pytest.param(["s3://acme-wh/dead1234_gone$table"], [], {"s3://acme-wh/dead1234_gone$table"}, [], id="trashed"),
        # The record holds the URL `describe_table` answered, which percent-encodes a registered location's name, while
        # discovery lists the directory the store keeps ([[LH-319]]).
        pytest.param(
            ["s3://acme-wh/registered/räksmörgås"],
            [],
            {"s3://acme-wh/registered/r%C3%A4ksm%C3%B6rg%C3%A5s"},
            [],
            id="trashed-as-the-catalog-spells-it",
        ),
        # A store the platform owns and the catalog opens by explicit URI, never registered ([[LH-164]]): accounted for,
        # not reported.
        pytest.param(["s3://lance-catalog/medallion/models/m"], [], set(), [], id="a-declared-platform-root"),
        # A report an operator reads across ticks must not reshuffle, and one dataset discovered twice in a
        # walk is one finding.
        pytest.param(
            ["s3://b/zeta", "s3://b/alpha", "s3://b/zeta"],
            [],
            set(),
            ["s3://b/alpha", "s3://b/zeta"],
            id="ordered-and-deduplicated",
        ),
    ],
)
def test_a_dataset_no_manifest_row_names_is_reported_by_its_location(discovered: list[str], registered: list[str], trashed: set[str], named: list[str]) -> None:
    names = TableIds(by_location={normalise(location): f"db$t{index}" for index, location in enumerate(registered)})

    found = _unregistered_datasets(discovered=discovered, names=names, trashed=trashed, declared_roots=("s3://lance-catalog/medallion/models",))

    assert [f.location for f in found] == named


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
        lambda _fs, _bucket, max_depth=3: SimpleNamespace(uris=["s3://lance-catalog/0d9c6f3e-7b2a-4c1e-9f5d-2a8b3c4d5e6f"], truncated=[]),
    )
    monkeypatch.setattr(
        mod, "scan_datasets", lambda _fs, _datasets, _opts: SimpleNamespace(orphans=[], incomplete=[], excluded=[], bytes_by_dataset={}, files_by_dataset={})
    )

    settings = MaintenanceSettings(s3_bucket="lance-catalog", s3_access_key_id="unit", s3_secret_access_key=SecretStr("unit"), orphan_scan_enabled=True)
    report = mod.ReconcileReport(checked_at="2026-09-20T00:00:00Z")
    mod._orphan_category(report, settings, sources)
    return report


@pytest.mark.parametrize(
    "sources",
    [
        pytest.param(mod.Sources(tables_error="catalog unreachable: connection refused"), id="table-listing"),
        # Only the trash index tells a table in the trash from an unregistered dataset: its bytes stay on disk while its
        # table record is gone ([[LH-319]]).
        pytest.param(mod.Sources(tables=[], trash_error="registry:trash: OSError: unreachable"), id="trash-index"),
    ],
)
def test_an_unread_input_reports_UNAVAILABLE_rather_than_the_whole_estate(monkeypatch: pytest.MonkeyPatch, sources: mod.Sources) -> None:
    """THE GUARD. The finding is an ABSENCE from the table listing and the trash, so an unread input makes
    every dataset it would have excused look unregistered — the loudest possible way to report nothing.
    UNAVAILABLE leaves both registration categories out of `counts` entirely, because a 0 there would read
    as "checked, none found" about a store that was never read."""
    report = _category(monkeypatch, sources)

    assert (report.unregistered_datasets, report.absent_datasets) == ([], []), "an unread input reported live datasets as drift"
    assert {"unregistered_datasets", "absent_datasets"}.isdisjoint(report.counts), "an unread category must not carry a count"
    assert {u.category for u in report.unavailable} == {"unregistered_datasets", "absent_datasets"}, (
        f"a category is silently absent rather than reported unavailable: {[(u.category, u.reason) for u in report.unavailable]}"
    )
