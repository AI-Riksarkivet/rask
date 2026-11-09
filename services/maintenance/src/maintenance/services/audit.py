"""The audit and the scan (ADR 0198 §2, decision 2): each one task per firing of its schedule, run by one replica.

Both read three stores and report their disagreements, repair what the report justifies, and release every drop past
its deadline (ADR 0146) that its own trash task has not, gated on neither the report nor the purge's opt-in, since it
deletes nothing. The hourly AUDIT (:func:`run_audit`) leaves the file layer unscanned, so it opens no table, and queues
a pass for every table and a task for every trash record that holds neither a schedule nor a queued task, which is what
bounds a lost birth or a lost schedule to one firing. The daily SCAN (:func:`run_scan`, owner 2026-11-09: a scan opens
every table, about 20 store requests each) runs the orphan scan too, and then, gated on THAT report running clean,
raises listing floors and reclaims expired trash (#79). The purge rides the scan rather than a table's pass precisely
because its permission comes from the report: it consumes the report object the same run produced, never a stored one.
It is off by default (`MAINTENANCE_TRASH_PURGE_ENABLED`), so the shipped configuration deletes no trashed byte.

One task of each, once per firing whatever the replica count: each schedule is a NATS message schedule (``@hourly``, and
``0 30 0 * * *`` for the scan, by default), each on a subject of its own, and the queue holds one task per subject. Both
repair the estate from a drift report of their own, so they hold one claim (:mod:`maintenance.services.queue`): the two
never run at once, and the later waits out the other.
"""

import logging
from collections import Counter
from functools import partial
from typing import Any

from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from lakehouse import location_claims, trash
from lakehouse.control_emit import ControlEmitter
from maintenance.core.config import MaintenanceSettings
from maintenance.services.credentials import end_trash, grant_census
from maintenance.services.due import table_ids
from maintenance.services.floor import raise_listing_floors
from maintenance.services.naming import TableIds, claim_store, claimed_location
from maintenance.services.plan import buckets_to_sweep
from maintenance.services.purge import objects_held, purge_expired_trash, refusal_log_entry
from maintenance.services.queue import Task, TaskQueue
from maintenance.services.rebuild import rebuild_tuples
from maintenance.services.reconcile import CATEGORIES, ReconcileReport, reconcile
from maintenance.services.reconcile import Sources as ReconcileSources
from maintenance.services.repair import repair_drift
from maintenance.services.tombstones import Tombstone, sweep_tombstones
from maintenance.services.trash_release import release_expired_trash


log = logging.getLogger(__name__)


async def run_audit(settings: MaintenanceSettings, *, queue: TaskQueue | None) -> dict[str, Any]:
    """The hourly audit: the drift report without the orphan scan, its repairs, the release of every drop past its
    deadline (ADR 0146), and last the tasks no schedule holds.

    The report is returned AND logged as one structured record: the log lands in the OTLP stream where drift is actually
    noticed. It leaves the file layer to the daily scan (:func:`run_scan`, owner 2026-11-09), so it opens no table, and
    with it what only a scanned file layer certifies, the listing floors and the purge.
    """
    payload, _, sources = await _report_and_repair(settings.model_copy(update={"orphan_scan_enabled": False}))
    if queue is not None:
        payload["unscheduled"] = await _queue_what_holds_nothing(settings, queue, sources)
    return payload


async def run_scan(settings: MaintenanceSettings, *, control: ControlEmitter) -> dict[str, Any]:
    """The daily scan: the drift report WITH the orphan scan (`MAINTENANCE_ORPHAN_SCAN_ENABLED`), its repairs and the
    release, then what the scanned file layer certifies: the listing floors, and the #79 expired-trash purge.

    **The purge lives HERE, in the same run as the report.** Its gate is "the drift report ran clean", the orphan scan
    included (a skipped scan is a coverage gap, :func:`maintenance.services.purge.report_is_clean`), so it must consume the
    report THIS run produced: a purge reading a stored or previous report would certify a state that no longer exists.
    Reclamation is off by default (`MAINTENANCE_TRASH_PURGE_ENABLED`), so on the shipped configuration this adds one
    report key and deletes nothing.
    """
    payload, report, _ = await _report_and_repair(settings)
    names = await run_in_threadpool(_swept_table_ids, settings)
    floors = await run_in_threadpool(
        partial(raise_listing_floors, settings, orphans=report.orphan_files, names=names, storage_options=settings.storage_options())
    )
    payload["floor_raise"] = floors.model_dump(mode="json")
    # WARNING and NAMED, like the purge line below, because this writes a version to a GOVERNED
    # table. Which tables is the entire audit trail; a count answers nobody's question about it.
    if floors.raised or floors.refused or floors.error:
        log.warning(
            "floor_raise_result",
            extra={
                "dry_run": floors.dry_run,
                "raised": [{"dataset": r.dataset, "version": r.version, "orphan_files": r.orphan_files, "orphan_bytes": r.orphan_bytes} for r in floors.raised],
                "refused": [{"dataset": r.dataset, "reason": r.refused} for r in floors.refused],
                "capped": floors.capped,
                "error": floors.error,
            },
        )
    purged = await purge_expired_trash(
        settings,
        report=report,
        control=control,
        control_root=settings.resolved_control_root,
    )
    payload["trash_purge"] = purged.model_dump(mode="json")
    # Reclamation is irreversible, so a scan that destroyed something — or declined to — says so at
    # WARNING. A quiet INFO would put the one irreversible thing this service does at the same
    # volume as a scan that did nothing.
    #
    # A DRY RUN'S PLAN IS REPORTED HERE TOO, because this line is the only place the records are
    # NAMED: no task's result is stored. A preview whose
    # whole output is `would_purge` would otherwise produce no line at all — invisible in exactly
    # the deployment that turned it on in order to see it.
    if purged.purged or purged.would_purge or purged.refused or purged.capped:
        log.warning(
            "trash_purge_result",
            extra={
                "dry_run": purged.dry_run,
                "purged": [f"{p.kind}:{p.id}" for p in purged.purged],
                "would_purge": [{"id": f"{p.kind}:{p.id}", "location": p.location, "expires_at": p.expires_at} for p in purged.would_purge],
                "refused": [refusal_log_entry(r) for r in purged.refused],
                "capped": purged.capped,
                "bytes_reclaimed": sum(p.bytes_deleted for p in purged.purged),
            },
        )
    return payload


async def _report_and_repair(settings: MaintenanceSettings) -> tuple[dict[str, Any], ReconcileReport, ReconcileSources | None]:
    """One cross-store drift REPORT, the repairs it justifies and the release of every drop past its deadline; the
    payload to log, the report, for a caller whose next step it gates, and the stores it read."""
    # The stores this run read, so the tuple rebuild below writes only grants the records the
    # REPORT saw actually justify — see `reconcile`'s `into_sources`.
    read_sources: list[ReconcileSources] = []
    report = await reconcile(
        settings,
        partial(grant_census, settings),
        warehouses_enabled=settings.warehouses_enabled,
        control_root=settings.resolved_control_root,
        into_sources=read_sources,
    )
    payload = report.model_dump(mode="json")
    summary = {
        "total": report.total,
        "counts": report.counts,
        "unavailable": [u.category for u in report.unavailable],
        "skipped": [s.category for s in report.skipped],
        # SOURCE AND REASON, because this list GATES RECLAMATION and the source alone cannot be
        # acted on. `report_is_clean` blocks the #79 purge while anything is incomplete, so these
        # entries are why the estate holds unreclaimed orphans — measured 2026-09-16: three entries
        # all reading `storage:lance-catalog`, against 932 orphan files the purge could not touch.
        # Each `IncompleteScan` carries its `reason`, which names what an operator can act on.
        "incomplete": [f"{i.source}: {i.reason}" for i in report.incomplete],
        # WHICH ones, not just how many — see `_drift_names`. Without this the WARNING is a number
        # an operator cannot act on: no task's result is stored.
        "findings": _drift_names(report),
        # The DISTRIBUTION beside the identities, because for this one category a bounded sample of
        # NAMES answers neither "how bad is any of them" nor "how many are there" — see
        # `_orphans_by_dataset`. Omitted entirely when the scan found nothing, so a clean tick does
        # not carry an empty key that reads like a category nobody looked at.
        **({"orphans_by_dataset": distribution} if (distribution := _orphans_by_dataset(report)) else {}),
        # WHICH OF THEM WILL CLEAR THEMSELVES, beside the distribution — the difference between a
        # finding to act on and a clock to wait out. Same omit-when-empty rule as the line above.
        **({"orphans_by_reclaimability": split} if (split := _orphans_by_reclaimability(report)) else {}),
        # STORAGE PER BUCKET ([[LH-074]]), largest first and bounded — the roll-up is only useful
        # if the biggest tenants are the ones an operator sees. Omitted when the scan is off.
        **({"bytes_by_bucket": _largest(report.bytes_by_bucket)} if report.bytes_by_bucket else {}),
    }
    # A category that could not be checked is NOT clean, so an unavailable/incomplete run is as
    # loud as a drifting one — otherwise a permanently-broken FGA connection reads as "no drift".
    if report.total or report.unavailable or report.incomplete:
        log.warning("reconcile_drift", extra=summary)
    else:
        log.info("reconcile_clean", extra=summary)
    # BEFORE the purge, and AFTER the report it consumes. Raising a floor does not clean THIS scan's
    # report — Lance reclaims at the table's next pass, and the next scan sees the smaller
    # number — so the ordering here buys nothing for the purge gate and is chosen for the other
    # reason: the report is what identifies the stranded datasets, and the purge must be gated on
    # the report as it was MEASURED, never on one a write in between could have changed.
    # BEFORE the floor raise and the purge, because it is the only one of the three that RESTORES
    # rather than reclaims: a tenant this revives may own datasets the other two would otherwise
    # act on while nobody can administer them.
    if read_sources:
        rebuilt = await rebuild_tuples(settings, report=report, sources=read_sources[0])
        payload["tuple_rebuild"] = rebuilt.model_dump(mode="json")
        if rebuilt.written or rebuilt.unjustified or rebuilt.error:
            # NAMED at WARNING: every entry is an authorization change (or a tenant that cannot be
            # recovered), and a count answers neither question an operator has about it.
            log.warning(
                "tuple_rebuild_result",
                extra={
                    "dry_run": rebuilt.dry_run,
                    "written": [f"{t.user} {t.relation} {t.object} <- {t.justified_by}" for t in rebuilt.written],
                    "unjustified": rebuilt.unjustified,
                    "capped": rebuilt.capped,
                    "error": rebuilt.error,
                },
            )
    # AFTER the rebuild, and the order is load-bearing rather than tidy: the rebuild REVIVES a
    # tenant by re-asserting the tenancy pointer its registry record justifies, and this pass
    # revokes authz for objects no record names. Running the revoke first would let one tick revoke
    # a pointer the next tick re-writes, and an operator reading the audit stream would see an
    # estate arguing with itself. Both read THIS tick's report, never a fresh scan.
    repaired = await repair_drift(settings, report=report)
    payload["drift_repair"] = repaired.model_dump(mode="json")
    if repaired.revoked or repaired.kept_by_the_catalog or repaired.error:
        # NAMED at WARNING for the rebuild's reason in the other direction: every entry removed a
        # grant surface, and "revoked 50 objects" tells an operator nothing they can check.
        log.warning(
            "drift_repair_result",
            extra={
                "dry_run": repaired.dry_run,
                "revoked": [f"{r.fga_object} ({r.tuples} tuples) <- {r.justified_by}" for r in repaired.revoked],
                "refused": repaired.refused,
                "kept_by_the_catalog": repaired.kept_by_the_catalog,
                "capped": repaired.capped,
                "error": repaired.error,
            },
        )

    # [[LH-061]]'s STORAGE tier, and it deletes no bytes: a trash record goes only where the
    # location it names is PROVEN to hold nothing. `repair.py` refuses `orphaned_trash` because the
    # record is the only remaining pointer to those bytes; probing first satisfies that objection
    # rather than overturning it. AFTER the authz repair and BEFORE the purge, because the purge's
    # gate is the drift report as MEASURED and a record cleared here was never purgeable anyway —
    # its root is one no purge tick can reach.
    # The record is ended by the catalog ([[XC-084]]): maintenance holds no write of its own, and the catalog ends a
    # record only once its own listing finds the place empty, revoking what grants the object still holds.
    def _clear(record: Tombstone) -> None:
        end_trash(record.kind, record.uuid, settings)

    probe = partial(objects_held, storage_options=settings.storage_options())
    swept = await run_in_threadpool(sweep_tombstones, settings, report=report, probe=probe, delete=_clear)
    payload["tombstone_sweep"] = swept.model_dump(mode="json")
    if swept.swept or swept.refused or swept.error:
        # NAMED at WARNING like its siblings: each entry removed a recovery record, and "swept 50"
        # tells an operator nothing they can check against the store.
        log.warning(
            "tombstone_sweep_result",
            extra={
                "dry_run": swept.dry_run,
                "swept": [f"{s.kind}:{s.id} <- {s.location} ({s.probed_objects} objects)" for s in swept.swept],
                "refused": swept.refused,
                "capped": swept.capped,
                "error": swept.error,
            },
        )
    # ADR 0146: a drop past its deadline loses its grants on every tick, ungated by the report and by the purge's
    # opt-in, since it deletes no byte. Before the purge, which then finds nothing left to revoke.
    payload["trash_release"] = await _release_expired_trash(settings)
    return payload, report, (read_sources[0] if read_sources else None)


async def _release_expired_trash(settings: MaintenanceSettings) -> dict[str, Any]:
    """One tick's release of the drops past their deadline, logged NAMED at WARNING when it did or held anything: each
    entry removed every grant on an object, and a count names no one to check."""
    released = await release_expired_trash(settings, control_root=settings.resolved_control_root)
    if released.released or released.held or released.reason:
        log.warning(
            "trash_release_result",
            extra={
                "released": [f"{r.kind}:{r.uuid} ({r.tuples_revoked} tuples) <- {r.id}" for r in released.released],
                "held": [{"id": f"{h.kind}:{h.id}", "reason": h.reason} for h in released.held],
                "reason": released.reason,
            },
        )
    return released.model_dump(mode="json")


#: How many identities per drift category reach the log line. A drifting estate can carry thousands and
#: one WARNING must not become the report; a truncated list SAYS it was truncated, so a reader can never
#: mistake the sample for the set.
_DRIFT_NAMES_PER_CATEGORY = 10


def _swept_table_ids(settings: MaintenanceSettings) -> TableIds:
    """Every table the swept buckets' manifests name. Blocking store reads: run off the event loop."""
    options = settings.storage_options()
    return table_ids({f"s3://{bucket}" for bucket in buckets_to_sweep(settings, options)}, options)


def _largest(totals: dict[str, int], limit: int = 10) -> dict[str, int]:
    """The `limit` biggest entries, descending. A whole-estate map would be hundreds of keys on one line."""
    return dict(sorted(totals.items(), key=lambda kv: -kv[1])[:limit])


def _drift_names(report: ReconcileReport) -> dict[str, list[str]]:
    """The IDENTITIES behind each non-zero drift count, bounded per category.

    Counts alone leave an operator nowhere to learn WHICH findings, and no task's result is stored. Measured on the
    live estate 2026-09-10: an operator read "orphan_buckets: 12" with nowhere to learn which twelve, so a finding is
    unactionable in exactly the deployment that reports it.

    Every finding model spells its identity differently (`bucket`, `namespace`, `id`, `path`, a
    `kind`/`id` pair), so this asks the MODEL rather than hard-coding a field per category: a new
    category is named automatically instead of silently reporting an empty list.
    """
    named: dict[str, list[str]] = {}
    for category in CATEGORIES:
        findings = getattr(report, category, None)
        if not findings:
            continue
        ids = [_finding_identity(f) for f in findings[:_DRIFT_NAMES_PER_CATEGORY]]
        if len(findings) > _DRIFT_NAMES_PER_CATEGORY:
            ids.append(f"… {len(findings) - _DRIFT_NAMES_PER_CATEGORY} more (truncated)")
        named[category] = ids
    return named


#: How many datasets the orphan distribution names before it starts counting the rest. Same bound and
#: same reason as `_DRIFT_NAMES_PER_CATEGORY`: a drifting estate can carry thousands and one WARNING
#: must not become the report.
_ORPHAN_DATASETS_NAMED = 10


def _orphans_by_dataset(report: ReconcileReport) -> dict[str, int]:
    """How many unreferenced files each dataset holds, largest first, with the tail COUNTED.

    [[LH-094]]. `orphan_files: 932` with ten file paths out of 932 identifies neither the scale of any
    one dataset's problem nor how many datasets are involved, and the per-dataset counts the scan
    already computes (`DatasetOrphanScan.orphans`) reach no log on the success path. The distribution
    is what makes the number actionable — and what makes it DIAGNOSTIC: measured 2026-09-16,
    `orphan_files` went 0 -> 932 in one tick, and "44 datasets, the largest holding 300" separates
    newly-VISIBLE from newly-CREATED where a single total cannot.

    Largest first, because that is the order an operator works in. The tail is one entry carrying its
    own count rather than silence: a truncation that hides how much it hid is the failure this summary
    exists to end, and the shown numbers still sum to `counts["orphan_files"]` so a reader can
    reconcile the two.
    """
    tally = Counter(orphan.dataset for orphan in report.orphan_files)
    if not tally:
        return {}
    ranked = tally.most_common()
    named = dict(ranked[:_ORPHAN_DATASETS_NAMED])
    if remainder := ranked[_ORPHAN_DATASETS_NAMED:]:
        named[f"… {len(remainder)} more datasets (truncated)"] = sum(count for _, count in remainder)
    return named


def _orphans_by_reclaimability(report: ReconcileReport) -> dict[str, int]:
    """How many orphans Lance will still collect, how many it can never see, and how many are unknown.

    [[LH-094]]. The count alone cannot tell an operator whether to act or to wait: measured 2026-09-18
    it fell 1011 -> 32 in ten minutes as the ordinary sweep reclaimed 979 files at the +7-day
    anniversary of the writes that made them, so the same number meant "wait" on one tick and "act" on
    the next. The split is what carries that difference.

    The line is the LISTING FLOOR, not an age, for the kinds Lance lists through it.
    `cleanup_old_versions` clamps its listing of `_transactions/` and `_deletions/` to the earliest
    retained manifest's commit timestamp BEFORE the 7-day unverified rule filters what was listed
    (lance v13.0.0 `rust/lance/src/dataset/cleanup.rs:756-778`), so such a file above that instant is
    unreachable at any `older_than` and with `delete_unverified=True` — permanently, once a dataset has
    collapsed to one live version. A `data/` or `_indices/` orphan is listed past the floor (lance
    #8944), so it always counts as `lance_will_reclaim`.

    `unknown` is its own bucket rather than folded into either, because a floor that could not be read
    is "we could not tell". Reporting that as "Lance will take it" is the conflation `checked=False`
    versus `orphans=[]` already exists to prevent, on the category that decides whether the trash purge
    may run. The three sum to `counts["orphan_files"]`, so a reader can reconcile them.
    """
    if not report.orphan_files:
        return {}
    buckets = {"lance_will_reclaim": 0, "beyond_lance_listing_floor": 0, "unknown": 0}
    for orphan in report.orphan_files:
        key = "unknown" if orphan.reclaimable_by_lance is None else ("lance_will_reclaim" if orphan.reclaimable_by_lance else "beyond_lance_listing_floor")
        buckets[key] += 1
    return buckets


def _finding_identity(finding: BaseModel) -> str:
    """One finding's identity, whatever the model calls it — first present field wins.

    Typed `BaseModel` rather than `object` because every category on `ReconcileReport` is a list of
    pydantic models; a `hasattr` probe would be a runtime check standing in for a fact the type system
    already has.

    Falls back to the dumped mapping rather than an empty string: a finding this cannot name is still a
    finding an operator needs to see, and a blank entry is the failure mode this function exists to end.
    """
    dumped = finding.model_dump()
    # QUALIFIED FIRST. `path` is relative to something on every model that carries one, and `dataset`
    # is that something — `OrphanFile.dataset` exists for exactly this ("so a finding is actionable
    # without re-deriving it") and the loop below reaches `path` before it would ever look. Measured on
    # the live estate 2026-09-16: 932 orphan files across 13 buckets, named `data/00110000….lance` and
    # `_transactions/0-….txn`. Every Lance dataset has both of those directories, so an unqualified
    # path is not merely unhelpful — it is not unique across the report.
    if (root := dumped.get("dataset")) and (relative := dumped.get("path")):
        return f"{root}/{relative}"
    # `location` is LAST, and the position is the whole care required: `OrphanedTrash` carries one
    # too and is identified by its `id`, so putting `location` ahead of `id` would silently rename an
    # existing category's findings in the report an operator reads every tick. Last, it reaches only
    # the models that offer nothing else — `UnregisteredDataset`, whose entire content is that no
    # root claims the table, so a table id (unique only WITHIN a root) cannot be its identity and the
    # URI is the only answer to "where do I go and look".
    for field in ("fga_object", "bucket", "namespace", "top_ns", "path", "task", "id", "location"):
        if value := dumped.get(field):
            return str(value)
    return str(dumped)


async def _queue_what_holds_nothing(settings: MaintenanceSettings, queue: TaskQueue, sources: ReconcileSources | None) -> dict[str, int]:
    """Queue a pass for every table, and a task for every trash record, that neither a schedule nor the queue holds.

    A table's first task comes with the catalog's announcement of it, its birth or its first commit, and each pass
    schedules the next (:mod:`maintenance.services.table_pass`); a table's drop announcement queues its trash task. So what
    this finds is an announcement that was lost, a namespace's trash record, which nothing announces, or a schedule that
    never landed: each is found within one audit, and costs no dataset read. A fired ``@at`` schedule leaves no message
    behind (measured, :meth:`TaskQueue.held`), so a table whose pass ran and scheduled nothing is found here too.

    The tables and the trash are the drift report's own reads of this run (``sources``), so the audit reads each claim
    once; only where the report could not read them all are the claims and the trash read here.
    """
    options = settings.storage_options()
    tables = sources.live_tables() if sources is not None else None
    if tables is None:
        tables = await run_in_threadpool(_held_tables, settings)
    records = sources.trash if sources is not None and sources.trash is not None and not sources.trash_error else None
    if records is None:
        records = await run_in_threadpool(trash.list_all, settings.resolved_control_root, options)
    trashed = {str(uuid): record for record in records if (uuid := trash.object_uuid(record))}
    covered_tables = await queue.held("schedule", "table") | await queue.held("task", "table")
    covered_trash = await queue.held("schedule", "trash") | await queue.held("task", "trash")
    queued = {"tables": 0, "trash": 0}
    for uuid, location in tables.items():
        if uuid not in covered_tables and uuid not in trashed and await queue.enqueue(Task(kind="table", uuid=uuid, location=location)):
            queued["tables"] += 1
    for uuid, record in trashed.items():
        task = Task(kind="trash", uuid=uuid, location=trash.location_of(record), trashed=trash.kind_of(record))
        if uuid not in covered_trash and await queue.enqueue(task):
            queued["trash"] += 1
    if any(queued.values()):
        log.warning("maintenance_unscheduled_queued", extra=queued)
    return queued


def _held_tables(settings: MaintenanceSettings) -> dict[str, str]:
    """Each table's location by its uuid, off every held claim, as an announcement names it (:func:`claimed_location`)."""
    claims = claim_store(settings)
    return {claim.uuid: claimed_location(settings.resolved_control_root, claim) for claim in location_claims.held_claims(claims) if claim.uuid is not None}
