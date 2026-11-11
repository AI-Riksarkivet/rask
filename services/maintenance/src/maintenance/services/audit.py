"""The audit and the scan (ADR 0198 §2, decision 2): each one task per firing of its schedule, run by one replica.

Both read three stores and report their disagreements, repair what the report justifies, and release every drop past
its deadline (ADR 0146) that its own trash task has not, gated on neither the report nor the purge's opt-in, since it
deletes nothing. The hourly AUDIT (:func:`run_audit`) walks no storage, so it opens no table and lists no bucket, and
queues a pass for every table and a task for every trash record that holds neither a schedule nor a queued task, which
is what bounds a lost birth or a lost schedule to one firing. The daily SCAN (:func:`run_scan`, owner 2026-11-09) walks
every bucket for its datasets too, and then, gated on THAT report running clean, reclaims expired trash (#79). The purge
rides the scan rather than a table's pass precisely because its permission comes from the report: it consumes the
report object the same run produced, never a stored one. It is off by default (`MAINTENANCE_TRASH_PURGE_ENABLED`), so
the shipped configuration deletes no trashed byte.

One task of each, once per firing whatever the replica count: each schedule is a NATS message schedule (``@hourly``, and
``0 30 0 * * *`` for the scan, by default), each on a subject of its own, and the queue holds one task per subject. Both
repair the estate from a drift report of their own, so they hold one claim (:mod:`maintenance.services.queue`): the two
never run at once, and the later waits out the other.
"""

import logging
import secrets
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from lakehouse import location_claims, trash
from lakehouse.control_emit import ControlEmitter
from maintenance.core.config import MaintenanceSettings
from maintenance.services.bucket_events import report_bucket_events
from maintenance.services.clone_endings import finish_clone_endings
from maintenance.services.credentials import end_trash, grant_census
from maintenance.services.naming import claim_store, claimed_location
from maintenance.services.orphan_check import FLOOR
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
    """The hourly audit: the drift report without the storage walk, its repairs, the release of every drop past its
    deadline (ADR 0146), and last the tasks and the orphan checks no schedule holds.

    The report is returned AND logged as one structured record: the log lands in the OTLP stream where drift is actually
    noticed. It leaves the walk to the daily scan (:func:`run_scan`, owner 2026-11-09), and with it the purge, whose gate
    refuses a report whose run walked no storage (:func:`maintenance.services.purge.report_is_clean`).
    """
    payload, _, sources = await _report_and_repair(settings, walk=False)
    if settings.store_events_expected and (bucket_events := await run_in_threadpool(report_bucket_events, settings)) is not None:
        payload["store_events"] = bucket_events.model_dump()
    if queue is not None:
        payload["unscheduled"] = await _queue_what_holds_nothing(settings, queue, sources)
    return payload


async def run_scan(settings: MaintenanceSettings, *, control: ControlEmitter) -> dict[str, Any]:
    """The daily scan: the drift report WITH the storage walk, its repairs and the release, then the #79 expired-trash
    purge.

    **The purge lives HERE, in the same run as the report.** Its gate is "the drift report ran clean"
    (:func:`maintenance.services.purge.report_is_clean`), so it must consume the report THIS run produced: a purge
    reading a stored or previous report would certify a state that no longer exists. Reclamation is off by default
    (`MAINTENANCE_TRASH_PURGE_ENABLED`), so on the shipped configuration this adds one report key and deletes nothing.
    """
    payload, report, _ = await _report_and_repair(settings, walk=True)
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


async def _report_and_repair(settings: MaintenanceSettings, *, walk: bool) -> tuple[dict[str, Any], ReconcileReport, ReconcileSources | None]:
    """One cross-store drift REPORT, the storage walk with it when ``walk``, the repairs it justifies and the release of
    every drop past its deadline; the payload to log, the report, for a caller whose next step it gates, and the stores
    it read."""
    # The stores this run read, so the tuple rebuild below writes only grants the records the
    # REPORT saw actually justify — see `reconcile`'s `into_sources`.
    read_sources: list[ReconcileSources] = []
    report = await reconcile(
        settings,
        partial(grant_census, settings),
        warehouses_enabled=settings.warehouses_enabled,
        control_root=settings.resolved_control_root,
        into_sources=read_sources,
        walk=walk,
    )
    payload = report.model_dump(mode="json")
    summary = {
        "total": report.total,
        "counts": report.counts,
        "unavailable": [u.category for u in report.unavailable],
        "skipped": [s.category for s in report.skipped],
        # SOURCE AND REASON, because this list GATES RECLAMATION and the source alone cannot be
        # acted on. `report_is_clean` blocks the #79 purge while anything is incomplete — measured
        # 2026-09-16: three entries all reading `storage:lance-catalog`. Each `IncompleteScan` carries
        # its `reason`, which names what an operator can act on.
        "incomplete": [f"{i.source}: {i.reason}" for i in report.incomplete],
        # WHICH ones, not just how many — see `_drift_names`. Without this the WARNING is a number
        # an operator cannot act on: no task's result is stored.
        "findings": _drift_names(report),
    }
    # A category that could not be checked is NOT clean, so an unavailable/incomplete run is as
    # loud as a drifting one — otherwise a permanently-broken FGA connection reads as "no drift".
    if report.total or report.unavailable or report.incomplete:
        log.warning("reconcile_drift", extra=summary)
    else:
        log.info("reconcile_clean", extra=summary)
    # BEFORE the purge, because it RESTORES rather than reclaims: a tenant this revives may own
    # datasets the purge would otherwise act on while nobody can administer them.
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
    # ADR 0198 §1: a clone's tag a door could not release is released by the catalog, asked here; it deletes no byte.
    payload["clone_endings"] = (await finish_clone_endings(settings)).model_dump(mode="json")
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


def _finding_identity(finding: BaseModel) -> str:
    """One finding's identity, whatever the model calls it — first present field wins.

    Typed `BaseModel` rather than `object` because every category on `ReconcileReport` is a list of
    pydantic models; a `hasattr` probe would be a runtime check standing in for a fact the type system
    already has.

    Falls back to the dumped mapping rather than an empty string: a finding this cannot name is still a
    finding an operator needs to see, and a blank entry is the failure mode this function exists to end.
    """
    dumped = finding.model_dump()
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
    """Queue a pass for every table, and a task for every trash record, that neither a schedule nor the queue holds, and
    schedule an orphan check for every table holding neither a check's schedule nor a queued check.

    A table's first task comes with the catalog's announcement of it, its birth or its first commit, and each pass
    schedules the next (:mod:`maintenance.services.table_pass`); a table's drop announcement queues its trash task. So what
    this finds is an announcement that was lost, a namespace's trash record, which nothing announces, or a schedule that
    never landed: each is found within one audit, and costs no dataset read. A fired ``@at`` schedule leaves no message
    behind (measured, :meth:`TaskQueue.held`), so a table whose pass ran and scheduled nothing is found here too.

    The tables and the trash are the drift report's own reads of this run (``sources``), so the audit reads each claim
    once; only where the report could not read them all are the claims and the trash read here.

    A table's first check is held by its first pass, and each check holds its next, so a table found here lost its
    check's schedule, or predates the checks. Its check is held a jittered time within the floor rather than queued at
    once, so a lost schedules stream does not start every table's check in the same hour; held at the floor's interval,
    since the interval a lost schedule carried is lost with it. A table in the trash gets none. A trash record already
    released, or carrying no deadline, gets no task: its task would end holding nothing and be queued again next hour.
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
    covered_checks = (await queue.held("schedule", "orphans") | await queue.held("task", "orphans")) if settings.orphan_check_enabled else set(tables)
    queued = {"tables": 0, "trash": 0, "checks": 0}
    now = datetime.now(UTC)
    for uuid, location in tables.items():
        if uuid in trashed:
            continue
        if uuid not in covered_tables and await queue.enqueue(Task(kind="table", uuid=uuid, location=location)):
            queued["tables"] += 1
        if uuid not in covered_checks:
            jitter = timedelta(seconds=secrets.randbelow(int(FLOOR.total_seconds())))
            queued["checks"] += await queue.schedule_first(Task(kind="orphans", uuid=uuid, location=location), now + jitter)
    for uuid, record in trashed.items():
        # A released or undated record's task ends holding nothing (:mod:`maintenance.services.trash_task`), so queueing
        # one would queue it again every hour. The daily scan's purge reaches a released record without a task, and an
        # undated one is never released (:func:`lakehouse.trash.release_at`).
        if trash.is_released(record) or trash.release_at(record) is None:
            continue
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
