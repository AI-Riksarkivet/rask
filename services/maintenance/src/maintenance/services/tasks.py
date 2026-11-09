"""What each task on the work queue runs, and the worker's lifetime beside the app's (ADR 0198 §2).

Four kinds: a table's pass (:mod:`maintenance.services.table_pass`), which schedules the table's next one; a trash
record's release at its deadline (:mod:`maintenance.services.trash_task`); the hourly audit and the daily scan
(:mod:`maintenance.services.audit`). Each runs on one replica at a time, as the queue hands it out.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from functools import partial
from typing import Final, get_args

from fastapi.concurrency import run_in_threadpool

from lakehouse.control_emit import ControlEmitter
from lakehouse.governed.secrets import fetch_required_secrets
from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import MaintenanceEmitter
from maintenance.core.metrics import create_task_series
from maintenance.services.audit import run_audit, run_scan
from maintenance.services.queue import QueueUnavailableError, Task, TaskKind, TaskQueue, Worker, creds_document
from maintenance.services.store_events import StoreEvents
from maintenance.services.table_pass import TableTask, emit_pass_lineage, emit_zero_baselines, run_table_pass
from maintenance.services.trash_task import run_trash_task


log = logging.getLogger(__name__)


async def run_task(task: Task, *, settings: MaintenanceSettings, queue: TaskQueue, emitter: MaintenanceEmitter, control: ControlEmitter) -> None:
    """Run one task off the queue. A failure raises, and the worker delivers the task again or ends it.

    A table's next pass that the bus does not take is left to the audit, which queues every table no schedule holds:
    running the whole pass again to retry one publish would cost the table's reads for nothing.
    """
    match task.kind:
        case "table":
            outcome = await run_in_threadpool(run_table_pass, TableTask(uuid=task.uuid, location=task.location), settings)
            await emit_pass_lineage(emitter, outcome.results)
            if outcome.next_due is None:
                return
            try:
                await queue.schedule_at(task, outcome.next_due)
            except QueueUnavailableError as exc:
                log.warning("maintenance_next_pass_unscheduled", extra={"uuid": task.uuid, "error": str(exc)[:300]})
        case "trash":
            await run_trash_task(task, settings=settings, queue=queue)
        case "audit":
            await run_audit(settings, queue=queue)
        case "scan":
            await run_scan(settings, control=control)


@asynccontextmanager
async def running_worker(
    settings: MaintenanceSettings, *, ready: Callable[[], bool], emitter: MaintenanceEmitter, control: ControlEmitter
) -> AsyncGenerator[TaskQueue | None]:
    """Connect the work queue as this service's own bus user, keep the audit's and the scan's schedules, and run the
    worker until the app stops; the queue, for the commit lane's publishes. A stack with no bus runs no worker and yields
    ``None``.

    Every loop is kept running: one that ends with an error is logged and started again after a pause, so no failure of
    one read leaves a replica that stays Ready and does nothing. The two cadences are asserted again every
    ``_CADENCE_SECONDS``, since a schedules stream purged or recreated loses them with every table's schedule, and the
    audit they start is what queues those tables again; a cadence is a clock, so asserting it again moves no firing.

    The credential is read from the store, never the environment, and held only in this process. On the way out the
    worker finishes the task it is running, within ``drain_seconds``, before the connection drains: a task cut short is
    delivered to another replica once its heartbeat stops.

    Raises:
        RuntimeError: The store holds no credential for the worker's bus user, or one without its JWT (failing closed).
    """
    if not settings.nats_url:
        yield None
        return
    secret = await run_in_threadpool(fetch_required_secrets, settings.dapr_secret_store, settings.nats_credentials_secret, require="seed")
    if not secret.get("jwt"):
        raise RuntimeError(f"secret 'jwt' unavailable from Dapr store {settings.dapr_secret_store!r}/{settings.nats_credentials_secret!r} — failing closed")
    queue = await TaskQueue.connect(settings.nats_url, creds_document(jwt=secret["jwt"], seed=secret["seed"]), name="maintenance")
    await _assert_cadences(queue, settings)
    emit_zero_baselines()
    create_task_series(get_args(TaskKind.__value__))
    worker = Worker(
        queue,
        run=partial(run_task, settings=settings, queue=queue, emitter=emitter, control=control),
        ready=ready,
        heartbeat_seconds=settings.task_heartbeat_seconds,
        max_deliver=settings.task_max_deliver,
        max_pass_seconds=settings.max_pass_seconds,
        retry_ended_seconds=settings.recheck_hours * 3600,
        store_events=StoreEvents(settings, queue).hold_passes,
    )
    loops = [
        asyncio.create_task(_supervised(worker, "maintenance-tasks", worker.run_tasks), name="maintenance-tasks"),
        asyncio.create_task(_supervised(worker, "maintenance-due", worker.run_due), name="maintenance-due"),
        asyncio.create_task(_supervised(worker, "maintenance-store-events", worker.run_store_events), name="maintenance-store-events"),
        asyncio.create_task(_supervised(worker, "maintenance-cadences", partial(_keep_cadences, worker, queue, settings)), name="maintenance-cadences"),
    ]
    try:
        yield queue
    finally:
        worker.stop()
        _, unfinished = await asyncio.wait(loops, timeout=settings.drain_seconds)
        for loop in unfinished:
            loop.cancel()
        await queue.close()


#: How long a loop that ended with an error waits before it starts again, and how often the cadences are asserted.
_RESTART_SECONDS: Final = 5.0
_CADENCE_SECONDS: Final = 900.0


async def _assert_cadences(queue: TaskQueue, settings: MaintenanceSettings) -> None:
    await queue.schedule_every(Task(kind="audit"), settings.audit_schedule)
    await queue.schedule_every(Task(kind="scan"), settings.scan_schedule)


async def _keep_cadences(worker: Worker, queue: TaskQueue, settings: MaintenanceSettings) -> None:
    """Assert the two cadences again every ``_CADENCE_SECONDS`` until the worker stops."""
    while not worker.stopping():
        await worker.pause(_CADENCE_SECONDS)
        if not worker.stopping():
            await _assert_cadences(queue, settings)


async def _supervised(worker: Worker, name: str, loop: Callable[[], Awaitable[None]]) -> None:
    """Run ``loop`` until the worker stops, starting it again after a pause whenever it ends with an error."""
    while not worker.stopping():
        try:
            await loop()
        except Exception:  # noqa: BLE001 — whatever ended the loop is logged, and the loop runs again
            log.exception("maintenance_loop_failed", extra={"loop": name})
            await worker.pause(_RESTART_SECONDS)
