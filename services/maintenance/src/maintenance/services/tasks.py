"""What each task on the work queue runs, and the worker's lifetime in its own process (ADR 0198 §2).

Five kinds: a table's pass (:mod:`maintenance.services.table_pass`), which schedules the table's next one and, when none
is held, its orphan check; a table's orphan check (:mod:`maintenance.services.orphan_check`), which schedules its next
one and, after it raised the table's listing floor, holds the table's pass; a trash record's release at its deadline
(:mod:`maintenance.services.trash_task`); the hourly audit and the daily scan (:mod:`maintenance.services.audit`). Each
runs on one replica at a time, as the queue hands it out.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Final, get_args

from fastapi.concurrency import run_in_threadpool

from lakehouse.control_emit import ControlEmitter
from lakehouse.governed.secrets import read_required_secret
from lakehouse.supervision import EndProcess, Supervisor, end_process
from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import MaintenanceEmitter
from maintenance.core.metrics import create_orphan_check_series, create_task_series
from maintenance.services.audit import run_audit, run_scan
from maintenance.services.orphan_check import FLOOR, run_orphan_check
from maintenance.services.queue import SAMPLE_SECONDS, QueueUnavailableError, Task, TaskKind, TaskQueue, Worker, creds_document, sample_queue
from maintenance.services.store_events import StoreEvents
from maintenance.services.table_pass import TableTask, emit_pass_lineage, emit_zero_baselines, run_table_pass
from maintenance.services.trash_task import run_trash_task


log = logging.getLogger(__name__)


async def run_task(task: Task, *, settings: MaintenanceSettings, queue: TaskQueue, emitter: MaintenanceEmitter, control: ControlEmitter) -> None:
    """Run one task off the queue. A failure raises, and the worker delivers the task again or ends it.

    A table's next pass, or check, that the bus does not take is left to the audit, which queues or schedules every one no
    schedule holds: running the whole pass again to retry one publish would cost the table's reads for nothing.
    """
    match task.kind:
        case "table":
            outcome = await run_in_threadpool(run_table_pass, TableTask(uuid=task.uuid, location=task.location), settings)
            await emit_pass_lineage(emitter, outcome.results)
            if outcome.next_due is None:
                return
            try:
                await queue.schedule_at(task, outcome.next_due)
                if settings.orphan_check_enabled:
                    # A check held already keeps its time: the guarded hold leaves it standing.
                    await queue.schedule_first(Task(kind="orphans", uuid=task.uuid, location=task.location), datetime.now(UTC) + FLOOR)
            except QueueUnavailableError as exc:
                log.warning("maintenance_next_pass_unscheduled", extra={"uuid": task.uuid, "error": str(exc)[:300]})
        case "orphans":
            await _check_orphans(task, settings=settings, queue=queue)
        case "trash":
            await run_trash_task(task, settings=settings, queue=queue)
        case "audit":
            await run_audit(settings, queue=queue)
        case "scan":
            await run_scan(settings, control=control)


async def _check_orphans(task: Task, *, settings: MaintenanceSettings, queue: TaskQueue) -> None:
    """Run ``task``'s check, hold the table's pass after a raise so its cleanup runs on the raised floor, and hold the next
    check at the interval the check chose, which rides the schedule's body."""
    checked = await run_in_threadpool(run_orphan_check, task, settings)
    now = datetime.now(UTC)
    try:
        if checked.raised:
            await queue.follow_up(Task(kind="table", uuid=task.uuid, location=task.location), now + timedelta(seconds=settings.commit_pass_delay_seconds))
        if checked.next_check is not None:
            hours = int(checked.next_check.total_seconds() // 3600)
            await queue.schedule_at(task.model_copy(update={"interval_hours": hours}), now + checked.next_check)
    except QueueUnavailableError as exc:
        log.warning("maintenance_next_check_unscheduled", extra={"uuid": task.uuid, "error": str(exc)[:300]})


@asynccontextmanager
async def connected_queue(settings: MaintenanceSettings) -> AsyncGenerator[TaskQueue | None]:
    """The work queue, connected as this service's own bus user and drained on the way out; ``None`` for a stack with no
    bus. The web pods hold it for the write-event lane's publishes alone, and run no worker.

    The credential is read from the store, never the environment, and held only in this process.

    Raises:
        RuntimeError: The store holds no credential for the worker's bus user, or one without its JWT (failing closed).
    """
    if not settings.nats_url:
        yield None
        return
    secret = await run_in_threadpool(read_required_secret, settings.nats_credentials_secret, require="seed")
    if not secret.get("jwt"):
        raise RuntimeError(f"secret {settings.nats_credentials_secret!r} gave this process no 'jwt': failing closed (the secret store is the sole source)")
    queue = await TaskQueue.connect(settings.nats_url, creds_document(jwt=secret["jwt"], seed=secret["seed"]), name="maintenance")
    try:
        yield queue
    finally:
        await queue.close()


@asynccontextmanager
async def running_worker(
    settings: MaintenanceSettings,
    *,
    ready: Callable[[], bool],
    emitter: MaintenanceEmitter,
    control: ControlEmitter,
    end: EndProcess = end_process,
) -> AsyncGenerator[TaskQueue | None]:
    """Connect the work queue (:func:`connected_queue`), keep the audit's and the scan's schedules, and run the worker
    until its process stops; the queue it runs on. A stack with no bus runs no worker and yields ``None``.

    Every loop runs under a supervisor (`lakehouse.supervision`): one that ends before the worker stops, with an error or
    without, ends the process by ``end``, so no fault of one leaves a replica that stays Ready and does nothing (ADR 0203).
    A loop waits out the bus's own errors itself (a bind, a fetch, a settle or a cadence the bus does not take), so only
    a fault ends one. The two cadences are asserted again every ``_CADENCE_SECONDS``, since a schedules stream purged or
    recreated loses them with every table's schedule, and the audit they start is what queues those tables again; a
    cadence is a clock, so asserting it again moves no firing.

    On the way out the worker finishes the task it is running, within ``drain_seconds``, before the connection drains:
    a task cut short is delivered to another replica once its heartbeat stops. A worker that cannot start, because the
    bus refuses its cadences or holds no claims bucket, leaves the connection closed all the same.

    Raises:
        RuntimeError: The store holds no credential for the worker's bus user, or one without its JWT (failing closed).
        ClaimsBucketMissingError: The bus holds no claims bucket.
    """
    async with connected_queue(settings) as queue:
        if queue is None:
            yield None
            return
        async with _worker_on(queue, settings, ready=ready, emitter=emitter, control=control, end=end):
            yield queue


@asynccontextmanager
async def _worker_on(
    queue: TaskQueue,
    settings: MaintenanceSettings,
    *,
    ready: Callable[[], bool],
    emitter: MaintenanceEmitter,
    control: ControlEmitter,
    end: EndProcess,
) -> AsyncGenerator[None]:
    """Run the worker's loops on ``queue`` until the block ends, then drain them."""
    await queue.bind_claims()
    await _assert_cadences(queue, settings)
    emit_zero_baselines()
    create_task_series(get_args(TaskKind.__value__))
    create_orphan_check_series()
    worker = Worker(
        queue,
        run=partial(run_task, settings=settings, queue=queue, emitter=emitter, control=control),
        ready=ready,
        heartbeat_seconds=settings.task_heartbeat_seconds,
        ack_wait_seconds=settings.task_ack_wait_seconds,
        max_deliver=settings.task_max_deliver,
        max_pass_seconds=settings.max_pass_seconds,
        max_seconds_by_kind=settings.max_seconds_by_kind,
        retry_ended_seconds=settings.recheck_hours * 3600,
        store_events=StoreEvents(settings, queue).hold_passes,
        end=end,
    )
    async with Supervisor(end=end) as loops:
        loops.keep("maintenance-tasks", worker.run_tasks)
        loops.keep("maintenance-due", worker.run_due)
        loops.keep("maintenance-store-events", worker.run_store_events)
        loops.keep("maintenance-cadences", partial(_keep_cadences, worker, queue, settings))
        loops.keep("maintenance-queue-sampler", partial(_sample_queue, worker, queue))
        try:
            yield
        finally:
            # From here a loop ends because the worker drains it, which is no fault.
            loops.wind_down()
            await worker.drain(loops.running, settings.drain_seconds)


#: How often the cadences are asserted.
_CADENCE_SECONDS: Final = 900.0


async def _assert_cadences(queue: TaskQueue, settings: MaintenanceSettings) -> None:
    await queue.schedule_every(Task(kind="audit"), settings.audit_schedule)
    await queue.schedule_every(Task(kind="scan"), settings.scan_schedule)


async def _keep_cadences(worker: Worker, queue: TaskQueue, settings: MaintenanceSettings) -> None:
    """Assert the two cadences again every ``_CADENCE_SECONDS`` until the worker stops; a bus that does not take them is
    asked again at the next."""
    while not worker.stopping():
        await worker.pause(_CADENCE_SECONDS)
        if worker.stopping():
            return
        try:
            await _assert_cadences(queue, settings)
        except QueueUnavailableError as exc:
            log.warning("maintenance_cadences_unasserted", extra={"error": str(exc)[:300]})


async def _sample_queue(worker: Worker, queue: TaskQueue) -> None:
    """Sample the queue's streams and durables into their gauges every ``SAMPLE_SECONDS`` until the worker stops, the first
    at once (:func:`maintenance.services.queue.sample_queue`)."""
    while not worker.stopping():
        await sample_queue(queue)
        await worker.pause(SAMPLE_SECONDS)
