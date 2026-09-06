"""The compaction + GC core — infra-light so the discovery + aggregation logic is unit-testable.

``discover_datasets`` is pure list-logic over a pyarrow filesystem; ``compact_one`` wraps the two
blocking Lance maintenance calls. Both keep IO at the edges so the orchestration can be tested with fakes.

(The function was ``discover_dataset_uris`` until it started returning a :class:`Discovery` — uris AND
the prefixes the depth bound stopped at — because the truncation must not be droppable. This docstring
kept the dead name, which is how a reader ends up grepping for a symbol that has no definition.)
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Protocol

import lance
import pyarrow.fs as pafs
from pydantic import BaseModel, Field

from maintenance.core.config import shared_lance_session
from maintenance.core.lineage_emit import declared_table_id
from maintenance.services.compaction_executor import CompactionPlaneUnavailable, DistributedOutcome
from maintenance.services.index_health import inspect_indices
from service_kit.lakehouse.base_refs import BaseRefs
from service_kit.lakehouse.features import (
    FLAG_BASE_PATHS,
    describe_compaction_unsupported_flags,
    describe_gc_unsupported_flags,
    gather_compaction_bases,
    manifest_feature_flags,
    unsupported_features_from_open_error,
)
from service_kit.lakehouse.objectfs import dataset_root_probe


log = logging.getLogger(__name__)


class Rewriter(Protocol):
    """How a caller offers the off-pod rewrite.

    A PROTOCOL rather than a client this module builds, for the reason this module's own docstring
    gives — it keeps IO at the edges so the orchestration stays testable with fakes — and because the
    transport is the sweep's business: today the catalog's HTTP doors, at M3 a `RayJob` submission,
    with nothing here changing either time.

    `table_id` is keyword-only, and that is a real guard rather than style: it and `uri` are both
    `str`, so positionally they are silently swappable — a caller that transposed them would sign a
    rewrite for one table against another's location and nothing would type-check it wrong.
    """

    def __call__(self, uri: str, *, table_id: str, options: Mapping[str, Any]) -> DistributedOutcome: ...


class DatasetResult(BaseModel):
    """What one dataset's maintenance pass did (or why it was skipped)."""

    uri: str
    #: ``in_pod`` or ``distributed`` — WHICH path rewrote the bytes.
    #:
    #: Carried rather than inferred, because the fallback is silent by design: a plan door that
    #: cannot answer degrades to the in-pod rewrite so disk keeps being reclaimed, and without this
    #: field an estate could run every compaction on the path it was configured away from while every
    #: counter read clean. The value is what a sweep summary counts, so the degradation is a number
    #: rather than an inference.
    compaction_mode: str = "in_pod"
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


def _compact_files(
    ds: lance.LanceDataset,
    result: DatasetResult,
    *,
    uri: str,
    refusal: str | None,
    target_rows_per_fragment: int | None,
    scan_batch_size: int | None,
    compact_threads: int | None,
    rewrite: Rewriter | None = None,
    table_id: str | None = None,
) -> None:
    """STEP 1 — merge small fragments, or record why the rewrite was declined.

    Extracted from ``compact_one``'s body (MAINT-09), where three ordered operations, their two nested
    fallback guards and the whole refusal ladder shared one 75-statement function. The order compact ->
    optimize indices -> cleanup is FIXED and lives in the caller; each step is its own function so a
    failure can be read against the step that produced it.
    """
    # defer_index_remap: with the Fragment Reuse Index the row-id remap is deferred, so compaction and
    # index maintenance "no longer conflict" (lance_docs/guide.md:3150) — cuts the CommitConflict class
    # of maintain: failures at the source. The `_optimize_indices` step that runs next folds the
    # compacted fragments into the indices; the interplay is pinned by
    # tests/unit/test_maintenance_optimize.py::test_compact_one_defer_index_remap_keeps_indices_working.
    # #76 target-size tuning: the #50 policy's target_rows_per_fragment (None → Lance default sizing).
    size_kw: dict[str, Any] = {"target_rows_per_fragment": target_rows_per_fragment} if target_rows_per_fragment else {}
    # Rows are not a unit of memory — see the docstring. Passed to BOTH compaction attempts below,
    # because the fallback path reads exactly the same bytes as the deferred one.
    if scan_batch_size is not None:
        size_kw["batch_size"] = scan_batch_size
    if compact_threads is not None:
        size_kw["num_threads"] = compact_threads
    # THE REWRITE OFF THIS POD, when a rewriter was supplied and this dataset is one the catalog can
    # name. `compact_files` below does plan, execute and commit in one call, so the pod's memory
    # ceiling is a function of the largest table anyone owns; the distributed protocol leaves only the
    # byte rewrite here, signed by the table-scoped credential. Tried BEFORE the refusal ladder's
    # rewrite so a refusal still skips both paths, and after `size_kw` so both are bounded the same.
    if refusal is None and rewrite is not None and table_id:
        try:
            outcome = rewrite(uri, table_id=table_id, options=size_kw)
        except CompactionPlaneUnavailable as exc:
            # Nothing was planned, so nothing was written: falling back is safe and is the only
            # answer that keeps reclaiming disk when the catalog is briefly unreachable. INFO rather
            # than WARNING — the sweep is doing its job — but `compaction_mode` records which path
            # ran, so a permanent degradation is a count rather than a guess.
            log.info("compaction_plane_unavailable_falling_back", extra={"uri": uri, "table_id": table_id, "reason": str(exc)})
        else:
            result.compaction_mode = "distributed"
            result.fragments_added = outcome.fragments_added
            result.fragments_removed = outcome.fragments_removed
            if outcome.tasks_failed:
                # Reported, never swallowed: the successful tasks committed, so the pass DID work —
                # but a half-done compaction that read as clean is worse than either outcome alone.
                result.error = f"compaction: {outcome.tasks_failed} of {outcome.tasks_planned} task(s) failed"
                result.error_type = "PartialCompaction"
            return

    if refusal is not None:
        # Root-scoped work still runs below; only the rewrite is skipped. Recorded on the result so
        # the reason is visible without inferring it from a zero, and logged once per dataset.
        #
        # WARNING, not info, and it matches what `sweep.summarize`'s own docstring already promised
        # ("its visibility is the WARNING log, the `lance.refused` span attribute, the
        # `compaction.datasets.refused` counter"). It was info while the gate refused every flag-16
        # dataset — 17 per tick on this estate, which is a level nobody can leave on. Now that the
        # refusal means a real clone hazard, each line is one an operator should see.
        log.warning("maintenance_compaction_skipped_unsupported", extra={"uri": uri, "reason": refusal})
        result.refused = refusal
        metrics: Any = None
    else:
        try:
            # ASK ONLY WHAT LANCE CAN ANSWER. `defer_index_remap` needs a fragment-reuse layout, and a
            # stable-row-id dataset does not have one — nor need one: stable ids ARE the remap, so
            # Lance refuses with "there is nothing to defer". The dataset carries the answer
            # (`has_stable_row_ids`, the same public accessor `ingest/catalog.py::A14` reads), so the
            # refusal is predictable rather than discoverable. Measured on the deployed estate: 211
            # `compact_defer_index_remap_unsupported` warnings in 48 hours — one guaranteed-failed call
            # per governed dataset per tick, since the cascade writes every tier with stable ids.
            #
            # The except stays, and is not belt-and-braces: Lance refuses in the OTHER direction too
            # ("requires row_addrs but none were provided") for a dataset that is neither, and a third
            # reason should degrade to a plain compaction rather than fail a tick.
            if getattr(ds, "has_stable_row_ids", False):
                metrics = ds.optimize.compact_files(**size_kw)
            else:
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


def _optimize_indices(ds: lance.LanceDataset, result: DatasetResult, *, uri: str, enabled: bool, index_columns: list[str] | None) -> None:
    """STEP 2 — keep the secondary indices (vector ANN / scalar / FTS) covering the new fragments.

    WITHOUT this a freshly-written row is not in the index, so vector/filter queries either miss it or
    fall back to a flat scan. Index optimize is a maintenance op exactly like compaction (Lance does it
    distributed via the medallion producer; here single-process) and is idempotent. Its own guard, so a
    dataset with no index — or an unreadable one — cannot cost the compaction that just succeeded.
    """
    if not enabled:
        # A policy may skip a STEP; it may not reorder them. Skipping index optimization after a
        # compaction leaves the new fragments unindexed until the next enabled pass — queries fall
        # back to a flat scan rather than returning wrong rows, which is why this is a legal choice.
        log.info("optimize_indices_disabled_by_policy", extra={"uri": uri})
    try:
        if enabled:
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


def _reclaim_versions(
    ds: lance.LanceDataset,
    result: DatasetResult,
    *,
    uri: str,
    older_than: timedelta | None,
    retain_versions: int | None,
    cleanup_enabled: bool,
    auto_cleanup_interval_commits: int | None,
) -> None:
    """STEP 3 — reclaim superseded versions, or hand that job to the dataset itself (#58)."""
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


def compact_one(
    uri: str,
    storage_options: dict[str, str],
    older_than: timedelta | None,
    *,
    # KEYWORD-ONLY from here, and these two are why (MAINT-16): `retain_versions` and
    # `target_rows_per_fragment` are both `int | None` and were adjacent positional parameters, so a
    # caller that swapped them type-checked cleanly and handed a RECLAIMER a retention count as a
    # fragment size (and a fragment size as the number of versions to keep). Nothing in the estate
    # could have caught it.
    retain_versions: int | None = None,
    target_rows_per_fragment: int | None = None,
    cleanup_enabled: bool = True,
    optimize_indices_enabled: bool = True,
    scan_batch_size: int | None = None,
    compact_threads: int | None = None,
    auto_cleanup_interval_commits: int | None = None,
    protected: BaseRefs | None = None,
    index_columns: list[str] | None = None,
    rewrite: Rewriter | None = None,
) -> DatasetResult:
    """One ORDERED maintenance pass over one dataset. Never raises — a per-dataset failure is captured
    in ``error`` so one bad dataset can't abort the whole pass.

    The order is compact → optimize indices → cleanup, and it is FIXED, not configurable: compaction
    leaves its new fragments unindexed, so index optimization must follow it, and cleanup runs LAST
    because it reclaims the superseded versions that BOTH earlier steps produced, in one pass. Each of
    the three is its own function above (:func:`_compact_files`, :func:`_optimize_indices`,
    :func:`_reclaim_versions`); this one opens the dataset, runs the refusal ladder, and sequences them
    under one per-dataset error capture. (This
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
    any rewrite (#64, :mod:`service_kit.lakehouse.features`) — see :attr:`DatasetResult.refused`. The
    COMPACTION half of that gate does not stop at the flag: ``base_paths`` (16) is set both by a
    shallow clone, where a rewrite silently materialises another root's data, and by a dataset that
    merely registers an external blob prefix, where a rewrite is an ordinary merge. Which one this is
    comes from ``gather_compaction_bases`` — the manifest's self-report, the object store's own answer
    for each base, and whether any ``DataFile`` resolves through one — and any of those coming back
    unknown REFUSES.
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
    # AND THE COMPACTION GATE ASKS ABOUT THE BASES, NOT ABOUT THE FLAG. Refusing on flag 16 alone was
    # over-broad by exactly the shape the cascade writes: `ingest/lander.py::create_empty` and
    # `medallion/services/compute.py` both register ONE external blob prefix through `initial_bases`,
    # which sets flag 16 while every data file stays under this dataset's own root. Measured on pylance
    # 10.0.0, compacting that is a merge and nothing more — 4 fragments -> 1, the base directory
    # byte-identical, 20/20 external payloads still resolving — yet it was refused, so the estate's
    # most-fragmented tiers accumulated fragments forever while this pass reported a clean sweep over
    # them (`fragments_removed_total=0` across 785 ticks).
    #
    # `gather_compaction_bases` takes the three readings `describe_compaction_unsupported_flags`
    # weighs, and the object-store probe is the load-bearing one: `BasePath.is_dataset_root` is set by
    # `shallow_clone` and by nothing else, so an `add_bases` pointed at a live Lance root reports False
    # and a manifest-only gate would wave the clone shape straight through. `DataFile.base_id` catches
    # the third shape neither of those sees — `target_bases=[...]` puts OUR files under a base that is
    # a dataset root by neither reading (measured: compacting it pulled 3,540 -> 5,991 bytes home and
    # orphaned the base's three files).
    #
    # IT FAILS CLOSED, and the asymmetry is the point: an unreadable probe, an unparseable BasePath or
    # an unreadable fragment list all REFUSE. A wrong refusal costs disk and prints a counted line in
    # the sweep summary; a wrong permit costs a clone its entire reason to exist.
    gc_refusal = describe_gc_unsupported_flags(reader_flags, writer_flags)
    if gc_refusal is not None:
        log.warning("maintenance_refused_unsupported_features", extra={"uri": uri, "reason": gc_refusal})
        return DatasetResult(uri=uri, refused=gc_refusal)
    compact_refusal = describe_compaction_unsupported_flags(
        reader_flags,
        writer_flags,
        # Gathered ONLY when the flag is actually set: this is the one place the gate costs IO (one
        # `get_file_info` per declared base), and the overwhelming majority of datasets declare none.
        #
        # `dataset_root_probe` binds the probe to THIS dataset's store, and that binding is load-bearing
        # rather than tidy: on S3 a manifest states its base as `/bucket/ns/t.lance` while this `uri` is
        # `s3://bucket/ns/t.lance` (the two spellings `base_refs.normalise` exists to reconcile).
        # Probing the schemeless form directly reads it as a LOCAL absolute path, finds nothing, and
        # answers "not a dataset root" — a wrong PERMIT on a real clone, which is the one direction
        # this gate must never take.
        gather_compaction_bases(ds, dataset_root_probe(uri, storage_options)) if (reader_flags | writer_flags) & FLAG_BASE_PATHS else None,
    )
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
        # THE ORDER IS FIXED, and it is the whole reason these three are separate functions rather than
        # a configurable list: compaction leaves its new fragments unindexed, so index optimization must
        # follow it, and reclamation runs LAST because it collects the superseded versions that BOTH
        # earlier steps produced, in one pass.
        _compact_files(
            ds,
            result,
            uri=uri,
            refusal=compact_refusal,
            target_rows_per_fragment=target_rows_per_fragment,
            scan_batch_size=scan_batch_size,
            compact_threads=compact_threads,
            rewrite=rewrite,
            # The id the PRODUCER stamped on the dataset, already resolved above. Deriving a second
            # one from the path here would be a second answer to the same question, and the two
            # disagree for most of the estate — `credentials.write_options_for` measures it: of eleven
            # top-level roots, path derivation answers for six, and the five it misses include the
            # cascade. Passing `None` correctly leaves this dataset on the in-pod rewrite.
            table_id=result.declared_table_id,
        )
        _optimize_indices(ds, result, uri=uri, enabled=optimize_indices_enabled, index_columns=index_columns)
        _reclaim_versions(
            ds,
            result,
            uri=uri,
            older_than=older_than,
            retain_versions=retain_versions,
            cleanup_enabled=cleanup_enabled,
            auto_cleanup_interval_commits=auto_cleanup_interval_commits,
        )
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
