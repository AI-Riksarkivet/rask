"""A trashed object's task (ADR 0198 §2): its release at its deadline, and the pass of a table an undrop restored.

The commit lane queues the task when the catalog announces a table's drop (``drop_table``, filed after its record), and
the audit queues it for any record no task or schedule holds, a namespace's among them. The task holds itself until the
record's deadline and :data:`lakehouse.trash.RELEASE_SETTLE` (a schedule), then asks the catalog to release this record
alone (:func:`maintenance.services.trash_release.release_expired_trash`): the release deletes no byte, and the purge of
the bytes stays the daily scan's, gated on the drift report that scan produced. A record gone by the time its task runs was undropped or
ended; a table undropped stands again under the same uuid, so it gets a table task, as a table the catalog makes does.
A drop that filed no record, a destructive one, leaves its task nothing to read past that.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from fastapi.concurrency import run_in_threadpool

from lakehouse import location_claims, trash
from maintenance.core.config import MaintenanceSettings
from maintenance.services.naming import claim_store
from maintenance.services.queue import Task, TaskQueue
from maintenance.services.trash_release import release_expired_trash


log = logging.getLogger(__name__)


class TrashUnreadableError(RuntimeError):
    """The trash, or the registry its release reads, could not be read: the task is tried again."""


async def run_trash_task(task: Task, *, settings: MaintenanceSettings, queue: TaskQueue, now: datetime | None = None) -> None:
    """Release ``task``'s record once its deadline has settled, holding the task until then.

    Raises:
        TrashUnreadableError: The trash or the warehouse registry could not be read, so nothing was decided.
    """
    now = now or datetime.now(UTC)
    options = settings.storage_options()
    record = await run_in_threadpool(trash.get, settings.resolved_control_root, options, task.uuid, kind=task.trashed)
    if record is None:
        await _pass_a_restored_table(task, settings=settings, queue=queue)
        return
    if trash.is_released(record):
        return
    # Held with the record's location, which a drop's announcement does not name: when it fires on a record an undrop
    # cleared, the location is how the restored table's claim is found.
    task = task if task.location else task.model_copy(update={"location": trash.location_of(record)})
    if (due := trash.release_at(record)) is None:
        return
    if due > now:
        await queue.schedule_at(task, due)
        return
    released = await release_expired_trash(settings, now=now, only=task.uuid)
    if not released.ran:
        raise TrashUnreadableError(released.reason or "the trash could not be read")
    if released.held:
        # Held for a reason a later read may clear: a manifest unreadable now, a table registered again meanwhile.
        await queue.schedule_at(task, now + timedelta(seconds=settings.retry_seconds))


async def _pass_a_restored_table(task: Task, *, settings: MaintenanceSettings, queue: TaskQueue) -> None:
    """Queue a pass for the table ``task`` names when its location's claim is held under its uuid again: an undrop."""
    if task.trashed != "table" or not task.location:
        return
    claims = claim_store(settings)
    held = await run_in_threadpool(location_claims.holder, claims, task.location)
    if held is not None and held.uuid == task.uuid:
        log.info("maintenance_restored_table_queued", extra={"uuid": task.uuid, "table": held.table})
        await queue.enqueue(Task(kind="table", uuid=task.uuid, location=task.location))
