"""One table's orphan check, run for the ``orphans`` task the work queue hands one replica (owner, 2026-11-09/10).

Each table's check is a task of its own (``maintenance.task.orphans.<uuid>``), held on a schedule of its own, and takes
its table's claim, so it never runs beside the table's pass or another check of it (:func:`maintenance.services.queue
._claim_key`). What it costs is the table's: the keyed reads a table's pass makes, then one scan of its main dataset
(:func:`maintenance.services.orphans.scan_dataset`), never an estate-wide listing. The check:

1. **Applies the table pass's own guards before touching anything** (:func:`maintenance.services.table_pass
   .run_table_pass`): the location's claim still records the task's uuid, no trash record keeps the table, its bucket is
   one maintenance governs, and a ``__manifest`` row names it. A table gone or in the trash is checked no more; its
   bytes are the trash's, and nothing in them changes what the purge deletes.
2. **Scans the table's main dataset** for files no live version references, with an age cut: a file the store cannot
   place before the scan began reading is no finding.
3. **Raises the listing floor** when it found stranded files and none whose class is undecided
   (:func:`maintenance.services.floor.raise_listing_floor`), and the worker then holds a pass of the table, so its cleanup
   runs on the raised floor.
4. **Says when it next runs.** F is a day, C ``MAINTENANCE_ORPHAN_CHECK_CEILING_DAYS``, I the interval this check was
   held at: stranded or undecided files -> F; a raised floor -> max(F, the table's retention + a day), since the floor
   moves only once a pass cleans the manifests beneath the raise; a check that finds nothing, or only files Lance
   reclaims itself -> min(2I, C); an excluded table (a branch, MemWAL, an unsupported flag, files through a base, a
   history or a file count past the scan's ceilings) or a bucket maintenance does not govern -> C; a table no
   ``__manifest`` row names -> F; a read that fails raises, so the worker delivers the check again.

Tables with branches, MemWAL, unsupported flags or files through a base stay unchecked (excluded): the scan subtracts
the main branch's references from a listing, which those layouts break.

Blocking S3/Lance work; the worker runs :func:`run_orphan_check` in the threadpool.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Final

from pydantic import BaseModel

from lakehouse import location_claims, objectfs, trash
from lakehouse.objectfs import fs_and_base
from maintenance.core.config import MaintenanceSettings
from maintenance.core.metrics import OrphanCheckOutcome, record_orphan_check
from maintenance.services.floor import FloorRaise, raise_listing_floor
from maintenance.services.naming import PolicyOwners, claim_store
from maintenance.services.orphans import DatasetOrphanScan, OrphanFile, scan_dataset
from maintenance.services.plan import PlanContext, resolve_plan
from maintenance.services.queue import ORPHAN_CHECK_FLOOR_HOURS, Task
from maintenance.services.table_pass import governs, held_location, read_for


log = logging.getLogger(__name__)

#: The shortest interval between two checks of a table (owner, 2026-11-09: fixed at a day).
FLOOR: Final = timedelta(hours=ORPHAN_CHECK_FLOOR_HOURS)
#: How long past a raise the table's retention is waited on, beyond the retention itself: the manifests beneath the
#: raise are reclaimable once older than it, and the pass that reclaims them comes within its recheck.
_AFTER_RETENTION: Final = timedelta(days=1)


class OrphanCheckIncompleteError(RuntimeError):
    """A read the check needs failed, so it decided nothing: the worker delivers the check again."""


class OrphanCheck(BaseModel):
    """What one table's check found, and when the table is next checked (``None``: no more)."""

    outcome: OrphanCheckOutcome
    next_check: timedelta | None
    #: Whether this check committed a raise, after which the worker holds the table's pass.
    raised: bool = False
    #: The table as ``__manifest`` names it, for a check that got as far as reading its name.
    table: str | None = None


def run_orphan_check(task: Task, settings: MaintenanceSettings, *, now: datetime | None = None) -> OrphanCheck:
    """One table's check (the module docstring has the rule), logged as one line and counted.

    Raises:
        OrphanCheckIncompleteError: A read the check needs failed; nothing was decided.
    """
    now = now or datetime.now(UTC)
    try:
        checked, found = _check(task, settings, now=now)
    except OrphanCheckIncompleteError as exc:
        record_orphan_check(outcome="incomplete", stranded=0)
        log.warning("maintenance_orphan_check", extra={"uuid": task.uuid, "location": task.location, "outcome": "incomplete", "reason": str(exc)})
        raise
    stranded = [orphan for orphan in found.orphans if orphan.reclaimable_by_lance is False] if found is not None else []
    record_orphan_check(outcome=checked.outcome, stranded=len(stranded))
    log.info("maintenance_orphan_check", extra={**_described(task, checked, found), "next_check_hours": _hours(checked.next_check)})
    return checked


def _check(task: Task, settings: MaintenanceSettings, *, now: datetime) -> tuple[OrphanCheck, DatasetOrphanScan | None]:
    ceiling = timedelta(days=settings.orphan_check_ceiling_days)
    if not settings.orphan_check_enabled:
        return OrphanCheck(outcome="disabled", next_check=None), None
    options = settings.storage_options()
    try:
        held = location_claims.holder(claim_store(settings), task.location)
        if held is None or held.uuid != task.uuid:
            return OrphanCheck(outcome="gone", next_check=None), None
        if trash.keeps(settings.resolved_control_root, options, task.uuid):
            return OrphanCheck(outcome="trashed", next_check=None), None
    except OSError as exc:
        raise OrphanCheckIncompleteError(f"the claim or the trash could not be read: {exc}") from exc
    governed = governs(task.location, top_namespace=held.segments[0], settings=settings, options=options)
    if governed is None:
        raise OrphanCheckIncompleteError("the table's warehouse could not be read")
    if not governed:
        return OrphanCheck(outcome="ungoverned", next_check=ceiling), None
    readings = read_for(task.location, held, settings, now=now)
    if readings is None:
        raise OrphanCheckIncompleteError("the table's __manifest rows or its policy could not be read")
    location = held_location(task.location)
    table = readings.table_ids.table_of(location)
    if table is None or table != held.table or readings.owners is None:
        return OrphanCheck(outcome="unnamed", next_check=FLOOR), None
    checked, found = _decide(task, table=table, location=location, context=readings.context, owners=readings.owners, settings=settings)
    return checked.model_copy(update={"table": table}), found


def _decide(
    task: Task, *, table: str, location: str, context: PlanContext, owners: PolicyOwners, settings: MaintenanceSettings
) -> tuple[OrphanCheck, DatasetOrphanScan]:
    """Scan the table ``__manifest`` names ``table`` at ``location`` and decide on what the scan found."""
    options = settings.storage_options()
    ceiling = timedelta(days=settings.orphan_check_ceiling_days)
    found = _scan(location, settings)
    if found.excluded:
        return OrphanCheck(outcome="excluded", next_check=ceiling), found
    if not found.checked:
        try:
            declared_only = not objectfs.is_lance_dataset_root(location, options)
        except OSError as exc:
            raise OrphanCheckIncompleteError(f"whether a dataset lies at the location could not be read: {exc}") from exc
        if declared_only:
            return OrphanCheck(outcome="clean", next_check=_doubled(task, ceiling)), found
        raise OrphanCheckIncompleteError(found.reason or "the dataset could not be read")
    stranded = [orphan for orphan in found.orphans if orphan.reclaimable_by_lance is False]
    undecided = [orphan for orphan in found.orphans if orphan.reclaimable_by_lance is None]
    if not stranded and not undecided:
        return OrphanCheck(outcome="reclaimable" if found.orphans else "clean", next_check=_doubled(task, ceiling)), found
    if undecided:
        return OrphanCheck(outcome="stranded", next_check=FLOOR), found
    plan = resolve_plan(location, context=context, owners=owners)
    if not plan.cleanup_enabled:
        log.info("maintenance_floor_not_raised", extra={"uuid": task.uuid, "table": table, "reason": "the table's policy keeps its whole history"})
        return OrphanCheck(outcome="stranded", next_check=FLOOR), found
    raised = raise_listing_floor(location, table=table, stranded=stranded, settings=settings)
    _log_raise(task, table, raised)
    if raised.version is None:
        return OrphanCheck(outcome="stranded", next_check=FLOOR), found
    retention = plan.older_than or timedelta(days=settings.older_than_days)
    return OrphanCheck(outcome="raised", next_check=max(FLOOR, retention + _AFTER_RETENTION), raised=True), found


def _scan(location: str, settings: MaintenanceSettings) -> DatasetOrphanScan:
    options = settings.storage_options()
    try:
        fs, prefix = fs_and_base(location, options)
    except OSError as exc:
        raise OrphanCheckIncompleteError(f"object storage could not be opened: {exc}") from exc
    return scan_dataset(fs, location, prefix=prefix, storage_options=options)


def _doubled(task: Task, ceiling: timedelta) -> timedelta:
    """Twice the interval this check was held at, never under the floor nor past the ceiling."""
    return min(max(2 * timedelta(hours=task.interval_hours), FLOOR), ceiling)


def _hours(interval: timedelta | None) -> int | None:
    return None if interval is None else int(interval.total_seconds() // 3600)


def _by_class(orphans: list[OrphanFile]) -> dict[str, dict[str, int]]:
    """Counts and bytes of the orphans by class: Lance reclaims them itself, they are stranded above the floor, or the
    floor could not be read."""
    classes: dict[str, dict[str, int]] = {}
    for orphan in orphans:
        name = "undecided" if orphan.reclaimable_by_lance is None else ("lance_will_reclaim" if orphan.reclaimable_by_lance else "stranded")
        tally = classes.setdefault(name, {"files": 0, "bytes": 0})
        tally["files"] += 1
        tally["bytes"] += orphan.size_bytes
    return classes


def _described(task: Task, checked: OrphanCheck, found: DatasetOrphanScan | None) -> dict[str, object]:
    """The check's one log line: the table, what it found by class, why a table was excluded, and what it decided."""
    return {
        "uuid": task.uuid,
        "table": checked.table,
        "location": task.location,
        "outcome": checked.outcome,
        "orphans": _by_class(found.orphans) if found is not None else {},
        "reason": found.reason if found is not None and not found.checked else None,
        "interval_hours": task.interval_hours,
    }


def _log_raise(task: Task, table: str, raised: FloorRaise) -> None:
    """A raise writes a version to a governed table, so it is logged at WARNING and NAMED, as is a refusal of one."""
    log.warning(
        "floor_raise_result",
        extra={
            "uuid": task.uuid,
            "table": table,
            "dataset": raised.dataset,
            "dry_run": raised.dry_run,
            "version": raised.version,
            "orphan_files": raised.orphan_files,
            "orphan_bytes": raised.orphan_bytes,
            "floor_before": raised.floor_before,
            "refused": raised.refused,
        },
    )
