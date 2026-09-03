"""The maintenance sweep: discover every dataset in the bucket, compact + GC each, aggregate the result.

Since #50, a per-table/namespace maintenance policy (from the catalog's ``_policies/`` registry) can skip
a dataset (``policy_disabled`` / ``policy_interval``) or override its old-version retention; #84 adds a
project-level record as the tenant-wide fallback (resolution: table > namespace > project > global
defaults, all inside ``maintenance_policies.resolve_policy``); a policy-less dataset keeps the global
defaults. Keeps the blocking S3/Lance orchestration out of the route so the cron
handler stays a thin shell and the aggregation (:func:`summarize`) stays unit-testable without S3.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import pyarrow.fs as pafs
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from maintenance.core.config import MaintenanceSettings, shared_lance_session
from maintenance.core.lineage_emit import MaintenanceEmitter, table_id_from_uri
from maintenance.core.metrics import (
    record_dataset_swept,
    record_failed,
    record_reclaimed,
    record_refused,
    record_run,
    record_run_started,
    record_trashed_skipped,
)
from maintenance.services import purge
from maintenance.services.optimize import DatasetResult, compact_one, discover_datasets
from maintenance.services.tiers import target_rows_for
from service_kit.governed import fga
from service_kit.lakehouse import base_refs, maintenance_policies, trash, warehouse_records
from service_kit.lakehouse.objectfs import s3_filesystem
from service_kit.lakehouse.work_items import DatasetPlan, DatasetWorkItem


log = logging.getLogger(__name__)

#: Per-tick cap on FAIL-event publishes. Each publish is already bounded by the emitter's 5s timeout and
#: the batch is gathered concurrently, but an unbounded fan-out over a bucket where EVERYTHING is failing
#: could still push the cron handler past the 30s Dapr ack window — cap it, and LOG what was dropped
#: (a silent cap would read as "covered everything"). The over-cap set is SHUFFLED before the cut: the
#: discovery listing order is deterministic, so a fixed head-slice would re-drop the SAME datasets every
#: tick and their FAIL would never emit — shuffling gets every failing dataset through within a few ticks,
#: converging on its deterministic run id (review 2026-07-10).
#:
#: THE CAP APPLIES TO THE FAIL LANE ONLY, and that asymmetry is now a decision rather than an accident
#: (MAINT-04). A FAIL condition persists: a dataset that failed this tick fails the next one too, so a
#: dropped FAIL emit is re-attempted in ~2 minutes and converges on its own deterministic run id. A
#: COMPLETE emit has no second chance — the material work it reports happened ONCE, and next tick that
#: dataset has nothing left to reclaim and produces no event at all. Capping it would not delay a
#: maintenance run in the lineage graph, it would erase it. So the COMPLETE lane is bounded by
#: CONCURRENCY (below) instead of by count: same protection against a fan-out of publishes, no loss.
_MAX_FAIL_EMITS_PER_TICK = 25

#: How many publishes may be in flight at once, in EITHER lane. The lanes used to disagree about this
#: too: FAIL gathered its whole (capped) batch while COMPLETE was awaited one dataset at a time inside
#: the discovery loop, so an estate where every dataset reclaimed something paid the emitter's 5s
#: timeout serially, once per dataset. Bounded rather than unbounded because a whole-estate gather is a
#: fan-out of HTTP publishes at one sidecar, which is the hazard the FAIL cap was reaching for.
_MAX_CONCURRENT_EMITS = 25

# Each compact+GC is blocking Lance/S3 work invisible to auto-instrumentation — a per-dataset INTERNAL
# span lets a slow or failing compaction be localized in the trace instead of hiding in the cron handler.
tracer = trace.get_tracer(__name__)


def _s3fs(settings: MaintenanceSettings) -> pafs.S3FileSystem:
    """A pyarrow S3 filesystem over the RustFS endpoint — used only to LIST the bucket."""
    return s3_filesystem(settings.storage_options())


def _policy_skip_reason(
    policy: dict[str, object],
    *,
    settings: MaintenanceSettings,
    options: dict[str, str],
    now: datetime,
    uri: str,
) -> str | None:
    """Why the policy says to skip this dataset this tick, or ``None`` to maintain it.

    Keyword-only past the policy it is deciding about: five positional parameters, two of them a
    ``dict`` each and two of them describing the same dataset from different angles, is a call site
    nobody can check by reading it (MAINT-16).

    ``compact_enabled=False`` opts the target out entirely; ``compact_interval_hours`` skips until the
    interval has elapsed since the sweep's own per-dataset ``last_maintained_at`` stamp (an unreadable,
    absent, or malformed stamp maintains — never the other way, or a lost stamp would silence maintenance
    forever — and is logged, so a persistently broken state prefix is visible, not just ineffective).
    """
    if not policy.get("compact_enabled", True):
        return "policy_disabled"
    interval = policy.get("compact_interval_hours")
    if interval:
        try:
            stamped = maintenance_policies.read_state(settings.resolved_policy_root, options, policy, uri)
        except Exception as exc:
            log.warning(
                "compaction_policy_state_unreadable",
                extra={"policy": policy.get("id"), "uri": uri, "error": str(exc)},
            )
            stamped = None
        if stamped is not None:
            # TypeError covers a naive stamp: `now` is aware, and aware-minus-naive raises.
            try:
                last = datetime.fromisoformat(stamped)
                if now - last < timedelta(hours=int(str(interval))):
                    return "policy_interval"
            except (ValueError, TypeError):
                log.warning(
                    "compaction_policy_state_malformed",
                    extra={"policy": policy.get("id"), "uri": uri, "stamp": stamped},
                )
    return None


def _load_policies(settings: MaintenanceSettings, options: dict[str, str]) -> list[dict[str, Any]]:
    """The #50 maintenance policies, loaded once per tick; a policy-less dataset keeps the global defaults.

    A registry we cannot read at all ABORTS the tick (fail toward not deleting, audit 2026-07-16):
    policies are the protective surface (retention extensions, opt-outs), so sweeping without them would
    GC version history an owner explicitly kept. The next cron fire retries; one bad record inside a
    readable registry is skipped-with-warning by ``list_policies``, not fatal.
    """
    try:
        policy_records = maintenance_policies.list_policies(settings.resolved_policy_root, options)
    except Exception:
        log.error("compaction_policies_unreadable_tick_aborted")
        raise
    # The count distinguishes "no policies set" from "policies invisible" (e.g. the catalog's control
    # root moved without MAINTENANCE_POLICY_ROOT following — a wrong root lists cleanly as empty).
    log.info("compaction_policies_loaded", extra={"policies": len(policy_records)})
    return policy_records


def _buckets_to_sweep(settings: MaintenanceSettings, options: dict[str, str]) -> list[str]:
    """#81 EVERY warehouse the registry knows, not just the configured list.

    ``sweep_buckets`` is the primary bucket plus a static env var — but a per-warehouse bucket is created
    by an API CALL at runtime, so every tenant provisioned since the last config edit was invisible to
    maintenance and its tables accumulated superseded versions and small fragments forever. A storage
    leak created by the very feature that introduces new buckets, and silent: the sweep reported success
    over the buckets it did know.

    An unreadable registry is NOT fatal here (unlike the policy registry, whose absence would mean
    sweeping without protective retention overrides) — it degrades to the configured list, which is
    exactly the old behaviour, and says so.
    """
    buckets = list(settings.sweep_buckets)
    try:
        registry = warehouse_records.list_warehouse_records(settings.resolved_control_root, options)
        discovered = [b for b in warehouse_records.maintainable_buckets(registry) if b not in buckets]
        buckets.extend(discovered)
        log.info("sweep_registry_buckets", extra={"configured": len(settings.sweep_buckets), "from_registry": len(discovered)})
    except Exception as exc:  # noqa: BLE001 — a missing registry must not stop maintaining what we know
        log.warning("sweep_registry_unreadable", extra={"error": str(exc)})
    return buckets


def _discover_all(fs: pafs.FileSystem, buckets: list[str]) -> list[str]:
    """Every dataset URI across every swept bucket, reporting the prefixes the walk could not reach.

    A bucket that does not exist (or is unreadable) is skipped, not fatal: one missing tenant bucket
    must not stop the sweep for everyone else.

    The truncation set is accumulated AND CONSUMED, which it was not: it was collected here and read by
    nothing, so ``Discovery``'s own docstring — "the sweep counts it, the reconciler files an
    IncompleteScan" — was true of the reconciler and false of the sweep. A tick that never walked a
    prefix was indistinguishable from one that walked everything, which is exactly the "0 that means we
    did not look" this module's docstrings forbid elsewhere.
    """
    uris: list[str] = []
    truncated: list[str] = []
    for bucket in buckets:
        try:
            found = discover_datasets(fs, bucket)
        except Exception as exc:  # noqa: BLE001 — one unreadable bucket must not stop the whole sweep
            log.warning("compaction_bucket_skipped", extra={"bucket": bucket, "error": str(exc)})
            continue
        log.info(
            "compaction_bucket_discovered",
            extra={"bucket": bucket, "datasets": len(found.uris), "truncated": len(found.truncated)},
        )
        uris.extend(found.uris)
        truncated.extend(found.truncated)
    if truncated:
        log.warning(
            "maintenance_discovery_truncated",
            extra={"prefixes": len(truncated), "examples": sorted(truncated)[:5], "max_depth": 3},
        )
    return uris


def _trash_exclusions(settings: MaintenanceSettings, options: dict[str, str]) -> dict[str, str]:
    """``{normalised path: why}`` for every dataset in the trash — the datasets this tick may NOT rewrite.

    #75 trash expiry is REPORT ONLY **in the sweep**, permanently. #79's purge lives on the RECONCILE
    tick instead, because its gate is that tick's drift report: a reclaimer earns its delete permission
    by first proving the report runs clean, and the sweep does not produce that report. So the sweep
    keeps naming which recoverable drops are past their deadline and keeps deleting NOTHING.

    ONE read of the trash index per tick, TWO consumers (F6(d)). The CONTROL root, not the policy root:
    the catalog writes ``_trash/`` under its registry root (``LANCE_CONTROL_ROOT``). These default to the
    same bucket, so a mismatch is invisible.

    THE POSTURE HERE IS DELIBERATE. This read used to be except-wrapped with "the trash report must never
    abort a maintenance tick" — correct while the only consumer was a log line. It is not correct now
    that the same read decides WHICH DATASETS THIS TICK MAY REWRITE. An unreadable trash index would
    silently restore the exact defect this exclusion exists to close: the sweep compacting and
    version-cleaning datasets an owner was told are frozen and recoverable. So it follows the policy
    registry's rule verbatim: a PROTECTIVE registry we cannot read at all ABORTS the tick, failing toward
    not-rewriting. The next cron fire retries; one unparseable record inside a readable index is already
    skipped-with-warning by ``trash.list_all``, not fatal.

    EVERY record, not just the expired ones. An expired-but-unpurged record is still frozen data:
    ``undrop`` deliberately does not consult the clock, so a passed deadline is not permission to
    rewrite — it only means the purge is entitled to reclaim it, and until it has, the bytes stand.
    """
    try:
        trash_records = trash.list_all(settings.resolved_control_root, options)
    except Exception:
        log.error("compaction_trash_index_unreadable_tick_aborted")
        raise
    # Guarded against the empty location: a trashed NAMESPACE record and a declared-only table both
    # carry `location=""`, and `normalise("")` is `""`, which is a prefix of everything.
    trashed_by_path: dict[str, str] = {}
    for record in trash_records:
        location = str(record.get("location") or "")
        if not location:
            continue
        trashed_by_path[base_refs.normalise(location)] = f"in trash as {record.get('id') or '?'} (expires_at={record.get('expires_at') or '?'})"
    # Selection goes through `purge.due_from` so ONE rule decides what "expired" means: the set this
    # logs and the set the purge deletes cannot be two different answers. Fed from the records already
    # in hand — a second `list_all` here would double the index read for nothing.
    if due := purge.due_from(trash_records):
        log.info(
            "trash_expiry_due_report_only",
            extra={"count": len(due), "ids": [str(r.get("id")) for r in due][:20]},
        )
    return trashed_by_path


def _protected_roots(uris: list[str], options: dict[str, str]) -> base_refs.BaseRefs:
    """#128d/#114 THE PRE-PASS — over every discovered dataset in EVERY bucket, before one is compacted.

    It has to be whole-estate and it has to be first: a shallow clone in bucket B is the only thing that
    knows bucket A's dataset must not be touched, so a per-bucket or per-dataset check cannot see it. The
    SOURCE carries no feature flag and no ``base_paths`` of its own (measured), which is why the flag
    gate inside ``compact_one`` misses this entirely.

    Cost is one manifest read per dataset and no data file is opened — the same order as discovery
    itself, paid once per tick. The SERVICE's session, not one minted per call:
    ``MAINTENANCE_LANCE_METADATA_CACHE_MB`` / ``_INDEX_CACHE_MB`` are the operator's cap, and this
    pre-pass is the one loop that opens EVERY dataset in the estate. Omitting it let base_refs mint its
    own default-sized session, so a tuned-down cap silently did not apply here and a second session
    competed with the tick's.
    """
    protected = base_refs.protected_roots(uris, options, session=shared_lance_session())
    if protected.protected:
        log.info("maintenance_protected_bases", extra={"count": len(protected.protected), "roots": sorted(protected.protected)[:20]})
    if protected.unreadable:
        # NOT fatal, and not silent either. An unreadable dataset might have been the referrer whose
        # base_paths protect a dataset this tick is about to rewrite, so the gap is named — the same
        # rule the orphan scan follows with `checked=False`. Escalating to a refusal of the whole tick
        # would let one corrupt dataset stop maintenance estate-wide.
        log.warning(
            "maintenance_base_refs_incomplete",
            extra={"count": len(protected.unreadable), "datasets": [uri for uri, _ in protected.unreadable][:20]},
        )
    return protected


def _exclude_trashed(uris: list[str], trashed_by_path: dict[str, str]) -> tuple[list[str], list[DatasetResult]]:
    """Split the discovered URIs into the ones this tick may rewrite and a result per excluded dataset.

    F6(d) THE TRASH EXCLUSION, and its POSITION is the load-bearing part.

    A recoverable drop deregisters the table's ``__manifest`` row and files a trash record. It moves no
    byte: the dataset directory keeps its ``_versions/`` child, which is the ONLY thing
    ``discover_datasets`` consults to decide "this is a Lance dataset". So a dropped table was
    rediscovered on the very next tick, indistinguishable from a live one, and compacted +
    index-remapped + ``cleanup_old_versions``'d every 30 minutes for its whole grace window — silently
    rewriting the history the window promises, and counting it as a successful maintenance pass.

    AFTER :func:`_protected_roots` and BEFORE the maintenance loop, never inside ``discover_datasets``:

     * The pre-pass must keep the FULL discovered list. A trashed shallow CLONE's manifest base_paths
       are the only evidence protecting its LIVE source from compaction (#114/#128d). Filtering before
       the pre-pass would re-open that data-loss defect from the trash side.
     * ``discover_datasets`` has three other callers — the orphan scan (``reconcile.py``), the purge's
       ``_estate_base_refs``, and this sweep. Excluding there would be a silent coverage reduction in
       the first and would remove the same protection evidence from the second.

    COUNTED, never silently dropped — a tick that maintains 40 of 42 datasets has to say what happened
    to the other two, and at cron frequency with nobody watching, a bare count is not actionable. Each
    result carries the record id and its deadline, so an exclusion that has outlived its deadline (a
    record the purge keeps refusing) reads as permanent rather than transient.
    """
    skipped_trashed = [(uri, trashed_by_path[base_refs.normalise(uri)]) for uri in uris if base_refs.normalise(uri) in trashed_by_path]
    record_trashed_skipped(len(skipped_trashed))
    if not skipped_trashed:
        return uris, []
    log.info(
        "maintenance_trashed_excluded",
        extra={"count": len(skipped_trashed), "datasets": [uri for uri, _ in skipped_trashed][:20]},
    )
    return (
        [uri for uri in uris if base_refs.normalise(uri) not in trashed_by_path],
        [DatasetResult(uri=uri, trashed=reason) for uri, reason in skipped_trashed],
    )


def maintain_one_item(item: DatasetWorkItem, *, settings: MaintenanceSettings, options: dict[str, str]) -> DatasetResult:
    """Execute ONE work item. The worker entry point — takes nothing computed across the estate.

    The absence of a whole-estate parameter here is load-bearing rather than incidental, and
    ``test_a_work_item_is_self_contained.py`` pins the signature: reintroducing one would put the
    queue in front of work that cannot actually be distributed.

    ``protected_by`` is rehydrated into the single-root ``BaseRefs`` ``compact_one`` expects.
    ``is_protected`` matches by containment, so reconstructing from the MATCHED root returns that same
    root for this uri — the verdict is preserved exactly, not approximated.

    It is NORMALISED on the way in, and that is not defensive tidying. ``is_protected`` normalises the
    location it is asked about but compares against ``protected`` verbatim, so a root that reaches this
    unnormalised — a hand-built item, or a producer that stores the URI it read rather than the verdict
    it was given — matches nothing, and base_refs names that outcome precisely: "the failure mode that
    looks exactly like having no guard at all". A work item crosses a queue, so it will eventually be
    built by something other than :func:`plan_sweep`.
    """
    protected = base_refs.BaseRefs(protected={base_refs.normalise(item.protected_by)} if item.protected_by else set())
    return _maintain_one(item.uri, item.plan, settings=settings, options=options, protected=protected)


def _resolve_plan(
    uri: str,
    *,
    policy_records: list[dict[str, Any]],
    settings: MaintenanceSettings,
    options: dict[str, str],
    now: datetime,
    older_than: timedelta,
) -> DatasetPlan:
    """Resolve this dataset's tier default, then its #50/#84 policy record, into one plan.

    #61 per-TIER default, before any policy is read. One row count cannot serve a ~1.8 MB bronze
    page-image row and a ~2 KB gold row: the same number is a ~1.8 GB fragment in one and a few MB in
    the other. A #50 policy record still overrides it, so retuning a tier stays a config change. ``None``
    for a URI whose tier cannot be read — deferring to Lance beats inventing a number that is then
    applied silently forever.

    ``scan_batch_size`` starts at the SETTINGS default (#93) rather than ``None``, so an estate with no
    policy anywhere is still bounded; a policy that names the field overrides it.

    #60 ``index_columns`` — the columns this target's queries depend on being indexed. Reporting only,
    and unreachable until a policy could name them: ``compact_one`` has accepted this argument all along
    and no caller ever supplied one, so the dropped-index check was dead code.

    A policy that cannot be resolved or parsed falls back to the GLOBAL defaults, including the settings
    batch size — not ``None``: a malformed policy must not be the one path that hands compaction Lance's
    unbounded 8192-row read (#93). "We could not read the tuning" is the worst moment to become unbounded.
    """
    plan = DatasetPlan(older_than=older_than, target_rows_per_fragment=target_rows_for(uri), scan_batch_size=settings.scan_batch_size)
    # ONE guard over the whole resolution, exactly as the inlined version had it: resolving the record,
    # asking the cadence and parsing the fields are all "reading the tuning", and any of them failing
    # means this dataset is maintained on the GLOBAL defaults rather than not maintained at all.
    try:
        policy = maintenance_policies.resolve_policy(policy_records, uri, logical_id=table_id_from_uri(uri), delimiter=settings.delimiter)
        if policy is None:
            return plan
        if skipped := _policy_skip_reason(policy, settings=settings, options=options, now=now, uri=uri):
            return DatasetPlan(skipped=skipped, policy=policy)
        plan.policy = policy
        if policy.get("retain_versions"):
            plan.retain_versions = int(str(policy["retain_versions"]))
        if policy.get("retention_days"):
            plan.older_than = timedelta(days=int(str(policy["retention_days"])))
        elif plan.retain_versions is not None:
            # "retain_versions: N" alone means exactly keep-last-N — an age bound on top would silently
            # keep everything younger than the global default and make the policy a no-op on fresh
            # datasets. Tag-pinned versions stay exempt either way.
            plan.older_than = None
        if policy.get("target_rows_per_fragment"):
            plan.target_rows_per_fragment = int(str(policy["target_rows_per_fragment"]))
        if policy.get("scan_batch_size"):
            plan.scan_batch_size = int(str(policy["scan_batch_size"]))
        if policy.get("auto_cleanup_interval_commits"):
            plan.auto_cleanup_interval_commits = int(str(policy["auto_cleanup_interval_commits"]))
        if policy.get("index_columns"):
            declared = policy["index_columns"]
            plan.index_columns = [str(c) for c in declared] if isinstance(declared, list) else None
        # Per-STEP opt-outs from the resolved policy. Winner-takes-all, like every other field: the
        # record that matched supplies these, and a record that leaves them unset gets the default (on)
        # rather than inheriting from the record it shadowed.
        plan.cleanup_enabled = bool(policy.get("cleanup_enabled", True))
        plan.optimize_indices_enabled = bool(policy.get("optimize_indices_enabled", True))
    except Exception as exc:  # noqa: BLE001 — unreadable tuning maintains on the defaults, never unbounded
        log.warning("compaction_policy_ignored", extra={"uri": uri, "error": str(exc)})
        # Back to the SETTINGS batch size, not None: a malformed policy must not be the one path that
        # hands compaction Lance's unbounded 8192-row read (#93).
        return DatasetPlan(older_than=older_than, scan_batch_size=settings.scan_batch_size)
    return plan


def _stamp_cadence(uri: str, plan: DatasetPlan, result: DatasetResult, *, settings: MaintenanceSettings, options: dict[str, str], now: datetime) -> None:
    """Record this dataset's ``last_maintained_at`` — only after a pass that could actually DO something.

    ``refused is None`` is load-bearing and was missing. A refusal carries ``error=None`` by construction
    (``optimize.py`` returns ``DatasetResult(uri=…, refused=…)`` with no error), so a dataset the sweep
    can never maintain was stamped as freshly maintained — and for the whole ``compact_interval_hours``
    window it then reported as a transient ``policy_interval`` skip rather than a standing refusal. A
    permanent condition wearing a temporary label, on the one surface an operator would use to notice it.

    Deliberately NOT extended to policy skips: those are the cadence working as intended.
    """
    policy = plan.policy
    # `plan.skipped` FIRST: a `policy_interval` skip carries the very record whose cadence produced it,
    # and re-stamping there would push the next maintenance out by another full interval on every tick —
    # a dataset frozen forever by the mechanism that exists to pace it. In the inlined version this was
    # implicit (the skip `continue`d past the stamp); extracted, it has to be said.
    if plan.skipped is not None or policy is None or not policy.get("compact_interval_hours") or result.error is not None or result.refused is not None:
        return
    try:
        maintenance_policies.write_state(settings.resolved_policy_root, options, policy, uri, now.isoformat())
    except Exception as exc:  # noqa: BLE001 — a lost stamp re-paces one dataset; it must not fail the tick
        log.warning("compaction_policy_stamp_failed", extra={"policy": policy.get("id"), "uri": uri, "error": str(exc)})


def _maintain_one(
    uri: str,
    plan: DatasetPlan,
    *,
    settings: MaintenanceSettings,
    options: dict[str, str],
    protected: base_refs.BaseRefs,
) -> DatasetResult:
    """One dataset's compact + index-optimize + GC pass, inside its own span.

    Each compact+GC is blocking Lance/S3 work invisible to auto-instrumentation — a per-dataset INTERNAL
    span lets a slow or failing compaction be localized in the trace instead of hiding in the cron
    handler. ``compact_one`` never raises (it captures the per-dataset error), so a failure and a refusal
    are both reflected on the span explicitly; otherwise a failed dataset looks identical to a clean one
    in the trace, and a refusal (#64) would be invisible there entirely.
    """
    with tracer.start_as_current_span("compaction.compact") as span:
        span.set_attribute("lance.maintenance.dataset_uri", uri)
        if plan.skipped:
            span.set_attribute("lance.maintenance.policy_skipped", plan.skipped)
            return DatasetResult(uri=uri, skipped=plan.skipped)
        result = compact_one(
            uri,
            options,
            plan.older_than,
            retain_versions=plan.retain_versions,
            target_rows_per_fragment=plan.target_rows_per_fragment,
            cleanup_enabled=plan.cleanup_enabled,
            optimize_indices_enabled=plan.optimize_indices_enabled,
            scan_batch_size=plan.scan_batch_size,
            compact_threads=settings.compact_threads,
            protected=protected,
            auto_cleanup_interval_commits=plan.auto_cleanup_interval_commits,
            index_columns=plan.index_columns,
        )
        if result.refused is not None:
            span.set_attribute("lance.maintenance.refused", result.refused)
        if result.error is not None:
            span.set_status(StatusCode.ERROR, result.error)
            if result.error_type:  # error.type: stable class name so error spans aggregate
                span.set_attribute("error.type", result.error_type)
        return result


def execute_unit(item: DatasetWorkItem, *, settings: MaintenanceSettings, options: dict[str, str], now: datetime) -> DatasetResult:
    """Do ONE unit and record what it cost — the whole of a worker's job, and of a serial iteration's.

    Both lanes call this so they cannot drift: a queued unit and a serially-executed one must maintain
    the dataset, stamp its cadence and move the same counters, or "execution moved onto a queue" would
    quietly also mean "and changed what maintenance does".

    The counters here are the PER-DATASET ones, and they are all monotonic `.add()`, so N unit
    recordings total exactly what one aggregate recording of N results did. ``record_run`` is
    deliberately NOT among them: it is the tick's completion half of the ``record_run_started`` pair, and
    firing it per unit would report N completed passes for one tick and destroy the lost-pass count that
    pair exists to give. The zero baseline those series need is emitted per tick by :func:`plan_sweep`,
    for the same reason.

    On ``record_refused`` the zero carries the most weight of any counter in this service: ``SUPPORTED``
    is a whitelist, so a rising refusal count after a pylance upgrade is the ONLY signal that maintenance
    quietly stopped covering the estate. The FAILURE series is keyed by the STABLE error class, never the
    message, which carries URIs and would make the series unbounded.
    """
    record_dataset_swept()
    result = maintain_one_item(item, settings=settings, options=options)
    _stamp_cadence(item.uri, item.plan, result, settings=settings, options=options, now=now)
    record_reclaimed(
        fragments_removed=result.fragments_removed,
        versions_removed=result.old_versions_removed,
        indices_optimized=result.indices_optimized,
    )
    record_refused(1 if result.refused else 0)
    if result.error is not None:
        record_failed({result.error_type or "Unknown": 1})
    return result


def plan_sweep(settings: MaintenanceSettings) -> tuple[list[DatasetWorkItem], list[DatasetResult]]:
    """Decide what this tick should do, WITHOUT doing any of it. Returns ``(work, already-decided)``.

    Every phase here is a metadata read — registries, a bucket listing, one manifest open per dataset —
    and none of them rewrites a byte or mints a version. That is what makes this half safe to keep in a
    request handler while the other half moves to a worker, and it is the same split the catalog's
    ``/compaction_plan`` door draws.

    The phases, each its own function above and each ordered for a reason that function states: read the
    protective registries (:func:`_load_policies`, :func:`_trash_exclusions` — either one unreadable
    ABORTS the tick), enumerate the buckets (:func:`_buckets_to_sweep`), discover (:func:`_discover_all`),
    take the whole-estate base-reference pre-pass (:func:`_protected_roots`), then remove what the trash
    freezes (:func:`_exclude_trashed`).

    The pre-pass is the one thing that cannot be per-dataset, and it is REDUCED here rather than carried:
    ``compact_one`` asks it exactly one question per dataset, so each work item leaves with that
    question's answer and the whole-estate value stays behind.

    The second return is the datasets already decided without work — the trash exclusions — which are
    results, not units, and must not be enqueued.

    ``record_run_started`` fires here, BEFORE discovery, for the reason it always did: a pass killed at
    dataset 400 of 900 was observationally identical to a tick that never arrived, and started-minus-
    completed is the lost-pass count.
    """
    record_run_started()
    # THE ZERO BASELINE, emitted per TICK because per-unit recording alone cannot: an estate with no
    # datasets runs no unit, and a counter nothing ever adds to is a series that does not exist — a
    # dashboard or alert on `rate(compaction_*_total[5m])` reads "no data" rather than "nothing to
    # reclaim". Adding zero is a no-op for the totals and creates the series from the first tick, which
    # is the rule `record_reclaimed` states and the sharper one `record_refused` states: a whitelist
    # that silently starts refusing the whole estate must be visible from the first tick after the
    # upgrade that caused it. Each unit then adds its own.
    record_reclaimed(fragments_removed=0, versions_removed=0, indices_optimized=0)
    record_refused(0)
    options = settings.storage_options()
    older_than = timedelta(days=settings.older_than_days)
    policy_records = _load_policies(settings, options)
    trashed_by_path = _trash_exclusions(settings, options)
    uris = _discover_all(_s3fs(settings), _buckets_to_sweep(settings, options))
    protected = _protected_roots(uris, options)
    uris, decided = _exclude_trashed(uris, trashed_by_path)
    now = datetime.now(UTC)
    # The discovery listing order is deterministic across ticks, so a pass that consistently dies at
    # dataset N never maintained anything after N — silently, forever. Shuffling rotates which datasets
    # sit behind a recurring failure point. It is a mitigation for the absence of a per-dataset failure
    # boundary, and it stops being needed once these units are executed independently rather than in
    # one serial pass; until then it still holds, and per-dataset pacing lives in the policy stamps,
    # which do not care about order.
    random.shuffle(uris)
    items = [
        DatasetWorkItem(
            uri=uri,
            plan=_resolve_plan(uri, policy_records=policy_records, settings=settings, options=options, now=now, older_than=older_than),
            protected_by=protected.is_protected(uri),
        )
        for uri in uris
    ]
    return items, decided


def plan_one(uri: str, settings: MaintenanceSettings) -> DatasetWorkItem | None:
    """Plan ONE dataset, named by a write event — the event lane's half of :func:`plan_sweep`.

    Returns the unit to enqueue, or ``None`` when this dataset must not be maintained right now. It
    applies the same three refusals the sweep applies, because an event lane that skipped any of them
    would be a second, weaker door onto the same bytes:

    * **the trash record** — a recoverably-dropped dataset is frozen until undrop or purge, and
      rewriting it destroys time-travel someone can still restore. This is the refusal the estate has
      already paid for by hand-overriding it once;
    * **the policy** — `compact_enabled` and the cadence stamp, resolved winner-takes-all;
    * **the base references** — whether another manifest resolves through these bytes.

    The protection check is :func:`sibling_base_refs`, not the sweep's whole-estate pre-pass, and that
    is a deliberate narrowing with the bound stated at that function: a referrer in another warehouse
    is invisible here exactly as a referrer outside the configured buckets is invisible to the sweep.
    It is computed PER CALL, so a clone created a minute ago protects its source on the next event.

    An unreadable trash index refuses rather than proceeds. The sweep aborts the whole tick on one;
    here the blast radius is one dataset, so the same fail-toward-not-deleting choice costs a single
    skipped event that the hourly backstop will re-plan.
    """
    options = settings.storage_options()
    try:
        trashed_by_path = _trash_exclusions(settings, options)
    except Exception as exc:  # noqa: BLE001 — an unreadable trash index must refuse, never proceed
        log.warning("arrival_trash_index_unreadable", extra={"uri": uri, "error": str(exc)})
        return None
    if base_refs.normalise(uri) in trashed_by_path:
        return None

    plan = _resolve_plan(
        uri,
        policy_records=_load_policies(settings, options),
        settings=settings,
        options=options,
        now=datetime.now(UTC),
        older_than=timedelta(days=settings.older_than_days),
    )
    if plan.skipped:
        return None
    return DatasetWorkItem(uri=uri, plan=plan, protected_by=base_refs.sibling_base_refs(uri, options).is_protected(uri))


def run_sweep(settings: MaintenanceSettings) -> list[DatasetResult]:
    """Plan the tick, then execute every unit it produced, in one process. Returns what was reclaimed.

    :func:`plan_sweep` decides (registries, discovery, the base-reference pre-pass, per-dataset policy)
    and :func:`maintain_one_item` does one dataset's work; this is the two composed. It is the SERIAL
    execution of a set of units that are individually independent — which is what makes moving the
    execution half onto a queue a wiring change rather than a redesign.

    #50/#84 policies: a per-table/namespace/project record from the catalog's ``_policies/`` registry can
    disable a dataset's maintenance, re-pace it (cadence stamp per dataset), or override its old-version
    retention (a table record beats a namespace record beats a project record); everything else keeps the
    global defaults. The resolution happens in the planner; each unit arrives carrying its verdict.

    MULTI-BUCKET (audit 2026-07-14). This used to sweep exactly ONE bucket, so every #3-A per-warehouse
    bucket and #3-B multi-base data bucket was invisible to GC — their tables accumulated superseded
    manifest versions and small fragments FOREVER. A storage leak created by the very features that
    introduce new buckets.
    """
    items, results = plan_sweep(settings)
    options = settings.storage_options()
    now = datetime.now(UTC)
    results.extend(execute_unit(item, settings=settings, options=options, now=now) for item in items)
    # The completion half of the `record_run_started` pair the planner opened. Started minus completed
    # is the lost-pass count, so this fires once per tick and only after every unit has been executed.
    record_run()
    return results


def _did_material_work(result: DatasetResult) -> bool:
    """Did the pass actually reclaim anything (fragments merged or old versions GC'd)?

    We record a maintenance event only when something material happened — a cron that re-sweeps every
    dataset on each tick would otherwise flood the lineage graph with no-op compaction runs.
    """
    return bool(result.fragments_removed or result.old_versions_removed)


async def _emit_complete(emitter: MaintenanceEmitter, *, table_id: str, namespace: str) -> None:
    """The COMPLETE publish, as a coroutine function so the attribute lookup happens INSIDE the guard.

    Not `partial(emitter.emit_maintenance, …)`: that resolves the method eagerly, at the point the job
    list is built, which is outside :func:`_publish_all`'s try — so an emitter that predates one half of
    the protocol raised an `AttributeError` straight into the cron handler, which is the exact failure
    the guardrail exists to make impossible.
    """
    await emitter.emit_maintenance(table_id=table_id, namespace=namespace)


async def _emit_failed(emitter: MaintenanceEmitter, *, table_id: str, namespace: str, error: str) -> None:
    """The FAIL publish — same late-binding rule as :func:`_emit_complete`."""
    await emitter.emit_maintenance_failed(table_id=table_id, namespace=namespace, error=error)


async def _publish_all(kind: str, jobs: list[tuple[str, Callable[[], Awaitable[None]]]]) -> None:
    """Run every publish in ``jobs`` concurrently, at most ``_MAX_CONCURRENT_EMITS`` in flight.

    ONE helper for both lanes, because the guarantee has to be the same for both and it was not: the
    FAIL lane was raise-proof "by construction, not by convention" while the COMPLETE lane awaited each
    publish bare inside the discovery loop, so one mis-wired publish (an ``AttributeError`` building the
    coro, a raise inside it) aborted the emit phase — taking every COMPLETE emit queued behind it AND
    the entire FAIL batch with it, and answering the cron tick 500. Each job is ``(table_id, factory)``;
    the factory is called inside the limiter so nothing is constructed until it may run.
    """
    if not jobs:
        return
    limiter = asyncio.Semaphore(_MAX_CONCURRENT_EMITS)

    async def _one(factory: Callable[[], Awaitable[None]]) -> None:
        async with limiter:
            await factory()

    try:
        outcomes = await asyncio.gather(*(_one(factory) for _table_id, factory in jobs), return_exceptions=True)
    except Exception as exc:
        # The gather itself failing (rather than one publish inside it) is not something a publish
        # guardrail should ever surface to the cron handler either.
        log.warning(f"maintenance_{kind}_emit_error", extra={"error": str(exc)})
        return
    for (table_id, _factory), outcome in zip(jobs, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            log.warning(f"maintenance_{kind}_emit_error", extra={"table": table_id, "error": str(outcome)})


async def emit_sweep_lineage(emitter: MaintenanceEmitter, results: list[DatasetResult], *, delimiter: str) -> None:
    """Emit a best-effort maintenance event per swept dataset: COMPLETE for material work, FAIL for a
    terminal per-dataset failure (#7b + the §4 failure-visibility item).

    Selection:

    * ``maintain:``-errored → a **FAIL** event (compact/GC escaped Lance's own auto-retry — terminal for
      this tick; before this, a persistently failing dataset surfaced only in OTel spans + a cron response
      body nobody reads).
    * ``open:``-errored → **no event** (unreadable / declared-only dir — transient non-dataset noise).
    * **REFUSED** (#64, an unsupported manifest feature flag) → **no event**, by construction: a
      refusal carries ``error=None`` and did no material work, so it falls through both branches
      below. Deliberate — nothing FAILED, we declined before touching a byte, and a FAIL event would
      claim a maintenance run went wrong. Its visibility is the WARNING log, the ``lance.refused``
      span attribute, the ``compaction.datasets.refused`` counter and ``summarize``'s own line.
    * no error + material work → the **COMPLETE** event (unchanged); no-op ticks skipped.
    * URI not the catalog's ``<uuid>_<table_id>`` layout → skipped either way (no id to key on). This is
      the DOCUMENTED blind spot for the medallion-nested datasets (``s3://<bucket>/medallion/<ns>`` has no
      catalog id to reconstruct — a URI→id map is out of proportion here).

    The parent namespace is derived via :func:`service_kit.governed.fga.parent_namespace_id` so events land on the SAME
    ``(:Dataset)`` the catalog created. BOTH lanes are then published through :func:`_publish_all`:
    gathered, bounded at ``_MAX_CONCURRENT_EMITS`` in flight, and raise-proof per publish. Only the FAIL
    lane is additionally CAPPED at ``_MAX_FAIL_EMITS_PER_TICK`` — see that constant for why capping the
    COMPLETE lane would erase a maintenance run rather than delay it. Nothing here raises into the sweep.
    """
    # One pass derives (table_id, namespace) for BOTH branches — the cap below then counts actual emits
    # (an unparseable maintain:-errored URI must not consume a cap slot while emitting nothing).
    complete: list[tuple[str, str]] = []  # (table_id, namespace)
    failed: list[tuple[str, str, str]] = []  # (table_id, namespace, error)
    for result in results:
        # The DECLARED name wins over the URI derivation. Without this the read half of T6 was dead
        # code: `declared_table_id` existed and nothing called it, so the cascade's own tiers still
        # emitted nothing — a URI like `medallion/bronze` names two different objects and cannot be
        # resolved, which is why the producer stamps it instead.
        table_id = result.declared_table_id or table_id_from_uri(result.uri)
        if table_id is None:
            continue
        namespace = fga.parent_namespace_id(table_id, delimiter=delimiter) or ""
        if result.error is not None:
            if result.error.startswith("maintain:"):
                failed.append((table_id, namespace, result.error))
            continue
        if not _did_material_work(result):
            continue
        complete.append((table_id, namespace))

    if len(failed) > _MAX_FAIL_EMITS_PER_TICK:
        log.warning(
            "maintenance_fail_emits_capped",
            extra={"failing": len(failed), "emitted": _MAX_FAIL_EMITS_PER_TICK},
        )
        random.shuffle(failed)  # fairness under a mass incident — see the cap constant's comment
        failed = failed[:_MAX_FAIL_EMITS_PER_TICK]
    await _publish_all("complete", [(t, partial(_emit_complete, emitter, table_id=t, namespace=ns)) for t, ns in complete])
    await _publish_all("fail", [(t, partial(_emit_failed, emitter, table_id=t, namespace=ns, error=err)) for t, ns, err in failed])


def summarize(results: list[DatasetResult]) -> dict[str, Any]:
    """Aggregate one sweep's per-dataset results into the cron response. Failures keep their MESSAGE
    (not just the URI) — a cron sweep has no human watching, so the *why* is the only debugging signal."""
    return {
        "datasets": len(results),
        "skipped": sum(1 for r in results if r.skipped),
        # #64 — a REFUSAL is its own line, never folded into `errors` or `skipped`. It is neither: a
        # skip is "not this tick" and an error is "something failed", while a refusal is permanent and
        # nothing failed. It also has to be LOUD, because `SUPPORTED` is a whitelist: a pylance
        # upgrade that adds a legitimate flag would otherwise stop maintaining every dataset that
        # sets it while this summary still reported a clean sweep.
        "refused": sum(1 for r in results if r.refused),
        "refusals": {r.uri: r.refused for r in results if r.refused},
        # F6(d) — its own line for the same reason `refused` has one. These datasets were discovered,
        # were maintainable, and were deliberately left alone because they are in the trash: dropped
        # with a grace window and therefore frozen until undrop or purge. Naming them (not just
        # counting) is what makes a stuck record — one whose deadline has passed and which the purge
        # keeps refusing — visible as a PERMANENT exclusion rather than a transient one.
        "trashed": sum(1 for r in results if r.trashed),
        "trashed_datasets": {r.uri: r.trashed for r in results if r.trashed},
        "fragments_removed": sum(r.fragments_removed for r in results),
        "indices_optimized": sum(r.indices_optimized for r in results),
        "versions_removed": sum(r.old_versions_removed for r in results),
        # `bytes_removed` was WRITE-ONLY until 2026-08-15: `compact_one` assigned it from the Lance
        # cleanup stats and nothing ever read it, so every tick measured how much it had reclaimed and
        # discarded the number while reporting three counts that do not answer the question. "How much
        # did we get back" is the one thing a reclaimer exists to tell you.
        "bytes_removed": sum(r.bytes_removed for r in results),
        # #58 — which datasets hand version reclamation to THEMSELVES instead of being swept. Without
        # it "reclaimed nothing" and "the writer reclaims this one" read identically on a zero.
        "auto_cleanup_configured": sum(1 for r in results if r.auto_cleanup_configured),
        # #60 — what `optimize_indices()` could NOT put right: rows left unindexed, delta
        # proliferation, params drift, a dropped index. This was computed on every tick — a
        # `describe_indices` + `index_stats` pass per index per dataset, the call that PANICS and needed
        # two separate BaseException guards to contain — and then discarded, surfacing only as a count
        # in one warning. The estate paid for the diagnosis every 120s and never received it.
        "index_findings": {r.uri: r.index_findings for r in results if r.index_findings},
        "errors": {r.uri: r.error for r in results if r.error},
    }
