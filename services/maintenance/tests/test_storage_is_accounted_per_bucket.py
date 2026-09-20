"""Per-bucket storage accounting, rolled up from the orphan scan's own listing ([[LH-074]]).

The scan already lists every file under a dataset prefix with its size — that is how it finds orphans
at all — and then discards the referenced ones. Summing the same pass is free, and it is the TOTAL
rather than the orphan subset: rolling up `OrphanFile.size_bytes` would measure unreferenced bytes,
which is not what a quota is about.
"""

from __future__ import annotations

from maintenance.services.reconcile import _roll_up


def test_roll_up_sums_datasets_sharing_a_bucket() -> None:
    assert _roll_up({"s3://wh-a/ds1": 100, "s3://wh-a/ds2": 50}) == {"wh-a": 150}


def test_roll_up_keeps_buckets_separate() -> None:
    assert _roll_up({"s3://wh-a/ds1": 100, "s3://wh-b/ds1": 7}) == {"wh-a": 100, "wh-b": 7}


def test_roll_up_handles_a_bare_bucket_root() -> None:
    assert _roll_up({"s3://wh-a": 12}) == {"wh-a": 12}


def test_roll_up_of_nothing_is_empty_not_zero() -> None:
    # An unscanned estate must not read as an empty one — absent, never 0.
    assert _roll_up({}) == {}
