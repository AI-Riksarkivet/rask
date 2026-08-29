"""Real-Lance regression tests for the compaction core (`compact_one`) — local filesystem, no S3.

Pins the §4 change: ``compact_files(defer_index_remap=True)`` (Fragment Reuse Index — compaction and
index maintenance "no longer conflict", lance_docs/guide.md:3160) followed IMMEDIATELY by
``optimize_indices()`` — the exact shipped sequence in ``compact_one`` — must leave the dataset's
indices present and the data fully queryable. Drives the SHIPPED function on a real dataset (§0: test
the shipped composition, not a re-implementation), plus the error-prefix contract the sweep's FAIL
selection keys on (``open:`` vs ``maintain:``).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import lance
import pyarrow as pa
import pytest
from lance.blob import Blob
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


def test_compaction_is_PERMITTED_on_an_external_blob_base_and_payloads_still_resolve(tmp_path: Path) -> None:
    """The over-refusal this test exists to kill, driven through the SHIPPED `compact_one`.

    `ingest/lander.py::create_empty` and `medallion/services/compute.py` both register ONE external
    blob base through `initial_bases`: a bare object-store prefix where the payload bytes already
    live. That sets flag 16 exactly as a shallow clone does — and the flags-only gate refused both,
    so the cascade's own tiers accumulated fragments forever while the sweep reported a clean pass
    (`fragments_removed_total=0` over 785 ticks on the live estate).

    Nothing about this layout is the clone hazard: the dataset's own data files are under its own
    root (every `DataFile.base_id` is None), so compaction merges its own fragments and copies
    nothing foreign in. MEASURED on pylance 10.0.0 building this same fixture by hand: 4 fragments
    -> 1, 9,445 -> 14,366 bytes locally, base directory byte-identical, 20/20 payloads still
    resolving.

    The load-bearing assertion is not `refused is None` — it is that fragments actually MERGED and
    that every external payload still reads back afterwards.
    """
    payloads = tmp_path / "payloads"
    payloads.mkdir()
    uris = []
    for i in range(20):
        blob = payloads / f"page-{i:03d}.bin"
        blob.write_bytes(b"X" * 4096)
        uris.append(blob.resolve().as_uri())

    def chunk(lo: int, hi: int) -> pa.Table:
        return pa.table({"id": pa.array(range(lo, hi), pa.int64()), "payload": lance.blob_array([Blob.from_uri(uris[i]) for i in range(lo, hi)])})

    uri = str(tmp_path / "bronze.lance")
    lance.write_dataset(
        chunk(0, 5),
        uri,
        data_storage_version="2.2",
        enable_stable_row_ids=True,
        initial_bases=[lance.DatasetBasePath(str(payloads.resolve()), "payloads")],
        external_blob_mode="reference",
    )
    for lo in (5, 10, 15):
        lance.write_dataset(chunk(lo, lo + 5), uri, mode="append", data_storage_version="2.2", external_blob_mode="reference")
    assert len(lance.dataset(uri).get_fragments()) == 4, "fixture must really be fragmented"
    base_files_before = sorted(p.name for p in payloads.iterdir())

    result = compact_one(uri, {}, older_than=timedelta(0))

    assert result.refused is None, f"the cascade's own bronze layout was refused compaction: {result.refused}"
    assert result.error is None, f"compaction errored on an external-blob-base dataset: {result.error}"
    assert result.fragments_removed >= 4 and result.fragments_added == 1, (
        f"nothing was actually merged: removed={result.fragments_removed} added={result.fragments_added}"
    )
    assert len(lance.dataset(uri).get_fragments()) == 1
    # The whole point of the base: its bytes are NOT ours to rewrite, and every pointer must survive.
    assert sorted(p.name for p in payloads.iterdir()) == base_files_before, "compaction touched the external base"
    table = lance.dataset(uri).scanner(columns=["payload"], blob_handling="all_binary").to_table()
    assert sum(1 for v in table.column("payload").to_pylist() if v) == 20, "payloads stopped resolving after compaction"


def test_compaction_refuses_a_base_that_HOLDS_this_datasets_data_files(tmp_path: Path) -> None:
    """The half of `add_bases` that is genuinely the clone hazard, and the one the manifest's own
    `BasePath.is_dataset_root` bit CANNOT see.

    `write_dataset(..., target_bases=["alt"])` lands this dataset's data files under the registered
    base. The manifest still reports `is_dataset_root=False` and the base directory still has no
    `_versions/` — it is not a dataset root by either reading — yet the files are foreign-resident
    and compaction pulls them home. MEASURED on pylance 10.0.0 with this exact fixture: local root
    3,540 -> 5,991 bytes, the base's three data files left behind as garbage, every surviving
    `base_id` None.

    So `DataFile.base_id` is a third, independent signal, and the gate refuses on it.
    """
    alt = tmp_path / "altbase"
    alt.mkdir()
    uri = str(tmp_path / "targeted.lance")
    lance.write_dataset(pa.table({"id": pa.array(range(10), pa.int64())}), uri, initial_bases=[lance.DatasetBasePath(str(alt), "alt")])
    for i in range(3):
        lance.write_dataset(pa.table({"id": pa.array(range(10 + i * 5, 15 + i * 5), pa.int64())}), uri, mode="append", target_bases=["alt"])
    assert any(df.base_id is not None for f in lance.dataset(uri).get_fragments() for df in f.data_files()), "fixture must really put data under the base"
    base_files_before = sorted(p.name for p in alt.iterdir())
    version_before = lance.dataset(uri).version

    result = compact_one(uri, {}, older_than=timedelta(0))

    assert result.refused is not None, "a dataset whose data files live under a base must be REFUSED"
    assert "16" in result.refused and "base_paths" in result.refused, f"the refusal must name the flag: {result.refused}"
    assert result.error is None
    assert result.fragments_removed == 0 and result.fragments_added == 0
    assert lance.dataset(uri).version == version_before, "a base-resident dataset was rewritten anyway"
    assert sorted(p.name for p in alt.iterdir()) == base_files_before, "the base's files were orphaned by a rewrite"


def test_compaction_refuses_a_registered_base_that_IS_A_LANCE_DATASET_ROOT(tmp_path: Path) -> None:
    """Why the gate PROBES object storage instead of trusting the manifest's self-report.

    `add_bases` pointed at another dataset's root produces `BasePath(is_dataset_root=False)` —
    measured on pylance 10.0.0, the bit is only set by `shallow_clone`. A gate reading that bit alone
    would permit compaction on a dataset registered against a live Lance root, which is the clone
    shape wearing the blob shape's manifest. The listing does not lie: `<base>/_versions/` is there.
    """
    source = str(tmp_path / "src.lance")
    lance.write_dataset(pa.table({"id": pa.array(range(40), pa.int64())}), source)
    assert (tmp_path / "src.lance" / "_versions").is_dir(), "fixture must really be a dataset root"

    uri = str(tmp_path / "registered.lance")
    lance.write_dataset(pa.table({"id": pa.array(range(20), pa.int64())}), uri)
    for i in range(3):
        lance.write_dataset(pa.table({"id": pa.array(range(20 + i * 5, 25 + i * 5), pa.int64())}), uri, mode="append")
    lance.dataset(uri).add_bases([lance.DatasetBasePath(path=source, name="src")])
    from service_kit.lakehouse import features

    assert [ref.is_dataset_root for ref in features.manifest_base_path_refs(lance.dataset(uri))] == [False], (
        "fixture assumption broken: the manifest bit would have been enough after all"
    )
    version_before = lance.dataset(uri).version

    result = compact_one(uri, {}, older_than=timedelta(0))

    assert result.refused is not None, "a base that IS a Lance dataset root must be refused"
    assert "16" in result.refused and "base_paths" in result.refused, f"the refusal must name the flag: {result.refused}"
    assert result.error is None
    assert lance.dataset(uri).version == version_before


def test_a_registered_but_unused_BARE_PREFIX_is_now_compacted(tmp_path: Path) -> None:
    """MOVED WITH THE CODE, deliberately and not quietly.

    This test used to be `test_compaction_refuses_a_registered_but_unused_base` and asserted the
    blanket flag-16 refusal, on the reasoning that "the very next write can land under that base".
    That reasoning is what kept the cascade's own tiers uncompacted forever, and it is answered
    rather than ignored: when a write DOES land under the base, `DataFile.base_id` says so and
    `test_compaction_refuses_a_base_that_HOLDS_this_datasets_data_files` pins the refusal. A gate
    cannot refuse today's safe rewrite because tomorrow's write might be unsafe — the next tick reads
    the manifest again.

    An `add_bases` prefix that is empty and holds none of our files is the ingest bronze shape with
    the blobs not yet landed, and compaction here merges only our own fragments.
    """
    uri = str(tmp_path / "based.lance")
    lance.write_dataset(pa.table({"id": pa.array(range(20), pa.int64())}), uri)
    for i in range(3):
        lance.write_dataset(pa.table({"id": pa.array(range(20 + i * 5, 25 + i * 5), pa.int64())}), uri, mode="append")
    alt = tmp_path / "altbase"
    alt.mkdir()
    lance.dataset(uri).add_bases([lance.DatasetBasePath(path=str(alt), name="alt")])
    assert lance.dataset(uri).count_rows() == 35

    result = compact_one(uri, {}, older_than=timedelta(0))

    assert result.refused is None, f"a bare registered prefix was refused: {result.refused}"
    assert result.error is None
    assert result.fragments_removed >= 4 and result.fragments_added == 1
    assert lance.dataset(uri).count_rows() == 35, "compaction lost rows"
    assert sorted(alt.iterdir()) == [], "compaction wrote into the registered base"


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


def test_a_LEGAL_HOLD_beats_auto_cleanup(tmp_path: Path) -> None:
    """`cleanup_enabled=False` must win over `auto_cleanup_interval_commits`, not be shadowed by it.

    The branch was `if auto_cleanup … elif cleanup_enabled … else`, so a policy that set both left the
    disable UNREACHABLE — and the disable is documented as "keeps the ENTIRE version history: a tier
    under legal hold". The sweep would hand that dataset's own commit path a standing instruction to
    delete exactly the versions the hold existed for, and report success.

    Asserted on the dataset's config rather than a log line: the damage is that auto-cleanup gets
    CONFIGURED, and configuration outlives the tick that wrote it.
    """
    uri = str(tmp_path / "held.lance")
    lance.write_dataset(pa.table({"v": [1, 2, 3]}), uri)

    result = compact_one(uri, {}, timedelta(days=7), cleanup_enabled=False, auto_cleanup_interval_commits=5)

    assert result.error is None
    assert result.auto_cleanup_configured is False, "a held dataset must not be configured to reclaim itself"
    raw = getattr(lance.dataset(uri), "config", None)
    config = dict((raw() if callable(raw) else raw) or {})
    assert "lance.auto_cleanup.interval" not in config, f"auto-cleanup was written onto a legal-hold dataset: {config}"


def test_auto_cleanup_does_not_COMMIT_A_VERSION_when_already_configured(tmp_path: Path) -> None:
    """`enable_auto_cleanup` is `update_config`, a Lance transaction even when nothing changes.

    MEASURED on pylance 9.0.0: three identical calls took a dataset from version 1 to 4. `compact_one`
    called it unconditionally for every policied dataset on a 120s cron, which made the reclaimer the
    estate's most prolific VERSION PRODUCER — manufacturing precisely the history it exists to remove,
    and resetting each dataset's age-based cleanup window every time it did.
    """
    uri = str(tmp_path / "cfg.lance")
    lance.write_dataset(pa.table({"v": [1, 2, 3]}), uri)

    first = compact_one(uri, {}, timedelta(days=7), auto_cleanup_interval_commits=5)
    assert first.error is None and first.auto_cleanup_configured
    settled = lance.dataset(uri).version

    for _ in range(3):
        again = compact_one(uri, {}, timedelta(days=7), auto_cleanup_interval_commits=5)
        assert again.auto_cleanup_configured, "an already-configured dataset must still report configured"

    assert lance.dataset(uri).version == settled, (
        f"re-applying an identical auto-cleanup config committed new versions ({settled} -> {lance.dataset(uri).version})"
    )


def test_a_SHALLOW_CLONE_gets_reclamation_and_indices_but_NOT_compaction(tmp_path: Path) -> None:
    """The flag-16 refusal was over-broad by exactly one operation, and the cost was the whole sweep.

    MEASURED on this estate 2026-08-16: 17 datasets were refused on `base_paths`, and they were exactly
    the ones with multiple fragments and version history; the 9 the sweep did maintain needed nothing.
    So the sweep ran every 120s and, by construction, did no work at all.

    The split is safe because the hazards differ. `cleanup_old_versions` is root-scoped — one call on a
    clone with dead fragments on BOTH sides removed the 2 clone-owned files and left all 4 base-owned
    ones — and `optimize_indices` writes a delta into the clone's own root. Only `compact_files` is
    wrong, and for COST not safety: it materialises the shared data into the clone (1,072 -> 108,199
    bytes against a 119,693-byte base), defeating the point of cloning.

    Asserted on both halves: the pass must not report a bare refusal that stops everything, and the
    clone must not silently grow a private copy.
    """
    source = str(tmp_path / "src.lance")
    ds = lance.write_dataset(pa.table({"v": list(range(64))}), source, max_rows_per_file=16)
    clone = str(tmp_path / "clone.lance")
    ds.shallow_clone(clone, reference=ds.version)
    assert not (tmp_path / "clone.lance" / "data").exists(), "fixture must really be a metadata-only clone"

    # Give the clone real reclamation work: extra versions, all superseded.
    for extra in range(3):
        lance.write_dataset(pa.table({"v": [extra]}), clone, mode="overwrite")

    result = compact_one(clone, {}, older_than=None, retain_versions=1)

    assert result.error is None, f"the clone must not error: {result.error}"
    # THE DISCRIMINATOR. Before the split the pass returned at the flag gate having done nothing, so
    # asserting only "no error / no fragments removed" passed with and without the fix.
    assert result.old_versions_removed > 0, "the clone got no version reclamation — the blanket flag-16 refusal is still stopping root-scoped work"
    assert result.fragments_removed == 0, "compaction must NOT run on a clone — it would materialise a private copy"
    assert result.refused is not None and "base_paths" in result.refused, (
        f"the compaction skip must be reported with its reason, got refused={result.refused!r}"
    )


def test_an_UNKNOWN_flag_still_refuses_everything(tmp_path: Path) -> None:
    """Splitting the gate must not weaken it. `SUPPORTED_FOR_GC` adds base_paths and nothing else, so
    data overlays (flag 64) and any future unknown flag still refuse the whole pass — the narrow gate
    is a per-operation exception, not a general relaxation."""
    from service_kit.lakehouse import features

    assert features.describe_gc_unsupported_flags(features.FLAG_DATA_OVERLAYS, 0) is not None
    assert features.describe_gc_unsupported_flags(1 << 20, 0) is not None
    # base_paths is the ONLY difference between the two masks.
    assert features.SUPPORTED_FOR_GC == features.SUPPORTED | features.FLAG_BASE_PATHS
    assert features.describe_gc_unsupported_flags(features.FLAG_BASE_PATHS, features.FLAG_BASE_PATHS) is None
    assert features.describe_unsupported_flags(features.FLAG_BASE_PATHS, features.FLAG_BASE_PATHS) is not None


# ---------------------------------------------------------------- the ORDER of the three steps


class _RecordingOptimize:
    """`ds.optimize`, recording WHICH step ran and WHEN rather than only that it ran."""

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    def compact_files(self, **_kw: object) -> object:
        self._calls.append("compact_files")
        from types import SimpleNamespace

        return SimpleNamespace(fragments_removed=2, fragments_added=1)

    def optimize_indices(self) -> None:
        self._calls.append("optimize_indices")


class _RecordingDataset:
    """The narrowest Lance dataset `compact_one` drives. A double, deliberately: the subject is the
    ORDER of three calls, and a real dataset can only show their effects — from which order is not
    recoverable (a reclaimed version looks the same whether it was reclaimed before or after the
    compaction that produced it)."""

    def __init__(self, calls: list[str]) -> None:
        from types import SimpleNamespace

        self._calls = calls
        self.optimize = _RecordingOptimize(calls)
        self.schema = SimpleNamespace(metadata={})
        self._ds = SimpleNamespace(serialized_manifest=lambda: b"")  # no feature flags set

    def describe_indices(self) -> list[object]:
        return []

    def list_indices(self) -> list[dict[str, object]]:
        return []

    def cleanup_old_versions(self, **_kw: object) -> object:
        from types import SimpleNamespace

        self._calls.append("cleanup_old_versions")
        return SimpleNamespace(old_versions=1, bytes_removed=64)


def test_compact_one_runs_the_three_steps_in_ORDER(monkeypatch: pytest.MonkeyPatch) -> None:
    """compact -> optimize_indices -> cleanup, asserted as a SEQUENCE.

    This is the function's single load-bearing invariant and it was guarded by prose in three places
    and by no assertion: `compact_one`'s docstring says the order is "FIXED, not configurable", the
    inline comment repeats it, and `base_refs.py` builds the whole #114 refusal on compaction and
    cleanup running as ONE ordered pass. Every existing test over `compact_one` asserts one step's
    EFFECT, which is order-blind — reversing two steps changes none of those numbers.

    Each step depends on the one before it. Compaction leaves its new fragments unindexed, so index
    optimization must FOLLOW it or the dataset is left with indices that do not cover the data it
    just rewrote. Cleanup runs LAST because it reclaims the superseded versions BOTH earlier steps
    produce; run first it reclaims nothing, and run between them it deletes the versions the index
    optimization still needs to remap through.
    """
    calls: list[str] = []
    monkeypatch.setattr("maintenance.services.optimize.lance.dataset", lambda *_a, **_k: _RecordingDataset(calls))

    result = compact_one("s3://wh/t.lance", {}, older_than=timedelta(days=7))

    assert result.error is None, result.error
    assert calls == ["compact_files", "optimize_indices", "cleanup_old_versions"]


def test_compact_one_does_not_call_a_DEPRECATED_pylance_api(tmp_path: Path, recwarn: pytest.WarningsRecorder) -> None:
    """`list_indices` is deprecated on pylance 10 ("Use describe_indices() instead") and the sweep is
    the estate's most frequent caller of it — every policied dataset, every 120s.

    A deprecation is a REMOVAL notice, and the two calls are not interchangeable at the call site:
    `list_indices` fans an index out into one dict PER SEGMENT while `describe_indices` returns one
    object per index, so a straight swap on the day it is removed would silently change the count
    this pass reports. Migrating now, with the count asserted below, is the cheap moment. The rest of
    this service already reads `describe_indices` (`index_health.inspect_indices`), so the sweep was
    also asking Lance the same question twice in two different shapes.
    """
    uri = _fragmented_indexed_dataset(tmp_path)

    result = compact_one(uri, {}, older_than=timedelta(days=7))

    assert result.error is None, result.error
    assert result.indices_optimized == 1, "the migration must not change what the metric counts"
    deprecated = [w for w in recwarn.list if issubclass(w.category, DeprecationWarning) and "list_indices" in str(w.message)]
    assert not deprecated, f"compact_one still calls a deprecated pylance API: {[str(w.message) for w in deprecated]}"
