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
from datetime import UTC, datetime, timedelta
from typing import Any

import pyarrow.fs as pafs
from opentelemetry import trace
from opentelemetry.trace import StatusCode

from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import MaintenanceEmitter, table_id_from_uri
from maintenance.core.metrics import record_dataset_swept, record_reclaimed, record_refused, record_run, record_run_started
from maintenance.services import base_refs, purge
from maintenance.services.optimize import DatasetResult, compact_one, discover_datasets
from service_kit.governed import fga
from service_kit.lakehouse import maintenance_policies, warehouse_records
from service_kit.lakehouse.objectfs import s3_filesystem


log = logging.getLogger(__name__)

#: Per-tick cap on FAIL-event publishes. Each publish is already bounded by the emitter's 5s timeout and
#: the batch is gathered concurrently, but an unbounded fan-out over a bucket where EVERYTHING is failing
#: could still push the cron handler past the 30s Dapr ack window — cap it, and LOG what was dropped
#: (a silent cap would read as "covered everything"). The over-cap set is SHUFFLED before the cut: the
#: discovery listing order is deterministic, so a fixed head-slice would re-drop the SAME datasets every
#: tick and their FAIL would never emit — shuffling gets every failing dataset through within a few ticks,
#: converging on its deterministic run id (review 2026-07-10).
_MAX_FAIL_EMITS_PER_TICK = 25

# Each compact+GC is blocking Lance/S3 work invisible to auto-instrumentation — a per-dataset INTERNAL
# span lets a slow or failing compaction be localized in the trace instead of hiding in the cron handler.
tracer = trace.get_tracer(__name__)


def _s3fs(settings: MaintenanceSettings) -> pafs.S3FileSystem:
    """A pyarrow S3 filesystem over the RustFS endpoint — used only to LIST the bucket."""
    return s3_filesystem(settings.storage_options())


def _policy_skip_reason(
    policy: dict[str, object],
    settings: MaintenanceSettings,
    options: dict[str, str],
    now: datetime,
    uri: str,
) -> str | None:
    """Why the policy says to skip this dataset this tick, or ``None`` to maintain it.

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


def run_sweep(settings: MaintenanceSettings) -> list[DatasetResult]:
    """Discover every dataset in EVERY swept bucket and compact + GC each; record what was reclaimed.

    #50/#84 policies: a per-table/namespace/project record from the catalog's ``_policies/`` registry can
    disable a dataset's maintenance, re-pace it (cadence stamp per dataset), or override its old-version
    retention (a table record beats a namespace record beats a project record); everything else keeps the
    global defaults.

    MULTI-BUCKET (audit 2026-07-14). This used to sweep exactly ONE bucket, so every #3-A per-warehouse
    bucket and #3-B multi-base data bucket was invisible to GC — their tables accumulated superseded
    manifest versions and small fragments FOREVER. A storage leak created by the very features that
    introduce new buckets. A bucket that does not exist (or is unreadable) is skipped, not fatal: one
    missing tenant bucket must not stop the sweep for everyone else.
    """
    # BEFORE discovery, not after the loop like `record_run()`: a pass killed at dataset 400 of 900
    # was observationally identical to a tick that never arrived (open_dapr.md §2.20). started minus
    # completed is the lost-pass count.
    record_run_started()
    older_than = timedelta(days=settings.older_than_days)
    options = settings.storage_options()
    fs = _s3fs(settings)
    # #50 maintenance policies — loaded once per tick; a policy-less dataset keeps the global defaults.
    # A registry we cannot read at all ABORTS the tick (fail toward not deleting, audit 2026-07-16):
    # policies are the protective surface (retention extensions, opt-outs), so sweeping without them
    # would GC version history an owner explicitly kept. The next cron fire retries; one bad record
    # inside a readable registry is skipped-with-warning by list_policies, not fatal.
    try:
        policy_records = maintenance_policies.list_policies(settings.resolved_policy_root, options)
    except Exception:
        log.error("compaction_policies_unreadable_tick_aborted")
        raise
    # The count distinguishes "no policies set" from "policies invisible" (e.g. the catalog's control
    # root moved without MAINTENANCE_POLICY_ROOT following — a wrong root lists cleanly as empty).
    log.info("compaction_policies_loaded", extra={"policies": len(policy_records)})
    # #81 EVERY warehouse the registry knows, not just the configured list. `sweep_buckets` is the
    # primary bucket plus a static env var — but a per-warehouse bucket is created by an API CALL at
    # runtime, so every tenant provisioned since the last config edit was invisible to maintenance and
    # its tables accumulated superseded versions and small fragments forever. A storage leak created
    # by the very feature that introduces new buckets, and silent: the sweep reported success over the
    # buckets it did know. An unreadable registry is NOT fatal here (unlike the policy registry, whose
    # absence would mean sweeping without protective retention overrides) — it degrades to the
    # configured list, which is exactly the old behaviour, and says so.
    buckets = list(settings.sweep_buckets)
    try:
        registry = warehouse_records.list_warehouse_records(settings.resolved_control_root, options)
        discovered = [b for b in warehouse_records.maintainable_buckets(registry) if b not in buckets]
        buckets.extend(discovered)
        log.info("sweep_registry_buckets", extra={"configured": len(settings.sweep_buckets), "from_registry": len(discovered)})
    except Exception as exc:  # noqa: BLE001 — a missing registry must not stop maintaining what we know
        log.warning("sweep_registry_unreadable", extra={"error": str(exc)})
    uris: list[str] = []
    truncated: list[str] = []
    for bucket in buckets:
        try:
            found = discover_datasets(fs, bucket)
        except Exception as exc:
            log.warning("compaction_bucket_skipped", extra={"bucket": bucket, "error": str(exc)})
            continue
        log.info(
            "compaction_bucket_discovered",
            extra={"bucket": bucket, "datasets": len(found.uris), "truncated": len(found.truncated)},
        )
        uris.extend(found.uris)
        # A prefix the walk could not reach is UNMAINTAINED, and saying so is the whole point: a
        # silent depth bound made "we maintained everything" and "we maintained what we could see"
        # the same summary line.
        truncated.extend(found.truncated)
    # #75 trash expiry — REPORT ONLY **in the sweep**, permanently. #79's purge lives on the RECONCILE
    # tick instead, because its gate is that tick's drift report: a reclaimer earns its delete permission
    # by first proving the report runs clean, and the sweep does not produce that report. So the sweep
    # keeps naming which recoverable drops are past their deadline and keeps deleting NOTHING.
    try:
        # The CONTROL root, not the policy root: the catalog writes `_trash/` under its registry root
        # (LANCE_CONTROL_ROOT). These default to the same bucket, so the mismatch was invisible — and
        # would have stayed invisible, because list_all uses allow_not_found=True and this whole block
        # is except-wrapped: a wrong root reports zero due records forever rather than failing.
        #
        # Selection goes through `purge.due_records` so ONE rule decides what "expired" means: the set
        # this logs and the set the purge deletes cannot be two different answers.
        due = purge.due_records(settings.resolved_control_root, options)
        if due:
            log.info(
                "trash_expiry_due_report_only",
                extra={"count": len(due), "ids": [str(r.get("id")) for r in due][:20]},
            )
    except Exception as exc:  # noqa: BLE001 — the trash report must never abort a maintenance tick
        log.warning("trash_expiry_report_failed", extra={"error": str(exc)})
    # #128d/#114 THE PRE-PASS — over every discovered dataset in EVERY bucket, before a single one is
    # compacted. It has to be whole-estate and it has to be first: a shallow clone in bucket B is the
    # only thing that knows bucket A's dataset must not be touched, so a per-bucket or per-dataset
    # check cannot see it. The SOURCE carries no feature flag and no `base_paths` of its own
    # (measured), which is why the flag gate inside `compact_one` misses this entirely.
    #
    # Cost is one manifest read per dataset and no data file is opened — the same order as discovery
    # itself, paid once per tick.
    protected = base_refs.protected_roots(uris, options)
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
    results: list[DatasetResult] = []
    now = datetime.now(UTC)
    # The FAIL-emit cap's own argument (top of this module), applied to the sweep it lives in: the
    # discovery listing order is deterministic across ticks, so a pass that consistently dies at
    # dataset N never maintained anything after N — silently, forever (open_dapr.md §2.19). Shuffling
    # rotates which datasets sit behind a recurring failure point; per-dataset pacing stays with the
    # policy stamps, which don't care about order.
    random.shuffle(uris)
    for uri in uris:
        record_dataset_swept()
        with tracer.start_as_current_span("compaction.compact") as span:
            span.set_attribute("lance.dataset_uri", uri)
            effective_older_than: timedelta | None = older_than
            retain_versions: int | None = None
            target_rows: int | None = None  # #76 compaction target-size from the policy
            # The sweep's READ batch — rows are not a unit of memory. Starts at the SETTINGS default
            # (#93) rather than None, so an estate with no policy anywhere is still bounded; a policy
            # that names the field overrides it below, which is the point of per-tier tuning.
            batch_size: int = settings.scan_batch_size
            auto_cleanup_interval: int | None = None  # #58 — set means the DATASET owns version cleanup
            policy: dict[str, Any] | None
            try:
                policy = maintenance_policies.resolve_policy(policy_records, uri, logical_id=table_id_from_uri(uri), delimiter=settings.delimiter)
                skipped = _policy_skip_reason(policy, settings, options, now, uri) if policy else None
                if skipped:
                    span.set_attribute("lance.policy_skipped", skipped)
                    results.append(DatasetResult(uri=uri, skipped=skipped))
                    continue
                if policy is not None:
                    if policy.get("retain_versions"):
                        retain_versions = int(str(policy["retain_versions"]))
                    if policy.get("retention_days"):
                        effective_older_than = timedelta(days=int(str(policy["retention_days"])))
                    elif retain_versions is not None:
                        # "retain_versions: N" alone means exactly keep-last-N — an age bound on top would
                        # silently keep everything younger than the global default and make the policy a
                        # no-op on fresh datasets. Tag-pinned versions stay exempt either way.
                        effective_older_than = None
                    if policy.get("target_rows_per_fragment"):
                        target_rows = int(str(policy["target_rows_per_fragment"]))
                    if policy.get("scan_batch_size"):
                        batch_size = int(str(policy["scan_batch_size"]))
                    if policy.get("auto_cleanup_interval_commits"):
                        auto_cleanup_interval = int(str(policy["auto_cleanup_interval_commits"]))
            except Exception as exc:
                log.warning("compaction_policy_ignored", extra={"uri": uri, "error": str(exc)})
                effective_older_than, retain_versions, target_rows, policy = older_than, None, None, None
                # Back to the SETTINGS default, not None: a malformed policy must not be the one path
                # that hands compaction Lance's unbounded 8192-row read (#93). "We could not read the
                # tuning" is the worst moment to become unbounded.
                batch_size, auto_cleanup_interval = settings.scan_batch_size, None
            result = compact_one(
                uri,
                options,
                effective_older_than,
                retain_versions=retain_versions,
                target_rows_per_fragment=target_rows,
                # Per-STEP opt-outs from the resolved policy. Winner-takes-all, like every other
                # field: the record that matched supplies these, and a record that leaves them unset
                # gets the default (on) rather than inheriting from the record it shadowed.
                cleanup_enabled=bool((policy or {}).get("cleanup_enabled", True)),
                optimize_indices_enabled=bool((policy or {}).get("optimize_indices_enabled", True)),
                scan_batch_size=batch_size,
                compact_threads=settings.compact_threads,
                protected=protected,
                auto_cleanup_interval_commits=auto_cleanup_interval,
            )
            if policy is not None and policy.get("compact_interval_hours") and result.error is None:
                # Stamp cadence state only after a successful pass, so a failed tick retries next tick.
                try:
                    maintenance_policies.write_state(settings.resolved_policy_root, options, policy, uri, now.isoformat())
                except Exception as exc:
                    log.warning(
                        "compaction_policy_stamp_failed",
                        extra={"policy": policy.get("id"), "uri": uri, "error": str(exc)},
                    )
            results.append(result)
            # #64 — a refusal is not an error, so it would otherwise be invisible in the trace too.
            if result.refused is not None:
                span.set_attribute("lance.refused", result.refused)
            # compact_one never raises (it captures the per-dataset error), so reflect a failure on the
            # span explicitly — else a failed dataset looks identical to a clean one in the trace.
            if result.error is not None:
                span.set_status(StatusCode.ERROR, result.error)
                if result.error_type:  # error.type: stable class name so error spans aggregate
                    span.set_attribute("error.type", result.error_type)
    record_run()
    record_reclaimed(
        fragments_removed=sum(r.fragments_removed for r in results),
        versions_removed=sum(r.old_versions_removed for r in results),
        indices_optimized=sum(r.indices_optimized for r in results),
    )
    # Always recorded, including 0 — the `record_reclaimed` rule. Here the zero carries the most
    # weight of any counter in this service: `SUPPORTED` is a whitelist, so a rising refusal count
    # after a pylance upgrade is the ONLY signal that maintenance quietly stopped covering the estate.
    record_refused(sum(1 for r in results if r.refused))
    return results


def _did_material_work(result: DatasetResult) -> bool:
    """Did the pass actually reclaim anything (fragments merged or old versions GC'd)?

    We record a maintenance event only when something material happened — a cron that re-sweeps every
    dataset on each tick would otherwise flood the lineage graph with no-op compaction runs.
    """
    return bool(result.fragments_removed or result.old_versions_removed)


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
    ``(:Dataset)`` the catalog created. COMPLETE emits stay awaited inline (unchanged semantics); the FAIL
    batch is gathered CONCURRENTLY (each publish already bounded by the emitter's 5s timeout) and capped
    at ``_MAX_FAIL_EMITS_PER_TICK`` so a bucket of failing datasets can't push the cron handler past the
    30s Dapr ack window. Every emit is best-effort internally, so nothing here raises into the sweep.
    """
    # One pass derives (table_id, namespace) for BOTH branches — the cap below then counts actual emits
    # (an unparseable maintain:-errored URI must not consume a cap slot while emitting nothing).
    failed: list[tuple[str, str, str]] = []  # (table_id, namespace, error)
    for result in results:
        table_id = table_id_from_uri(result.uri)
        if table_id is None:
            continue
        namespace = fga.parent_namespace_id(table_id, delimiter=delimiter) or ""
        if result.error is not None:
            if result.error.startswith("maintain:"):
                failed.append((table_id, namespace, result.error))
            continue
        if not _did_material_work(result):
            continue
        await emitter.emit_maintenance(table_id=table_id, namespace=namespace)

    if len(failed) > _MAX_FAIL_EMITS_PER_TICK:
        log.warning(
            "maintenance_fail_emits_capped",
            extra={"failing": len(failed), "emitted": _MAX_FAIL_EMITS_PER_TICK},
        )
        random.shuffle(failed)  # fairness under a mass incident — see the cap constant's comment
        failed = failed[:_MAX_FAIL_EMITS_PER_TICK]
    # Raise-proof by construction, not by convention: the emitters swallow internally, but the guardrail
    # ("a publish failure must never fail the sweep") must hold even for a mis-wired emitter — an
    # AttributeError building a coro or a raise inside one is logged per dataset and never reaches the
    # cron handler.
    try:
        outcomes = await asyncio.gather(
            *(emitter.emit_maintenance_failed(table_id=t, namespace=ns, error=err) for t, ns, err in failed),
            return_exceptions=True,
        )
        for (table_id, _ns, _err), outcome in zip(failed, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                log.warning("maintenance_fail_emit_error", extra={"table": table_id, "error": str(outcome)})
    except Exception as exc:
        log.warning("maintenance_fail_emit_error", extra={"error": str(exc)})


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
        "fragments_removed": sum(r.fragments_removed for r in results),
        "indices_optimized": sum(r.indices_optimized for r in results),
        "versions_removed": sum(r.old_versions_removed for r in results),
        "errors": {r.uri: r.error for r in results if r.error},
    }
