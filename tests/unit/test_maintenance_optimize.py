"""Real-Lance regression tests for the compaction core (`compact_one`) — local filesystem, no S3.

Pins the §4 change: ``compact_files(defer_index_remap=True)`` (Fragment Reuse Index — compaction and
index maintenance "no longer conflict", lance_docs/guide.md:3160) followed IMMEDIATELY by
``optimize_indices()`` — the exact shipped sequence in ``compact_one`` — must leave the dataset's
indices present and the data fully queryable. Drives the SHIPPED function on a real dataset (§0: test
the shipped composition, not a re-implementation), plus the error-prefix contract the sweep's FAIL
selection keys on (``open:`` vs ``maintain:``).
"""

from __future__ import annotations

import pytest
from collections.abc import Callable
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


# --------------------------------------------------------------------------- #
# #64 — the feature-flag refusal, BEFORE any rewrite.
#
# Every fixture below is a REAL dataset carrying a REAL manifest flag. The defect these pin was
# reproduced, not theorized: `compact_one` on a shallow clone returned fragments_removed=8,
# old_versions_removed=2, error=None — and left the clone holding its own full copy of data it had
# only referenced. "Success" that silently defeats the feature and GCs the evidence.
# --------------------------------------------------------------------------- #


def test_compaction_refuses_a_shallow_clone_without_materializing_it(tmp_path: Path) -> None:
    """The reproduced defect, and the assertion that fails without the pre-rewrite gate.

    A shallow clone has NO `data/` directory: every file resolves through the manifest's `base_paths`
    to the source root (feature flag 16). Compacting it rewrites those foreign files into local ones
    — storage amplification that defeats the entire point of a metadata-only clone — and then runs
    version GC on the result.

    The load-bearing assertions are the two NEGATIVE ones: the version is unchanged and no `data/`
    exists. `refused is not None` alone would pass a gate that refused AFTER compacting.
    """
    source = str(tmp_path / "src.lance")
    lance.write_dataset(pa.table({"id": pa.array(range(50), pa.int64())}), source)
    for i in range(4):
        lance.write_dataset(pa.table({"id": pa.array(range(50 + i * 10, 60 + i * 10), pa.int64())}), source, mode="append")
    ds = lance.dataset(source)
    clone = str(tmp_path / "clone.lance")
    ds.shallow_clone(clone, reference=ds.version)
    assert not (tmp_path / "clone.lance" / "data").exists(), "fixture must really be metadata-only"
    version_before = lance.dataset(clone).version

    result = compact_one(clone, {}, older_than=timedelta(0))

    assert result.refused is not None, "a shallow clone must be REFUSED, not compacted"
    assert "16" in result.refused and "base_paths" in result.refused, f"the refusal must name the flag: {result.refused}"
    # Not an error and not a policy skip: nothing failed, and this is permanent rather than "not this
    # tick". Inflating either count is what kept this invisible.
    assert result.error is None and result.error_type is None
    assert result.skipped is None
    # NOTHING WAS REWRITTEN — the whole point of checking before the pass rather than after it.
    assert result.fragments_removed == 0 and result.fragments_added == 0
    assert result.old_versions_removed == 0
    assert lance.dataset(clone).version == version_before, "the clone was committed to — a rewrite happened"
    assert not (tmp_path / "clone.lance" / "data").exists(), "the clone was MATERIALIZED: metadata-only data got copied in"


def test_compaction_refuses_a_registered_but_unused_base(tmp_path: Path) -> None:
    """`add_bases` sets flag 16 while every `DataFile.base_id` stays `None` — nothing about the
    files looks different yet, and the very next write can land under that base.

    This is the shape a consequence-based detector cannot see, which is why the gate reads the
    manifest FLAGS rather than inspecting file paths.
    """
    from lance.dataset import DatasetBasePath

    uri = str(tmp_path / "based.lance")
    lance.write_dataset(pa.table({"id": pa.array(range(20), pa.int64())}), uri)
    for i in range(3):
        lance.write_dataset(pa.table({"id": pa.array(range(20 + i * 5, 25 + i * 5), pa.int64())}), uri, mode="append")
    alt = tmp_path / "altbase"
    alt.mkdir()
    lance.dataset(uri).add_bases([DatasetBasePath(path=str(alt), name="alt")])
    version_before = lance.dataset(uri).version

    result = compact_one(uri, {}, older_than=timedelta(0))

    assert result.refused is not None and "16" in result.refused
    assert result.error is None
    assert lance.dataset(uri).version == version_before, "a multi-base dataset was rewritten anyway"


def test_compaction_refuses_a_dataset_that_uses_data_overlays(tmp_path: Path, overlay_dataset: Callable[[Path], str]) -> None:
    """Flag 64. An overlay supersedes individual CELL values from `data/overlay-<uuid>.lance`;
    a rewrite that does not understand them would fold stale base values back in.

    Today pylance refuses the open itself, so this asserts the CLASSIFICATION: a typed refusal that
    names the flag, not an untyped `open:` error carrying a Rust source path — which is what the
    sweep's lineage layer drops as transient non-dataset noise and `summarize` buried in `errors`.
    """
    uri = overlay_dataset(tmp_path)
    data_dir = Path(uri) / "data"
    files_before = {p.name for p in data_dir.iterdir()}

    result = compact_one(uri, {}, older_than=timedelta(0))

    assert result.refused is not None, "an overlay dataset must be REFUSED"
    assert "64" in result.refused and "overlay" in result.refused.lower(), f"the refusal must name the flag: {result.refused}"
    assert result.error is None, "a refusal is not an error — the lineage layer drops errors as noise"
    assert result.error_type is None
    assert {p.name for p in data_dir.iterdir()} == files_before, "the overlay dataset's files were rewritten"


def test_an_ordinary_dataset_is_still_compacted(tmp_path: Path) -> None:
    """The negative that keeps the gate honest. A refusal that fired on everything would satisfy
    every test above while silently stopping all maintenance — the exact failure mode a whitelist
    invites."""
    uri = _fragmented_indexed_dataset(tmp_path)

    result = compact_one(uri, {}, older_than=timedelta(0))

    assert result.refused is None, f"an ordinary dataset was refused: {result.refused}"
    assert result.error is None, result.error
    assert result.fragments_removed >= 4, "the gate blocked a compaction it should have allowed"


def test_a_missing_dataset_is_an_open_error_not_a_feature_refusal(tmp_path: Path) -> None:
    """The other half of `test_compact_one_open_error_prefix_for_a_missing_dataset`.

    Declared-only prefixes that never open are ordinary estate noise. Classifying them as refusals
    would make the refusal counter permanently non-zero — and that counter is the ONLY signal that a
    pylance upgrade silently stopped this pass maintaining part of the estate.
    """
    result = compact_one(str(tmp_path / "nope.lance"), {}, older_than=timedelta(7))
    assert result.refused is None
    assert result.error is not None and result.error.startswith("open:")


def test_a_STABLE_ROW_ID_dataset_falls_back_instead_of_failing_the_sweep(tmp_path: Path) -> None:
    """Lance refuses `defer_index_remap` in BOTH directions, and only one was handled.

    The fallback was written for datasets WITHOUT stable row ids, whose refusal reads
    "defer_index_remap requires row_addrs but none were provided" — so it matches on ``row_addrs``.
    Lance also refuses the OPPOSITE case:

        Invalid user input: defer_index_remap=true is not supported on datasets with stable row IDs:
        stable row IDs do not require index remapping during compaction, so there is nothing to defer.

    That message contains no ``row_addrs``, so it fell through to `raise` and became a per-dataset
    sweep error. MEASURED on the live estate 2026-08-16: a real sweep reported
    `datasets: 31, fragments_removed: 0, errors: 11`, and all eleven were this. The medallion cascade
    writes "at file format 2.2 with stable row ids", so the datasets the pipeline produces are exactly
    the ones compaction could never touch — maintenance ran and reclaimed nothing.

    Driven against a REAL stable-row-id dataset rather than a double, so it is Lance's own refusal
    being handled and the test cannot pass against a message Lance no longer emits.
    """
    uri = str(tmp_path / "stable.lance")
    lance.write_dataset(pa.table({"id": pa.array(range(100), pa.int64())}), uri, enable_stable_row_ids=True)
    for i in range(4):
        base = 100 + i * 10
        lance.write_dataset(pa.table({"id": pa.array(range(base, base + 10), pa.int64())}), uri, mode="append")
    assert len(lance.dataset(uri).get_fragments()) >= 5

    result = compact_one(uri, {}, older_than=timedelta(days=7))

    assert result.error is None, f"a stable-row-id refusal became a sweep error: {result.error}"
    assert result.fragments_removed >= 4, "the fallback compaction did not run, so nothing was reclaimed"


class _Panic(BaseException):
    """Stand-in for `pyo3_runtime.PanicException`, whose defining property is its BASE CLASS.

    pyo3 synthesises that module lazily, so it cannot be imported to catch by type. What matters is
    that a Rust panic surfaces as a `BaseException` — which is precisely why `except Exception`
    handlers do not see it.
    """


def test_a_RUST_PANIC_in_index_stats_does_not_kill_the_whole_sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One panicking index took down the entire pass, not just its dataset.

    MEASURED on the live estate 2026-08-16, immediately after compaction started working:

        maintenance/services/index_health.py:65 in _stats
        lance/dataset.py:7359 in index_stats
        pyo3_runtime.PanicException: not yet implemented

    and the sweep answered HTTP 500. `index_stats` panics on an index type Lance has not implemented
    stats for; a panic is a `BaseException`, so BOTH guards missed it — `_stats`' own `except
    Exception` and `compact_one`'s per-dataset capture. The blast radius is the point: index health is
    a REPORTING step, and a report that cannot be produced for one index must not cost every other
    dataset its compaction and version reclamation.

    The dataset here is real and already compacted; only `index_stats` is made to panic.
    """
    from maintenance.services import index_health

    uri = _fragmented_indexed_dataset(tmp_path)

    def _boom(*_a: object, **_k: object) -> dict[str, object]:
        raise _Panic("not yet implemented")

    monkeypatch.setattr(index_health, "_stats", _boom)

    result = compact_one(uri, {}, older_than=timedelta(days=7))

    assert result.error is None, f"a panic in a REPORTING step failed the dataset: {result.error}"
    assert result.fragments_removed >= 4, "compaction was lost to an index-report panic"
