"""Real-Lance regression tests for the compaction core (`compact_one`) — local filesystem, no S3.

Pins the §4 change: ``compact_files(defer_index_remap=True)`` (Fragment Reuse Index — compaction and
index maintenance "no longer conflict", lance_docs/guide.md:3160) followed IMMEDIATELY by
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


def test_a_policy_can_skip_cleanup_while_still_compacting(tmp_path: Path) -> None:
    """`cleanup_enabled=False` keeps the ENTIRE version history — a tier under legal hold, or one
    whose time-travel window IS the product.

    Compaction still runs, because it changes layout, not history. That is the whole reason these are
    per-STEP flags rather than one all-or-nothing opt-out: the two operations have different risks and
    an operator may legitimately want one without the other.
    """
    uri = _fragmented_indexed_dataset(tmp_path)
    before = len(lance.dataset(uri).versions())

    result = compact_one(uri, {}, timedelta(0), cleanup_enabled=False)

    assert result.error is None, result.error
    assert result.old_versions_removed == 0, "cleanup ran despite the policy disabling it"
    assert len(lance.dataset(uri).versions()) >= before, "version history was reclaimed anyway"


def test_cleanup_still_runs_by_default(tmp_path: Path) -> None:
    """The negative of the test above. Without it, a gate that skipped cleanup UNCONDITIONALLY would
    pass — which is the same class of mistake as a detector that refuses everything."""
    uri = _fragmented_indexed_dataset(tmp_path)
    result = compact_one(uri, {}, timedelta(0))
    assert result.error is None, result.error
    assert result.old_versions_removed > 0, "the default pass must still reclaim old versions"


def test_a_policy_can_skip_index_optimization(tmp_path: Path) -> None:
    """Skipping index optimization after a compaction leaves the new fragments unindexed until the
    next enabled pass — queries fall back to a flat scan rather than returning wrong rows, which is
    why it is a legal choice rather than a corrupting one."""
    uri = _fragmented_indexed_dataset(tmp_path)
    result = compact_one(uri, {}, timedelta(0), optimize_indices_enabled=False)
    assert result.error is None, result.error
    assert result.indices_optimized == 0


def test_a_scan_batch_size_reaches_compaction_and_still_compacts(tmp_path: Path) -> None:
    """The sweep's READ batch is policy-settable because rows are not a unit of memory: Lance's
    default 8192-ROW batch against ~1.8 MB bronze page-image rows is ~15 GB per compute thread.

    Asserts the value REACHES `compact_files` (a silently-dropped kwarg is the failure mode that
    matters here — the pass would look identical while still reading 8192 rows) and that compaction
    genuinely still happens at a small batch.
    """
    uri = _fragmented_indexed_dataset(tmp_path)
    seen: dict[str, object] = {}
    real = lance.dataset(uri).optimize.__class__.compact_files

    def _spy(self: object, *args: object, **kwargs: object) -> object:
        seen.update(kwargs)
        return real(self, *args, **kwargs)  # ty: ignore[invalid-argument-type] — a spy is deliberately untyped

    lance.dataset(uri).optimize.__class__.compact_files = _spy  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]
    try:
        result = compact_one(uri, {}, timedelta(0), scan_batch_size=64)
    finally:
        lance.dataset(uri).optimize.__class__.compact_files = real  # type: ignore[method-assign]

    assert result.error is None, result.error
    assert seen.get("batch_size") == 64, f"scan_batch_size never reached compact_files: {seen}"
    assert result.fragments_removed > 0, "compaction did not run at a small batch size"


def test_no_batch_size_leaves_lance_defaulting(tmp_path: Path) -> None:
    """The negative: `compact_one` itself must not pin a batch size. Passing `batch_size=None`
    through to Lance is NOT the same as omitting it.

    Still true after #93, and the layering is the point: the safe DEFAULT lives in
    `MaintenanceSettings` and is applied by the SWEEP, so `compact_one` stays a faithful pass-through
    for callers that have bounded memory some other way. What #93 changed is that nothing in the
    deployed estate reaches Lance's unbounded default any more — see
    `test_maintenance_policies.py::test_an_unpolicied_sweep_is_still_bounded`."""
    uri = _fragmented_indexed_dataset(tmp_path)
    seen: dict[str, object] = {}
    real = lance.dataset(uri).optimize.__class__.compact_files

    def _spy(self: object, *args: object, **kwargs: object) -> object:
        seen.update(kwargs)
        return real(self, *args, **kwargs)  # ty: ignore[invalid-argument-type] — a spy is deliberately untyped

    lance.dataset(uri).optimize.__class__.compact_files = _spy  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]
    try:
        compact_one(uri, {}, timedelta(0))
    finally:
        lance.dataset(uri).optimize.__class__.compact_files = real  # type: ignore[method-assign]

    assert "batch_size" not in seen, f"an unset policy pinned a batch size anyway: {seen}"


def test_handing_cleanup_to_the_dataset_skips_our_own_sweep(tmp_path: Path) -> None:
    """#58 — Lance ships its OWN auto-cleanup on the commit path, so a write-heavy tier may need no
    cron of ours. Setting the interval configures the DATASET and SKIPS this pass's cleanup step:
    one owner, never two, because both running is two processes racing to delete the same manifests.
    """
    uri = _fragmented_indexed_dataset(tmp_path)
    before = len(lance.dataset(uri).versions())

    result = compact_one(uri, {}, timedelta(0), auto_cleanup_interval_commits=10)

    assert result.error is None, result.error
    assert result.auto_cleanup_configured is True, "the dataset was never given the cleanup config"
    assert result.old_versions_removed == 0, "our sweep reclaimed versions the DATASET now owns"
    assert len(lance.dataset(uri).versions()) >= before, "the sweep reclaimed anyway"


def test_without_the_interval_our_sweep_still_owns_cleanup(tmp_path: Path) -> None:
    """The negative of the test above — without it, code that ALWAYS delegated would pass."""
    uri = _fragmented_indexed_dataset(tmp_path)
    result = compact_one(uri, {}, timedelta(0))
    assert result.error is None, result.error
    assert result.auto_cleanup_configured is False
    assert result.old_versions_removed > 0, "nobody reclaimed old versions"
