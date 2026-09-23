"""A dataset whose id the reconciler cannot recover is UNKNOWN, and the report must say so.

[[LH-164]]/[[LH-176]]. `_unregistered_datasets` answers "storage holds a dataset the catalog does not
know", and to do it at all it must recover a table id from the location. Its docstring records the
skip honestly — "a location `table_id_from_location` cannot answer for is skipped, never reported:
it names a directory rather than a table" — and that is right about each location and wrong about the
REPORT, because the skip is silent. A category that answers 0 over an estate it could not read is the
one number this module's own docstring says it must never print.

MEASURED ON THE LIVE ESTATE 2026-09-23, over the 564 dataset locations the reconcile scan holds:
**163 — 29% — have no recoverable id**, and they fall into knowable shapes rather than noise:

  * `medallion/models/<model>` (16) — the MODEL REGISTRY, a platform-owned store the catalog
    deliberately cannot resolve (`catalog/core/config.py:516-522`: the trainer writes it directly and
    "the promote/describe endpoints open it by EXPLICIT URI");
  * `<uuid8>_<ns>$<table>/tree/<branch>/...` — Lance BRANCHES. The format puts branch isolation in the
    storage prefix, so these are part of their parent table and are correctly not separate tables;
  * `<bucket>/<tier>/<name>` — the medallion cascade's own layout, which is not the catalog's.

The live report said `unregistered_datasets: 0` over exactly that. The count was not wrong about the
locations it could read; it was silent about the 29% it could not.

A COVERAGE FIELD, NOT A FINDING AND NOT AN INCOMPLETE SCAN. These are not known to be ungoverned;
they are not known. `incomplete` was the first channel tried and the existing gate
`test_the_scan_s_exclusions_reach_their_own_field_and_no_other` refused it, correctly: `incomplete`
gates the #79 purge, no operator action can clear 163 branch prefixes and cascade layouts, and a gate
nobody can satisfy is what `NON_GATING_CATEGORIES` spends twenty lines arguing is not a safety
property. It sits beside `excluded_datasets`, which is the same shape for the same reason.
"""

from __future__ import annotations

from maintenance.services.reconcile import _unreadable_locations


ROOT = "s3://lance-catalog"


def test_a_catalog_laid_out_location_is_readable() -> None:
    """The control. Without it every assertion below passes on a detector that flags everything."""
    assert _unreadable_locations(discovered=[f"{ROOT}/4750a5b9_acme-bronze$events"], declared_roots=()) == []


def test_a_location_with_no_recoverable_id_is_named() -> None:
    """The medallion cascade's own layout: `<bucket>/<tier>/<name>` carries no `$` and no uuid8, so the
    id recovery returns None and the comparison silently drops it."""
    found = _unreadable_locations(discovered=[f"{ROOT}/silver/vasa-publish"], declared_roots=())
    assert found == [f"{ROOT}/silver/vasa-publish"]


def test_a_DECLARED_platform_root_is_not_reported() -> None:
    """The model registry is not unknown — it is known and known to be unresolvable. Reporting it every
    tick would bury the locations that genuinely were not looked at."""
    assert _unreadable_locations(discovered=[f"{ROOT}/medallion/models/churn"], declared_roots=(f"{ROOT}/medallion/models",)) == []


def test_a_declared_root_does_not_swallow_its_SIBLING() -> None:
    """`rm --recursive a/b` also removes `a/b-other`: a prefix match without the delimiter would
    declare `medallion/models-scratch` platform-owned because it starts with the same characters."""
    found = _unreadable_locations(discovered=[f"{ROOT}/medallion/models-scratch/x"], declared_roots=(f"{ROOT}/medallion/models",))
    assert found == [f"{ROOT}/medallion/models-scratch/x"]


def test_a_BRANCH_prefix_is_named_like_any_other_unreadable_location() -> None:
    """Lance puts branch isolation in the storage prefix (`tree/<b>/`), so a branch directory is part of
    its parent table rather than a table. It is still a location the comparison could not read, and
    saying so is what lets a reader decide it is benign — which a silent skip never does."""
    uri = f"{ROOT}/a1b2c3d4_ns$t/tree/feature-x"
    assert _unreadable_locations(discovered=[uri], declared_roots=()) == [uri]


def test_the_result_is_sorted_and_deduplicated() -> None:
    """A report read by a human and diffed by a machine: stable order, no repeats."""
    uris = [f"{ROOT}/silver/b", f"{ROOT}/silver/a", f"{ROOT}/silver/b/"]
    assert _unreadable_locations(discovered=uris, declared_roots=()) == [f"{ROOT}/silver/a", f"{ROOT}/silver/b"]


def test_the_WIRING_reaches_the_report_and_does_not_gate(tmp_path: object) -> None:
    """The hop a green detector cannot prove, and the channel it must NOT use.

    `_registration_drift` is where both registration categories are attached, so the coverage note
    belongs there — and it must land in `unreadable_locations`, never in `incomplete`, which gates the
    #79 purge.
    """
    from maintenance.services.reconcile import ReconcileReport, Sources, _registration_drift

    report = ReconcileReport(checked_at="now")
    sources = Sources(tables=[], table_locations={}, trash=[])
    datasets = [("s3://wh/silver/vasa", "wh/silver/vasa"), ("s3://wh/4750a5b9_ns$t", "wh/4750a5b9_ns$t")]
    _registration_drift(report, sources, datasets, walked_buckets=["wh"], declared_roots=())

    assert report.unreadable_locations == ["s3://wh/silver/vasa"], report.unreadable_locations
    assert report.incomplete == [], "a coverage field must not also block the purge"
    assert "unreadable_locations" not in report.counts, "it is coverage, not a drift category"


def test_a_DECLARED_root_reaches_the_report_too(tmp_path: object) -> None:
    """The control for the wiring: the declared roots must actually be threaded, not defaulted away.
    A defaulted parameter makes an un-wired chain look clean."""
    from maintenance.services.reconcile import ReconcileReport, Sources, _registration_drift

    report = ReconcileReport(checked_at="now")
    sources = Sources(tables=[], table_locations={}, trash=[])
    datasets = [("s3://wh/medallion/models/churn", "wh/medallion/models/churn")]
    _registration_drift(report, sources, datasets, walked_buckets=["wh"], declared_roots=("s3://wh/medallion/models",))
    assert report.unreadable_locations == []
