"""Maintenance's work queue on NATS JetStream (ADR 0198 §2): the tasks every replica's worker pulls, and the schedules
that put a task back on the queue when its time comes.

THE SERVICE TALKS TO THE BROKER HERE, AND ONLY HERE (ADR 0198 decision 5). Dapr's JetStream subscription sends no
in-progress acknowledgement, so a pass longer than its ack wait would be delivered again beneath itself, and its publish
sets no header but the message id, so it cannot put a schedule (`pubsub/jetstream/jetstream.go:115-145`, components-contrib
e90662b02d70). nats-py is the client, and the worker is a pull worker. The streams and their durable consumers
are the chart's (`chart/templates/nats-stream-job.yaml`), so this binds to them and creates nothing, under a bus user of its
own whose credential is read from the store at start.

Two streams, because a stream holding message schedules may not discard new messages (ADR-51) while the queue must refuse
a second task for a table that has one:

* ``MAINTENANCE_TASKS`` (work queue, one message per subject, a new one discarded): ``maintenance.task.<kind>.<uuid>``.
  A task already queued, or in flight, refuses another, so a burst of commits makes one pass, and a table is never
  passed twice at once.
* ``MAINTENANCE_SCHEDULES`` (work queue, schedules allowed): ``maintenance.schedule.<kind>.<uuid>`` holds when the task
  is next due, a newer one replacing it, and fires into ``maintenance.due.<kind>.<uuid>``, which the worker moves onto
  the queue (:meth:`Worker.run_due`); its ack deletes the fired message, and no consumer reads a held one. Measured on
  nats-server 2.15.2 (2026-11-09): a replaced ``@at`` fires once, with the newer body; one already past fires at once.

A commit puts no task on the queue itself: it holds its table's FOLLOW-UP (:meth:`TaskQueue.follow_up`) a delay out,
beside the table's schedule on ``maintenance.schedule.followup.<kind>.<uuid>``, unless one is already held there. So a
burst of commits makes one pass, at most one per delay, and that pass reads the table after every commit it covers; the
follow-up never replaces the schedule a pass writes. Fired, it waits while a task holds the table, since a pass already
running may have read the table before the commits it follows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Final, Literal

import nats
from nats.aio.client import Client, RawCredentials
from nats.errors import Error as NatsError
from nats.js.errors import APIError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from maintenance.core.metrics import record_pass_overran, record_task_completed, record_task_ended


if TYPE_CHECKING:
    from nats.aio.msg import Msg
    from nats.js import JetStreamContext


log = logging.getLogger(__name__)

TASKS_STREAM: Final = "MAINTENANCE_TASKS"
SCHEDULES_STREAM: Final = "MAINTENANCE_SCHEDULES"
#: The store's change events (:mod:`maintenance.services.store_events`), which the store publishes on ``store.events``.
STORE_EVENTS_STREAM: Final = "STORE_EVENTS"
#: The chart's durable pull consumers: on the tasks, on the fired schedules, and on the store's events.
TASK_CONSUMER: Final = "maintenance-worker"
DUE_CONSUMER: Final = "maintenance-due"
STORE_EVENTS_CONSUMER: Final = "maintenance-store-events"

#: The prefix of the worker's reply subjects. Its own, so no other bus user, each of which may read `_INBOX.>`, reads the
#: tasks it fetches or the answers it gets.
INBOX_PREFIX: Final = "_INBOX_maintenance"
#: How long a worker waits on an empty queue before asking again, and how many fired schedules it moves at once.
_FETCH_TIMEOUT: Final = 5.0
_DUE_BATCH: Final = 10
#: The kinds whose task has a schedule of its own, which the hourly audit reads to find one nothing holds.
_SCHEDULED_KINDS: Final = frozenset({"table", "trash"})
#: The redelivery delay after a failed delivery, doubling up to a ceiling: a store that is down for a while is asked
#: again at a pace it can bear.
_RETRY_FIRST: Final = 15.0
_RETRY_CEILING: Final = 900.0
#: The token a follow-up's subjects carry after their area, and how long a fired follow-up waits for the task holding its
#: table before it asks again.
_FOLLOW_UP: Final = "followup"
_FOLLOW_UP_RETRY: Final = 60.0
#: JetStream's answers to a publish that expects a subject to hold nothing (`Nats-Expected-Last-Subject-Sequence: 0`) while
#: it holds a message, both "wrong last sequence". Measured on nats-server 2.15.2 (2026-11-09): one server answers 10071
#: (on a held `@at` follow-up, the held one kept unreplaced and the next stored once it fired; on the work queue, refused
#: while a task was queued and while it was in flight, stored once it was acknowledged), and the dev estate's three-node
#: cluster, whose streams are R3, answers 10164.
_HELD_ALREADY: Final = frozenset({10071, 10164})
_EXPECT_NOTHING: Final = {"Nats-Expected-Last-Subject-Sequence": "0"}
#: How long a JetStream API request is waited for.
_API_TIMEOUT: Final = 10.0
#: A pass running this many times its bound is taken to be hung: the replica ends itself, and Kubernetes restarts it.
_HUNG_FACTOR: Final = 2
_HUNG_EXIT: Final = 70

type TaskKind = Literal["table", "trash", "audit", "scan"]
type Area = Literal["task", "schedule", "due"]


class QueueUnavailableError(RuntimeError):
    """The bus did not take a publish, or did not answer it: nothing was queued, and the caller retries."""


class Task(BaseModel):
    """One unit of maintenance work, as its message carries it."""

    model_config = ConfigDict(frozen=True)

    kind: TaskKind
    #: The table's uuid, or the trashed object's; empty for the audit and the scan, of which there is one each.
    uuid: str = ""
    #: The table's location as its claim records it, for a ``table`` task.
    location: str = ""
    #: The trashed object's kind, for a ``trash`` task.
    trashed: Literal["table", "namespace"] = "table"

    def subject(self, area: Area) -> str:
        """Where this task's message lives: on the queue, held as a schedule, or fired from one."""
        return f"maintenance.{area}.{self.kind}" if self.kind in ("audit", "scan") else f"maintenance.{area}.{self.kind}.{self.uuid}"

    def follow_up_subject(self, area: Literal["schedule", "due"]) -> str:
        """Where this task's follow-up is held, or fired: beside its schedule, under a subject of its own."""
        return f"maintenance.{area}.{_FOLLOW_UP}.{self.kind}.{self.uuid}"


class _SubjectsState(BaseModel):
    subjects: dict[str, int] = Field(default_factory=dict)


class _SubjectsPage(BaseModel):
    """One page of a filtered stream info: the subjects it names, and how many match in all."""

    state: _SubjectsState = Field(default_factory=_SubjectsState)
    total: int = 0


def _parsed(msg: Msg) -> Task | None:
    try:
        return Task.model_validate_json(msg.data)
    except ValidationError:
        log.error("maintenance_task_unreadable", extra={"subject": msg.subject})
        return None


def _at(at: datetime) -> str:
    """An ``@at`` schedule for the instant ``at``, in UTC to the second (ADR-51)."""
    return f"@at {at.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}"


def _redelivery_delay(delivered: int) -> float:
    return min(_RETRY_FIRST * 2 ** max(delivered - 1, 0), _RETRY_CEILING)


def creds_document(*, jwt: str, seed: str) -> str:
    """The NATS credentials document nats-py reads (:class:`nats.aio.client.RawCredentials`), from the two values the store
    holds for a bus user: each sits on the line after its marker."""
    return f"-----BEGIN NATS USER JWT-----\n{jwt.strip()}\n------END NATS USER JWT------\n\n-----BEGIN USER NKEY SEED-----\n{seed.strip()}\n------END USER NKEY SEED------\n"


class TaskQueue:
    """The worker's one connection to the bus, as its own user, and the publishes the passes make on it."""

    def __init__(self, client: Client) -> None:
        self._client = client
        self._js: JetStreamContext = client.jetstream()

    @classmethod
    async def connect(cls, url: str, credentials: str, *, name: str) -> TaskQueue:
        """Connect as the user ``credentials`` names (a NATS creds document, held in memory), reconnecting for ever: a bus
        outage must leave the worker waiting on the bus, never closed for good."""
        client = await nats.connect(
            servers=[url], user_credentials=RawCredentials(credentials), name=name, max_reconnect_attempts=-1, reconnect_time_wait=2, inbox_prefix=INBOX_PREFIX
        )
        return cls(client)

    async def enqueue(self, task: Task) -> bool:
        """Put ``task`` on the queue; ``False`` when its table already has one queued or in flight, which covers it. The
        publish expects the task's subject to hold nothing, so JetStream refuses it while a task holds it; the stream's
        one-message-per-subject limit stands behind that.

        Raises:
            QueueUnavailableError: JetStream refused the publish for another reason, or did not answer it.
        """
        try:
            await self._js.publish(task.subject("task"), task.model_dump_json().encode(), stream=TASKS_STREAM, headers=_EXPECT_NOTHING)
        except APIError as exc:
            if exc.err_code in _HELD_ALREADY:
                return False
            raise QueueUnavailableError(f"the queue refused {task.subject('task')}: {exc}") from exc
        except (NatsError, TimeoutError) as exc:
            raise QueueUnavailableError(f"the bus did not take {task.subject('task')}: {exc}") from exc
        return True

    async def schedule_at(self, task: Task, at: datetime) -> None:
        """Hold ``task`` until ``at``, then put it on the queue, replacing any time it was held for before (ADR-51). A time
        already past fires at once."""
        await self._hold(task, _at(at), held=task.subject("schedule"), fires=task.subject("due"))

    async def schedule_every(self, task: Task, cadence: str) -> None:
        """Put ``task`` on the queue at every firing of ``cadence`` (``@hourly``, ``@every 30m``, a six-field cron), once
        per firing whatever the replica count, replacing any cadence it held before."""
        await self._hold(task, cadence, held=task.subject("schedule"), fires=task.subject("due"))

    async def follow_up(self, task: Task, at: datetime) -> bool:
        """Hold a commit's ``task`` until ``at``, beside its schedule and never replacing it, unless a follow-up is held
        for it already, which covers this commit: its pass reads the table after it. ``False`` then. Fired, it is queued
        once no task holds the table (:meth:`Worker.run_due`).

        Raises:
            QueueUnavailableError: JetStream refused the follow-up for another reason, or did not answer it.
        """
        held = task.follow_up_subject("schedule")
        try:
            await self._hold(task, _at(at), held=held, fires=task.follow_up_subject("due"), expect_nothing=True)
        except APIError as exc:
            if exc.err_code in _HELD_ALREADY:
                return False
            raise QueueUnavailableError(f"the queue refused {held}: {exc}") from exc
        return True

    async def _hold(self, task: Task, when: str, *, held: str, fires: str, expect_nothing: bool = False) -> None:
        """Publish ``task`` on ``held`` as a schedule that fires into ``fires`` at ``when``; with ``expect_nothing``, only
        while ``held`` holds no message.

        Raises:
            APIError: With ``expect_nothing``, JetStream refused the schedule; the caller reads its code.
            QueueUnavailableError: JetStream refused the schedule, or did not answer it.
        """
        headers = {"Nats-Schedule": when, "Nats-Schedule-Target": fires, **(_EXPECT_NOTHING if expect_nothing else {})}
        try:
            await self._js.publish(held, task.model_dump_json().encode(), stream=SCHEDULES_STREAM, headers=headers)
        except APIError as exc:
            if expect_nothing:
                raise
            raise QueueUnavailableError(f"the queue refused {held}: {exc}") from exc
        except (NatsError, TimeoutError) as exc:
            raise QueueUnavailableError(f"the bus did not take {held}: {exc}") from exc

    async def held(self, area: Literal["task", "schedule"], kind: Literal["table", "trash"]) -> set[str]:
        """The uuid of every ``kind`` task the queue holds (``task``), or that a schedule holds (``schedule``): what the audit
        reads to find a table or a trash record left with neither.

        A schedule holds its subject only until it fires: measured on nats-server 2.15.2 (2026-11-09), a fired ``@at``
        leaves no message on its ``schedule`` subject, while a cadence (``@hourly``) stays. A follow-up is no schedule of
        the table's: its subject carries a token more, which the filter's one-token wildcard does not match.

        Read a page at a time: a filtered stream info names at most 100,000 subjects (`limit`, measured on nats-server
        2.15.2) and says how many match in all (`total`).

        Raises:
            QueueUnavailableError: JetStream refused the read, or did not answer it.
        """
        stream = TASKS_STREAM if area == "task" else SCHEDULES_STREAM
        prefix = f"maintenance.{area}.{kind}."
        found: set[str] = set()
        while True:
            page = _SubjectsPage.model_validate(await self._api(f"$JS.API.STREAM.INFO.{stream}", {"subjects_filter": f"{prefix}*", "offset": len(found)}))
            found |= {subject.removeprefix(prefix) for subject in page.state.subjects}
            if not page.state.subjects or len(found) >= page.total:
                return found

    async def bind(self, stream: str, consumer: str) -> JetStreamContext.PullSubscription:
        """A pull subscription on the chart's durable ``consumer`` of ``stream``, which it binds to and never creates."""
        return await self._js.pull_subscribe_bind(durable=consumer, stream=stream)

    async def _api(self, subject: str, body: dict[str, object]) -> dict[str, object]:
        """One JetStream API request's answer.

        Raises:
            QueueUnavailableError: JetStream answered with an error, or did not answer.
        """
        try:
            reply = await self._client.request(subject, json.dumps(body).encode(), timeout=_API_TIMEOUT)
        except (NatsError, TimeoutError) as exc:
            raise QueueUnavailableError(f"the bus did not answer {subject}: {exc}") from exc
        answer = json.loads(reply.data)
        if isinstance(answer, dict) and answer.get("error"):
            raise QueueUnavailableError(f"the bus refused {subject}: {answer['error']}")
        return answer if isinstance(answer, dict) else {}

    async def close(self) -> None:
        """Drain, so the acknowledgements and publishes in flight are sent before the connection goes."""
        await self._client.drain()


class Worker:
    """What one replica runs: one task at a time off the queue, every fired schedule moved onto it, and the store's change
    events handed to ``store_events``.

    A task is acknowledged when its run returns, delivered again after a delay that grows with each failure, and ended
    on its last delivery, counted (``maintenance.tasks.ended``). While it runs the worker tells JetStream it is still
    working on it, every ``heartbeat_seconds``, so the task is never delivered to another replica while this one lives;
    a replica that dies stops telling it, and the task goes to another after the consumer's ack wait. The consumer
    allows one delivery past ``max_deliver``, which only a task whose worker died on every delivery reaches: it is ended
    and counted without running, since JetStream keeps a work-queue task that ran out of deliveries unacknowledged, and
    its table would take no other (measured on nats-server 2.15.2). A run past ``_HUNG_FACTOR`` times
    ``max_pass_seconds`` is taken to be hung, and the replica ends itself so its task goes to another, never two passes
    of one table at once. A loop outlives
    any error of the bus's: a fetch, ack or heartbeat the bus refuses or drops is logged and the loop asks again, so a
    JetStream leader change or a reconnect never leaves a running replica pulling nothing.
    """

    def __init__(
        self,
        queue: TaskQueue,
        *,
        run: Callable[[Task], Awaitable[None]],
        ready: Callable[[], bool],
        heartbeat_seconds: float,
        max_deliver: int,
        max_pass_seconds: float,
        retry_ended_seconds: float,
        store_events: Callable[[bytes], Awaitable[object]] | None = None,
    ) -> None:
        self._queue = queue
        self._retry_ended = retry_ended_seconds
        self._run = run
        self._store_events = store_events
        self._ready = ready
        self._heartbeat = heartbeat_seconds
        self._max_deliver = max_deliver
        self._max_pass = max_pass_seconds
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        """Ask every loop to end once what it is doing is done."""
        self._stopping.set()

    def stopping(self) -> bool:
        """Whether the worker has been asked to stop."""
        return self._stopping.is_set()

    async def pause(self, seconds: float) -> None:
        """Wait ``seconds``, or less if the worker is asked to stop meanwhile."""
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            return

    async def run_tasks(self) -> None:
        """Pull one task at a time from the chart's durable consumer until :meth:`stop`, and none while ``ready`` says no:
        a service whose signing key is unresolved takes no task, since what its pass emits could not be signed."""
        subscription = await self._queue.bind(TASKS_STREAM, TASK_CONSUMER)
        while not self._stopping.is_set():
            if not self._ready():
                await self.pause(_FETCH_TIMEOUT)
                continue
            for msg in await self._fetch(subscription, 1):
                await self._safely(self._handle(msg), msg)

    async def _handle(self, msg: Msg) -> None:
        task = _parsed(msg)
        if task is None:
            await msg.term()
            record_task_ended(kind="unreadable")
            return
        if (delivered := msg.metadata.num_delivered or 1) > self._max_deliver:
            await self._end(msg, task, deliveries=delivered, error="every delivery outlived its worker")
            return
        beating = asyncio.create_task(self._keep_alive(msg, task))
        try:
            await self._run(task)
        except Exception as exc:  # noqa: BLE001 — every failure of a run is the task's, retried or ended below
            await self._retry_or_end(msg, task, exc)
            return
        finally:
            beating.cancel()
        await msg.ack()
        record_task_completed(kind=task.kind)

    async def _keep_alive(self, msg: Msg, task: Task) -> None:
        started = monotonic()
        warned = False
        while True:
            await asyncio.sleep(self._heartbeat)
            try:
                await msg.in_progress()
            except NatsError as exc:
                log.warning("maintenance_heartbeat_failed", extra={"subject": msg.subject, "error": f"{type(exc).__name__}: {exc}"[:300]})
            ran = monotonic() - started
            if not warned and ran > self._max_pass:
                warned = True
                record_pass_overran(kind=task.kind)
                log.error("maintenance_task_overran", extra={"subject": msg.subject, "seconds": round(ran)})
            if ran > _HUNG_FACTOR * self._max_pass:
                # A thread cannot be stopped, and a hung one would hold the interpreter's exit: end the process outright.
                log.critical("maintenance_task_hung", extra={"subject": msg.subject, "seconds": round(ran)})
                os._exit(_HUNG_EXIT)

    async def _retry_or_end(self, msg: Msg, task: Task, exc: Exception) -> None:
        delivered = msg.metadata.num_delivered or 1
        if delivered >= self._max_deliver:
            await self._end(msg, task, deliveries=delivered, error=f"{type(exc).__name__}: {exc}"[:300])
            return
        log.warning("maintenance_task_retried", extra={"subject": msg.subject, "deliveries": delivered, "error": f"{type(exc).__name__}: {exc}"[:300]})
        await msg.nak(delay=_redelivery_delay(delivered))

    async def _end(self, msg: Msg, task: Task, *, deliveries: int, error: str) -> None:
        """End ``task`` on this delivery, counted, and hold its next try ``retry_ended_seconds`` out: the hourly audit queues
        a table or a trash record nothing holds, and would otherwise hand a task that kills its worker straight back. A bus
        that refuses the hold leaves it to that audit."""
        log.error("maintenance_task_ended", extra={"subject": msg.subject, "deliveries": deliveries, "error": error})
        record_task_ended(kind=task.kind)
        await msg.term()
        if task.kind not in _SCHEDULED_KINDS:
            return
        try:
            await self._queue.schedule_at(task, datetime.now(UTC) + timedelta(seconds=self._retry_ended))
        except QueueUnavailableError as exc:
            log.warning("maintenance_task_end_unscheduled", extra={"subject": msg.subject, "error": str(exc)[:300]})

    async def run_due(self) -> None:
        """Move each fired schedule onto the queue until :meth:`stop`. A table that already has a task queued or in flight
        is covered by it, since its pass computes the table's next time; a fired follow-up is not, since the commit it
        follows may be one that task's pass read the table before, so it waits until nothing holds the table."""
        subscription = await self._queue.bind(SCHEDULES_STREAM, DUE_CONSUMER)
        while not self._stopping.is_set():
            for msg in await self._fetch(subscription, _DUE_BATCH):
                await self._safely(self._move(msg), msg)

    async def run_store_events(self) -> None:
        """Hand each of the store's change events to ``store_events`` until :meth:`stop`. An event that is not one is
        ended; one whose handling the store or the bus refused is delivered again after a pause."""
        handle = self._store_events
        if handle is None:
            return
        subscription = await self._queue.bind(STORE_EVENTS_STREAM, STORE_EVENTS_CONSUMER)
        while not self._stopping.is_set():
            for msg in await self._fetch(subscription, _DUE_BATCH):
                await self._safely(self._on_store_event(msg, handle), msg)

    async def _on_store_event(self, msg: Msg, handle: Callable[[bytes], Awaitable[object]]) -> None:
        try:
            await handle(msg.data)
        except ValueError as exc:
            log.error("maintenance_store_event_unreadable", extra={"subject": msg.subject, "error": str(exc)[:300]})
            await msg.term()
            return
        except Exception as exc:  # noqa: BLE001 — a store or bus that failed the handling is asked again, never the loop's end
            log.warning("maintenance_store_event_retried", extra={"subject": msg.subject, "error": f"{type(exc).__name__}: {exc}"[:300]})
            await msg.nak(delay=_RETRY_FIRST)
            return
        await msg.ack()

    async def _fetch(self, subscription: JetStreamContext.PullSubscription, batch: int) -> list[Msg]:
        """The next ``batch`` messages, or none: an empty wait, or a fetch the bus refused or dropped, which is logged
        and asked again after a pause."""
        try:
            return await subscription.fetch(batch, timeout=_FETCH_TIMEOUT)
        except TimeoutError:
            return []
        except NatsError as exc:
            log.warning("maintenance_fetch_failed", extra={"error": f"{type(exc).__name__}: {exc}"[:300]})
            await asyncio.sleep(_FETCH_TIMEOUT)
            return []

    async def _safely(self, handling: Awaitable[None], msg: Msg) -> None:
        """Run one message's handling; an ack, nak or term the bus refuses or drops is logged, and the message is
        delivered again after the consumer's ack wait."""
        try:
            await handling
        except NatsError as exc:
            log.warning("maintenance_message_unsettled", extra={"subject": msg.subject, "error": f"{type(exc).__name__}: {exc}"[:300]})

    async def _move(self, msg: Msg) -> None:
        task = _parsed(msg)
        if task is None:
            await msg.term()
            return
        try:
            queued = await self._queue.enqueue(task)
        except QueueUnavailableError as exc:
            log.warning("maintenance_due_not_queued", extra={"subject": msg.subject, "error": f"{type(exc).__name__}: {exc}"[:300]})
            await msg.nak(delay=_RETRY_FIRST)
            return
        if not queued and msg.subject == task.follow_up_subject("due"):
            await msg.nak(delay=_FOLLOW_UP_RETRY)
            return
        await msg.ack()
