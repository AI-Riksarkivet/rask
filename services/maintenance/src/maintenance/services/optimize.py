"""The compaction + GC core — infra-light so the discovery + aggregation logic is unit-testable.

``discover_datasets`` is pure list-logic over a pyarrow filesystem; ``compact_one`` wraps the two
blocking Lance maintenance calls. Both keep IO at the edges so the orchestration can be tested with fakes.

(The function was ``discover_dataset_uris`` until it started returning a :class:`Discovery` — uris AND
the prefixes the depth bound stopped at — because the truncation must not be droppable. This docstring
kept the dead name, which is how a reader ends up grepping for a symbol that has no definition.)
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import lance
import pyarrow.fs as pafs
from pydantic import BaseModel, Field

from maintenance.core.config import shared_lance_session
from maintenance.core.lineage_emit import declared_table_id
from maintenance.services.index_health import inspect_indices
from service_kit.lakehouse.base_refs import BaseRefs
from service_kit.lakehouse.features import (
    describe_gc_unsupported_flags,
    describe_unsupported_flags,
    manifest_feature_flags,
    unsupported_features_from_open_error,
)


log = logging.getLogger(__name__)


class DatasetResult(BaseModel):
    """What one dataset's maintenance pass did (or why it was skipped)."""

    uri: str
    #: Why the policy layer skipped this dataset this tick (``policy_disabled`` / ``policy_interval``);
    #: ``None`` when maintenance ran.
    skipped: str | None = None
    #: Why this dataset was REFUSED: its manifest sets a feature flag this pass cannot correctly
    #: rewrite (#64). Deliberately NOT ``skipped`` and deliberately NOT ``error`` — a skip is "not
    #: this tick" (the cadence count would be inflated by something permanent), and an error is
    #: "something failed" (nothing failed; we declined, before touching a byte). Folding a refusal
    #: into either is what made a shallow clone's silent full materialization invisible.
    refused: str | None = None
    #: #F6(d) — this dataset is in the TRASH: dropped with a grace window, recoverable until it expires,
    #: and therefore frozen. Names the record id and its deadline so a record stuck long past its
    #: deadline (one the purge keeps refusing) is visible as a permanent exclusion rather than a
    #: transient one.
    #:
    #: Its own field for the reason `refused` has one, one rung further out. `skipped` is "not this
    #: tick" and would inflate the policy-cadence reading for something that lasts until undrop or
    #: purge; `refused` is about the dataset's LAYOUT (a flag this pass cannot rewrite); this is about
    #: its GOVERNANCE state — we can maintain it perfectly well and must not.
    trashed: str | None = None
    fragments_removed: int = 0
    fragments_added: int = 0
    indices_optimized: int = 0
    old_versions_removed: int = 0
    bytes_removed: int = 0
    #: #60 — what `optimize_indices()` could not put right on this dataset. Empty on a healthy pass.
    #: Serialized dicts rather than models so the sweep's summary stays a plain JSON-able report.
    index_findings: list[dict[str, Any]] = Field(default_factory=list)
    #: True when this pass handed version reclamation to the DATASET (#58) instead of sweeping it.
    #: Distinguishes "reclaimed nothing" from "the writer reclaims this one" — which read identically
    #: on ``old_versions_removed=0`` alone.
    auto_cleanup_configured: bool = False
    #: The canonical lineage/FGA name a PRODUCER declared on the dataset (`lineage.dataset_id` in its
    #: schema metadata), or None. Carried here because `compact_one` already holds the open dataset —
    #: the emit path downstream has only a URI, and for the medallion tiers a URI cannot be resolved
    #: to a name at all (`medallion/bronze` is both `bronze$events` and `bronze$pages`).
    declared_table_id: str | None = None
    error: str | None = None
    # Stable identifier for span aggregation (otel attributes.md: set `error.type` whenever the span
    # status is ERROR) — the exception CLASS name, never the message.
    error_type: str | None = None


class Discovery(BaseModel):
    """What one bucket's walk found — AND what it did not reach.

    ``truncated`` exists because the depth bound used to be silent. A dataset nested deeper than
    ``max_depth`` was neither maintained by the sweep nor scanned by the orphan pass, and NOTHING
    recorded that: both surfaces reported success over the datasets they happened to reach. That is
    the "0 that means we did not look" the orphan module's own docstring forbids, arrived at from the
    other direction. A prefix we stopped walking is now DATA the caller must dispose of — the sweep
    counts it, the reconciler files an ``IncompleteScan``.
    """

    uris: list[str] = []
    #: Prefixes the walk stopped at because it hit ``max_depth`` — each may hide any number of datasets.
    truncated: list[str] = []


def discover_datasets(fs: pafs.FileSystem, bucket: str, *, max_depth: int = 3) -> Discovery:
    """Lance datasets under ``bucket`` — a directory IS a dataset iff it has a ``_versions/`` child
    (the Lance table-layout marker); any other directory is a namespace prefix and is recursed into
    (bounded by ``max_depth``). Skips ``__`` bookkeeping dirs (the catalog's ``__manifest``) and the
    control-plane registries (``_warehouses``, ``_policies``, ``_protection``, ``_trash``) — no dataset ever
    lives under them, and probing them is wasted S3 round-trips on the hot discovery path.

    The catalog lays top-level tables out as ``<uuid>_<table_id>/``, but the medallion cascade nests
    its datasets one level down (``medallion/bronze``, ``medallion/silver-media`` …) — without the
    marker probe the sweep both reported the ``medallion/`` prefix as a failed dataset AND never
    maintained the real ones under it. (The example said ``medallion/raw`` until 2026-08-16, naming a
    tier that R23 makes impossible: raw is the external world, and the governed medallion is exactly
    bronze -> silver -> gold. There is no raw dataset for the sweep to find.)

    Returns a :class:`Discovery`, not a bare list, so the depth cut-off cannot be dropped on the floor.
    """
    found = Discovery()

    def _walk(prefix: str, depth: int) -> None:
        for info in fs.get_file_info(pafs.FileSelector(prefix, recursive=False)):
            if info.type != pafs.FileType.Directory:
                continue
            name = info.path.rstrip("/").split("/")[-1]
            if name.startswith("__") or name in ("_warehouses", "_policies", "_protection", "_trash"):
                continue
            marker = fs.get_file_info(f"{info.path}/_versions")
            if marker.type == pafs.FileType.Directory:
                found.uris.append(f"s3://{info.path}")
            elif depth < max_depth:
                _walk(info.path, depth + 1)
            else:
                # Not a dataset, and we are out of depth — anything under here is unmaintained and
                # unscanned. Record the prefix rather than returning silently.
                found.truncated.append(f"s3://{info.path}")

    _walk(bucket, 1)
    return found


def compact_one(
    uri: str,
    storage_options: dict[str, str],
    older_than: timedelta | None,
    retain_versions: int | None = None,
    target_rows_per_fragment: int | None = None,
    *,
    cleanup_enabled: bool = True,
    optimize_indices_enabled: bool = True,
    scan_batch_size: int | None = None,
    compact_threads: int | None = None,
    auto_cleanup_interval_commits: int | None = None,
    protected: BaseRefs | None = None,
    index_columns: list[str] | None = None,
) -> DatasetResult:
    """One ORDERED maintenance pass over one dataset. Never raises — a per-dataset failure is captured
    in ``error`` so one bad dataset can't abort the whole pass.

    The order is compact → optimize indices → cleanup, and it is FIXED, not configurable: compaction
    leaves its new fragments unindexed, so index optimization must follow it, and cleanup runs LAST
    because it reclaims the superseded versions that BOTH earlier steps produced, in one pass. (This
    sentence had the last two steps swapped until 2026-08-08 while the code and the inline comment
    below were right.) The two ``*_enabled`` flags let a policy skip a STEP
    without reordering them — an operator who wants compaction but not version reclamation (a tier
    under legal hold, say) can have exactly that.

    ``scan_batch_size`` and ``compact_threads`` together bound the compaction read, and they only
    work together: the memory is their PRODUCT. Lance's default batch is 8192 ROWS, and rows are not a
    unit of memory — against ~1.8 MB bronze page-image rows that is ~15 GB per compute thread, times
    a thread count that defaults to the HOST's core count rather than the pod's cpu limit. Both
    default to safe values in ``MaintenanceSettings`` (#93); ``None`` here means "let Lance decide",
    which is only correct for a caller that has bounded memory some other way.

    ``auto_cleanup_interval_commits`` hands version reclamation to the DATASET (#58) — Lance's own
    commit-path auto-cleanup — and, having done so, SKIPS this pass's cleanup step. One owner, never
    two: both running is not additive, it is two processes racing to delete the same manifests.

    A dataset whose manifest sets a feature flag this pass cannot correctly rewrite is REFUSED before
    any rewrite (#64, :mod:`service_kit.lakehouse.features`) — see :attr:`DatasetResult.refused`.
    """
    try:
        # The shared bounded session (#102): per-tick reopens are correct for a mutating pass, but
        # each must not mint-and-discard gigabyte-scale default caches.
        ds = lance.dataset(uri, storage_options=storage_options, session=shared_lance_session())
        # Read the flags inside the same guard: a manifest we cannot PARSE is a dataset we could not
        # read, which is exactly what `open:` means. Refusing on it would be a lie (we know nothing
        # about its layout), and maintaining it would be the shallow-clone mistake again.
        reader_flags, writer_flags = manifest_feature_flags(ds)
    except Exception as exc:
        # pylance refuses a manifest whose flags IT does not know before we can read them ourselves
        # (measured: a committed data overlay, flag 64). That is a REFUSAL, not an unopenable
        # directory — reported as `open:` it reads as transient noise and the lineage layer drops it.
        if (refusal := unsupported_features_from_open_error(exc)) is not None:
            log.warning("maintenance_refused_unsupported_features", extra={"uri": uri, "reason": refusal})
            return DatasetResult(uri=uri, refused=refusal)
        return DatasetResult(uri=uri, error=f"open: {exc}", error_type=type(exc).__name__)
    # TWO GATES, because the three operations do not share a hazard. This was ONE blanket refusal, and
    # the cost was measured: 17 of the estate's datasets were refused on flag 16 and they were exactly
    # the ones with multiple fragments and version history, while the 9 the sweep did maintain needed
    # nothing. The sweep ran every 120s and, by construction, did no work at all.
    #
    # The narrow gate (below) refuses everything this pass cannot rewrite correctly, INCLUDING flag 64
    # and anything unknown. The wide gate refuses only what is unsafe root-scoped, which flag 16 is not:
    # ONE `cleanup_old_versions` call on a clone with dead fragments on both sides removed the 2
    # clone-owned files and left all 4 base-owned ones, and the base still read in a fresh process
    # (pylance 9.0.0). `optimize_indices` writes a delta into the clone's own root and leaves the base
    # byte-identical.
    #
    # So flag 16 gates COMPACTION alone — not for safety but for cost: compacting a CLONE silently
    # materialises the shared data into its own root (1,072 -> 108,199 bytes against a 119,693-byte
    # base), defeating the point of cloning. A refusal that says so beats doing it behind someone's back.
    #
    # THE COST OF THAT REFUSAL IS OPEN, AND IT IS THE INGEST TIER. Two different layouts set flag 16
    # and only the clone was measured: the other is a bronze table whose base is the external blob
    # prefix its payload bytes already live at (`ingest/lander.py::create_empty` registers one through
    # `initial_bases`). Its own data files are its own, so compacting it copies nothing — measured on
    # pylance 10.0.0 as 4 fragments -> 1, every row still readable, the base untouched — yet it is
    # refused here, so the estate's widest-rowed, most-fragmented tier accumulates fragments forever
    # while this pass reports a clean pass over it.
    #
    # NOT WIDENED, because the manifest cannot carry the distinction that would make it safe.
    # `service_kit.lakehouse.features.describe_compaction_unsupported_flags` separates a CLONE from a
    # non-clone base (`BasePath.is_dataset_root`, measured), and that much is decidable. What is not
    # is the non-clone half: `initial_bases` (an external blob prefix, no Lance data file will ever
    # live there) and `add_bases` (a registered ALTERNATE base a later write may place data files
    # under) produce BYTE-IDENTICAL manifest entries — verified on pylance 10.0.0 — and this service
    # already refuses the second on purpose
    # (`tests/unit/test_maintenance_optimize.py::test_compaction_refuses_a_registered_but_unused_base`,
    # whose stated reason is exactly "the very next write can land under that base"). Permitting one
    # permits the other. Which way that goes is an owner's call, not a gate's; until it is made this
    # keeps the strict flags-only refusal.
    gc_refusal = describe_gc_unsupported_flags(reader_flags, writer_flags)
    if gc_refusal is not None:
        log.warning("maintenance_refused_unsupported_features", extra={"uri": uri, "reason": gc_refusal})
        return DatasetResult(uri=uri, refused=gc_refusal)
    compact_refusal = describe_unsupported_flags(reader_flags, writer_flags)
    # #114 — the OTHER direction, and the flag check above cannot see it. Flag 16 marks the dataset
    # that SPANS bases (the clone); the dataset in danger here is the SOURCE, which carries no flag
    # and no base_paths of its own and looks completely ordinary. Only the cross-estate pre-pass
    # (`base_refs.protected_roots`) knows another manifest resolves through these bytes.
    #
    # MEASURED, and the mechanism is not what the issue title says: `compact_files` ADDS the merged
    # file and deletes nothing (4 -> 5 files, clone still opens); `cleanup_old_versions` then removes
    # the obsoleted originals (-> 1 file) and the clone fails to open IN A FRESH PROCESS. Since this
    # function runs compact -> optimize_indices -> cleanup as one pass, the refusal belongs here, in
    # front of all three, rather than in front of compaction alone.
    if protected is not None and (root := protected.is_protected(uri)) is not None:
        why = f"another dataset resolves its files through {root} (shallow clone / multi-base) — compacting or reclaiming here would break it"
        log.warning("maintenance_refused_protected_base", extra={"uri": uri, "reason": why})
        return DatasetResult(uri=uri, refused=why)
    # Read the producer's DECLARED name while the dataset is open — the emit path downstream holds only
    # a URI, and for the cascade's own tiers a URI cannot be resolved to a name at all. Never fatal: a
    # dataset with no declared id simply falls back to the URI derivation, which is the common case
    # until producers stamp it and remains the case for every dataset already on disk.
    result = DatasetResult(uri=uri, declared_table_id=declared_table_id(ds))
    try:
        # defer_index_remap: with the Fragment Reuse Index the row-id remap is deferred, so compaction and
        # index maintenance "no longer conflict" (lance_docs/guide.md:3150) — cuts the CommitConflict class
        # of maintain: failures at the source. The optimize_indices() right below folds the compacted
        # fragments into the indices; the interplay is pinned by
        # tests/unit/test_maintenance_optimize.py::test_compact_one_defer_index_remap_keeps_indices_working.
        # #76 target-size tuning: the #50 policy's target_rows_per_fragment (None → Lance default sizing).
        size_kw: dict[str, Any] = {"target_rows_per_fragment": target_rows_per_fragment} if target_rows_per_fragment else {}
        # Rows are not a unit of memory — see the docstring. Passed to BOTH compaction attempts below,
        # because the fallback path reads exactly the same bytes as the deferred one.
        if scan_batch_size is not None:
            size_kw["batch_size"] = scan_batch_size
        if compact_threads is not None:
            size_kw["num_threads"] = compact_threads
        if compact_refusal is not None:
            # Root-scoped work still runs below; only the rewrite is skipped. Recorded on the result so
            # the reason is visible without inferring it from a zero, and logged once per dataset.
            log.info("maintenance_compaction_skipped_unsupported", extra={"uri": uri, "reason": compact_refusal})
            result.refused = compact_refusal
            metrics: Any = None
        else:
            try:
                metrics = ds.optimize.compact_files(defer_index_remap=True, **size_kw)
            except Exception as exc:
                # defer_index_remap needs row_addrs (a stable-row-id, fragment-reuse-able layout). A dataset
                # WITHOUT them — e.g. a small model-REGISTRY dataset (models$<model>) — raises
                # "defer_index_remap requires row_addrs but none were provided". Fall back to the plain
                # (non-deferred) compaction so one such dataset doesn't get reported as a sweep failure. These
                # registry datasets aren't concurrently indexed, so the CommitConflict that defer_index_remap
                # avoids isn't a risk here. Any OTHER error propagates to the outer per-dataset error capture.
                # MATCH THE PARAMETER, NOT ONE MESSAGE. Lance refuses `defer_index_remap` in BOTH
                # directions and this only caught one:
                #   * no stable row ids -> "defer_index_remap requires row_addrs but none were provided"
                #   * WITH stable row ids -> "defer_index_remap=true is not supported on datasets with
                #     stable row IDs: ... there is nothing to defer."
                # The second carries no `row_addrs`, so it re-raised and became a per-dataset sweep error.
                # Measured on the live estate 2026-08-16: a real sweep reported datasets 31,
                # fragments_removed 0, errors 11 — every one of them this, because the medallion cascade
                # writes with stable row ids, so the datasets the pipeline produces were exactly the ones
                # compaction could never touch. Maintenance ran and reclaimed nothing.
                # Either refusal means the same thing operationally: this dataset does not want the
                # deferred remap, so compact without it. Anything else still propagates.
                if "defer_index_remap" not in str(exc):
                    raise
                log.warning("compact_defer_index_remap_unsupported", extra={"uri": uri, "error": str(exc)})
                metrics = ds.optimize.compact_files(**size_kw)
        result.fragments_removed = int(getattr(metrics, "fragments_removed", 0))
        result.fragments_added = int(getattr(metrics, "fragments_added", 0))
        # Keep secondary indices (vector ANN / scalar / FTS) covering the new fragments. WITHOUT this a
        # freshly-written row isn't in the index → vector/filter queries either miss it or fall back to a
        # flat scan. Index optimize is a maintenance op exactly like compaction (Lance does it distributed
        # via medallion-producer; here single-process). Idempotent. Own guard so a no-index dataset can't fail it.
        if not optimize_indices_enabled:
            # A policy may skip a STEP; it may not reorder them. Skipping index optimization after a
            # compaction leaves the new fragments unindexed until the next enabled pass — queries fall
            # back to a flat scan rather than returning wrong rows, which is why this is a legal choice.
            log.info("optimize_indices_disabled_by_policy", extra={"uri": uri})
        try:
            if optimize_indices_enabled:
                ds.optimize.optimize_indices()
                # Count USER indices only: defer_index_remap creates the ``__lance_frag_reuse`` SYSTEM index,
                # which would otherwise report every ever-compacted dataset as "index maintained" forever —
                # phantom signal in the reclaim metrics (review 2026-07-10, verified on pylance 8.0.0).
                # Counted INSIDE the branch: the metric names work that was DONE, so a skipped step
                # must report 0 rather than the number of indices that happen to exist. Reporting a
                # count for a step that did not run is the same dishonesty as a toast built from the
                # request instead of the response.
                #
                # `describe_indices`, not the DEPRECATED `list_indices` pylance 10 warns on: the two do
                # not agree at this call site, so waiting for the removal would have silently changed
                # the number. `list_indices` fans an index out into one dict PER SEGMENT (its own body
                # is a nested comprehension over `describe_indices()`), so a delta-heavy index counted
                # several times; one object per index is what "indices optimized" has always meant.
                # It is also the shape `index_health.inspect_indices` already reads two lines below.
                result.indices_optimized = len([ix for ix in ds.describe_indices() if not str(ix.name).startswith("__")])
                # #60 — AFTER the optimize, ask what it did NOT fix. `indices_optimized` counts the
                # call; these are the states that survive it (rows still unindexed, delta fan-out, a
                # column with no index at all), each of which degrades queries to a full scan while
                # this pass reports success. REPORT only: the repair for three of the four is a
                # rebuild, whose cost belongs to a policy and an operator rather than to a cron tick
                # that was asked to compact.
                result.index_findings = [f.model_dump() for f in inspect_indices(ds, expected_columns=index_columns)]
                if result.index_findings:
                    log.warning("maintenance_index_findings", extra={"uri": uri, "findings": len(result.index_findings)})
        except BaseException as exc:  # noqa: BLE001 — a Rust PANIC is not an Exception; see below
            # This guard already existed and already had the right INTENT — index work is best-effort,
            # so a dataset with no index, or an unreadable one, must not lose the compaction that just
            # succeeded. It only caught `Exception`, and `index_stats` PANICS on index types Lance has
            # no stats implementation for. A pyo3 panic derives from BaseException, so it sailed past
            # here into the per-dataset capture and turned a REPORT failure into a dataset failure —
            # discarding `fragments_removed` from a compaction that had already happened.
            if isinstance(exc, KeyboardInterrupt | SystemExit):
                raise
            log.warning("optimize_indices_skipped", extra={"uri": uri, "error": str(exc), "error_type": type(exc).__name__})
        # error_if_tagged_old_versions=False: tagged versions are EXEMPT from GC (they survive until the tag
        # is deleted). The default (True) RAISES once any tag ages past older_than — which, since the catalog
        # creates long-lived promotion tags, would permanently stall GC for that dataset (the raise is caught
        # and recorded as error, reclaiming nothing). We want GC to skip tagged versions and reclaim the rest.
        # retain_versions (#50 policy override): with ``older_than=None`` it is pure count-based
        # retention ("keep exactly the last N"); when both are set, a version must clear *both* bounds
        # to be removed. Never pass both as None — pylance then substitutes a 14-day default.
        # `cleanup_enabled=False` keeps the ENTIRE version history: a tier under legal hold, or one
        # whose time-travel window is the product. Compaction may still run — it changes layout, not
        # history — so this is a real per-step choice rather than an all-or-nothing opt-out.
        #
        # #58: when the DATASET owns version reclamation, configure it here and do not also sweep.
        # Applied AFTER compaction so a failure to configure can never cost us the compaction that
        # already succeeded, and recorded on the result so an operator can see which owner ran.
        # ORDER MATTERS, and it used to be wrong. This was `if auto_cleanup … elif cleanup_enabled …
        # else`, which made `cleanup_enabled=False` UNREACHABLE whenever a policy also set
        # `auto_cleanup_interval_commits` — so a tier under legal hold handed its own commit path a
        # standing instruction to delete the versions the hold existed for. A hold must beat a
        # convenience, so the disable is checked FIRST and nothing below can override it.
        if not cleanup_enabled:
            if auto_cleanup_interval_commits is not None:
                # Reported, not silently dropped: the policy asks for two contradictory things, and the
                # operator needs to know which one won.
                log.warning(
                    "auto_cleanup_suppressed_by_cleanup_disabled",
                    extra={"uri": uri, "interval_commits": auto_cleanup_interval_commits},
                )
            log.info("cleanup_disabled_by_policy", extra={"uri": uri})
        elif auto_cleanup_interval_commits is not None:
            try:
                from lance.dataset import AutoCleanupConfig

                # AutoCleanupConfig is a TypedDict keyed in SECONDS, not a timedelta — the 14-day
                # fallback mirrors what pylance substitutes when neither bound is given.
                older_than_seconds = int((older_than or timedelta(days=14)).total_seconds())
                # ONLY WRITE WHEN IT WOULD CHANGE SOMETHING. `enable_auto_cleanup` is `update_config`,
                # which is a Lance TRANSACTION even when the config is byte-identical — measured on
                # pylance 9.0.0: three identical calls took a dataset from version 1 to 4. This runs on
                # every policied dataset every 120s, so the reclaimer was the estate's most prolific
                # VERSION PRODUCER: it manufactured exactly the history it exists to remove, and each
                # new version resets that dataset's age-based cleanup window.
                # `config` is a METHOD on pylance 9.0.0, not a property — read defensively so a future
                # release flipping it either way cannot turn "already configured" into a silent rewrite.
                raw = getattr(ds, "config", None)
                current = dict((raw() if callable(raw) else raw) or {})
                already = (
                    current.get("lance.auto_cleanup.interval") == str(auto_cleanup_interval_commits)
                    and current.get("lance.auto_cleanup.older_than") == f"{older_than_seconds}s"
                )
                if already:
                    result.auto_cleanup_configured = True
                else:
                    ds.optimize.enable_auto_cleanup(
                        AutoCleanupConfig(interval=auto_cleanup_interval_commits, older_than_seconds=older_than_seconds),
                    )
                    result.auto_cleanup_configured = True
            except Exception as exc:
                # Not fatal: the dataset keeps whatever cleanup config it had, and the NEXT pass retries.
                # It IS reported, because silently falling back to no cleanup at all is how a tier grows
                # versions forever while its policy says it is being reclaimed.
                log.warning("auto_cleanup_enable_failed", extra={"uri": uri, "error": str(exc)})
                result.error = f"auto_cleanup: {exc}"
                result.error_type = type(exc).__name__
        else:
            # cleanup_enabled is necessarily True here — the disable is handled first, above — so this
            # is the sweep-owned reclamation path and needs no second check.
            stats: Any = ds.cleanup_old_versions(older_than=older_than, retain_versions=retain_versions, error_if_tagged_old_versions=False)
            result.old_versions_removed = int(getattr(stats, "old_versions", 0))
            result.bytes_removed = int(getattr(stats, "bytes_removed", 0))
    except BaseException as exc:  # noqa: BLE001 — a Rust PANIC is not an Exception; see below
        # `BaseException`, deliberately, and this is the LAST line of defence for the whole sweep.
        # pylance can PANIC (`pyo3_runtime.PanicException: not yet implemented` out of
        # `index_stats`), and a pyo3 panic derives from BaseException, so `except Exception` lets it
        # past. Measured on the live estate 2026-08-16: one panicking index answered the entire sweep
        # HTTP 500 — every OTHER dataset lost its compaction and version reclamation to one dataset's
        # unreadable index report.
        #
        # This function's contract is per-dataset isolation: `run_sweep` collects a DatasetResult per
        # uri and reports failures alongside successes. A failure mode that escapes that contract
        # turns a partial problem into a total outage, which is exactly what happened.
        if isinstance(exc, KeyboardInterrupt | SystemExit):
            raise
        result.error = f"maintain: {exc}"
        result.error_type = type(exc).__name__
    return result
