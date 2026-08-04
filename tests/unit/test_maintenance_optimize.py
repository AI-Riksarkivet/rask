"""Real-Lance regression tests for the compaction core (`compact_one`) — local filesystem, no S3.

Pins the §4 change: ``compact_files(defer_index_remap=True)`` (Fragment Reuse Index — compaction and
index maintenance "no longer conflict", lance_docs/guide.md:3150) followed IMMEDIATELY by
``optimize_indices()`` — the exact shipped sequence in ``compact_one`` — must leave the dataset's
indices present and the data fully queryable. Drives the SHIPPED function on a real dataset (§0: test
the shipped composition, not a re-implementation), plus the error-prefix contract the sweep's FAIL
selection keys on (``open:`` vs ``maintain:``).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import lance
import pyarrow as pa
from maintenance.services.optimize import compact_one


def _fragmented_indexed_dataset(root: Path) -> str:
    """A local Lance dataset with a BTREE index and several small fragments (each append = 1 fragment)."""
    uri = str(root / "t.lance")
    lance.write_dataset(pa.table({"id": pa.array(range(100), pa.int64())}), uri)
    ds = lance.dataset(uri)
    ds.create_scalar_index("id", "BTREE")
    for i in range(4):
        base = 100 + i * 10
        lance.write_dataset(pa.table({"id": pa.array(range(base, base + 10), pa.int64())}), uri, mode="append")
    return uri


def test_compact_one_defer_index_remap_keeps_indices_working(tmp_path: Path) -> None:
    uri = _fragmented_indexed_dataset(tmp_path)
    assert len(lance.dataset(uri).get_fragments()) >= 5  # genuinely fragmented before the pass

    result = compact_one(uri, {}, older_than=timedelta(days=7))

    # The shipped sequence (deferred-remap compaction → immediate optimize_indices) completes cleanly …
    assert result.error is None, result.error
    assert result.fragments_removed >= 4  # the small fragments actually merged (into fewer, bigger ones)
    assert result.fragments_added < result.fragments_removed
    # EXACTLY the one user index: deferred remap creates the __lance_frag_reuse SYSTEM index, which the
    # metric must exclude (>=1 would stay green while the system index inflates every dataset's count).
    assert result.indices_optimized == 1
    # … and the dataset stays correct afterwards: the index is still listed and the data fully readable
    # (an index broken by the deferred remap would surface here as a wrong count or a scan error).
    ds = lance.dataset(uri)
    assert any(ix["fields"] == ["id"] for ix in ds.list_indices())
    assert ds.count_rows() == 140
    assert ds.count_rows(filter="id = 137") == 1  # a row from a post-index append is findable


def test_compact_one_reports_zero_indices_for_an_unindexed_dataset(tmp_path: Path) -> None:
    # Review 2026-07-10 (verified on pylance 8.0.0): defer_index_remap creates the __lance_frag_reuse
    # system index on first compaction — an unindexed dataset must still report indices_optimized == 0,
    # not phantom "index maintenance" on every tick forever after.
    uri = str(tmp_path / "plain.lance")
    lance.write_dataset(pa.table({"id": pa.array(range(50), pa.int64())}), uri)
    for i in range(3):
        lance.write_dataset(pa.table({"id": pa.array(range(50 + i * 10, 60 + i * 10), pa.int64())}), uri, mode="append")

    result = compact_one(uri, {}, older_than=timedelta(days=7))

    assert result.error is None, result.error
    assert result.fragments_removed >= 3
    assert result.indices_optimized == 0  # the system index is excluded from the metric


def test_compact_one_open_error_prefix_for_a_missing_dataset(tmp_path: Path) -> None:
    # The sweep's FAIL selection keys on these prefixes: an unopenable dir is "open:" (transient
    # non-dataset noise → never a FAIL event). Pin the prefix so a reword can't silently flip selection.
    result = compact_one(str(tmp_path / "nope.lance"), {}, older_than=timedelta(days=7))
    assert result.error is not None and result.error.startswith("open:")


def test_sweep_buckets_unions_primary_and_extras() -> None:
    """GC must cover EVERY bucket that can hold Lance data (audit 2026-07-14).

    The sweep discovered exactly ONE bucket, so every #3-A per-warehouse bucket and #3-B multi-base data
    bucket was invisible to it: their tables accumulated superseded manifest versions and small fragments
    forever. A storage leak created by the very features that introduce new buckets.
    """
    from maintenance.core.config import MaintenanceSettings

    s = MaintenanceSettings.model_validate({"s3_bucket": "lance-catalog", "s3_extra_buckets": "lance-source, s3://mb-a/, lance-catalog, "})
    # primary first, extras normalized (s3:// + slashes stripped), de-duplicated, empties dropped
    assert s.sweep_buckets == ["lance-catalog", "lance-source", "mb-a"]

    bare = MaintenanceSettings.model_validate({"s3_bucket": "only"})
    assert bare.sweep_buckets == ["only"]  # no extras => unchanged single-bucket behavior


def test_gc_does_not_reclaim_branch_referenced_data(tmp_path: Path) -> None:
    """GC must not delete data that only a BRANCH still references (audit 2026-07-14 — was unverified).

    The audit flagged this as an unknown and said to probe it live BEFORE anyone creates a branch: if
    `cleanup_old_versions` did not walk branch manifests, the compaction cron would eventually reclaim data
    files that a branch is the sole reference for — silent, unrecoverable data loss on a feature we ship.

    Probed empirically: it is BRANCH-AWARE and safe. This test pins that, so a pylance upgrade that
    regresses it fails here rather than in a customer's compaction cron.
    """
    import datetime

    import lance
    import pyarrow as pa

    uri = str(tmp_path / "t")
    ds = lance.write_dataset(pa.table({"id": [1]}), uri)
    ds = lance.write_dataset(pa.table({"id": [2]}), uri, mode="append")
    ds.create_branch("keepme")  # pins v2's data
    ds = lance.write_dataset(pa.table({"id": [3]}), uri, mode="append")  # main advances past it

    data_dir = tmp_path / "t" / "data"
    before = {p.name for p in data_dir.iterdir()}

    # The compaction cron's exact call, with the most aggressive window possible.
    ds.cleanup_old_versions(older_than=datetime.timedelta(seconds=0), error_if_tagged_old_versions=False)

    after = {p.name for p in data_dir.iterdir()}
    assert lance.dataset(uri).branches.list(), "GC destroyed the branch — it would delete branch data"
    assert before == after, f"GC reclaimed branch-referenced data files: {before - after}"
