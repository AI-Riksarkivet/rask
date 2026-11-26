"""What one replica does with maintenance's work queue (ADR 0198 §2): it pulls one task at a time and holds it under a
lease it keeps renewing, with a claim on the task's table, moves each fired schedule onto the queue, and hands the
store's change events and the commit announcements to the service's handling of each.

The connection, and every word said to the bus on it, is :class:`maintenance.services.queue.TaskQueue`'s; the worker is
a pull worker on the chart's durable consumers (:mod:`maintenance.services.queue_wire` names them). This module holds
the rules a delivery is settled by: when a task is acknowledged, delivered again or ended, and when a replica that
cannot vouch for its task ends itself.
"""

import asyncio
import json
import logging
import random
import secrets
from collections.abc import Awaitable, Callable, Collection, Mapping
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Final, Literal, NoReturn

from nats.aio.msg import Msg
from nats.errors import Error as NatsError
from opentelemetry import propagate, trace
from opentelemetry.trace import Link, SpanKind, StatusCode
from pydantic import BaseModel, ConfigDict, ValidationError

from lakehouse.process.supervision import FLUSH_SECONDS, EndProcess, end_process, is_panic
from maintenance.core.metrics import (
    ArrivalOutcome,
    record_arrival_settled,
    record_pass_overran,
    record_schedule_not_queued,
    record_store_event_settled,
    record_task_completed,
    record_task_duration,
    record_task_ended,
    record_task_redelivered,
    record_task_retried,
    record_task_wait,
)
from maintenance.services.queue import API_TIMEOUT, QueueUnavailableError, TaskQueue
from maintenance.services.queue_wire import (
    ARRIVALS_CONSUMER,
    ARRIVALS_STREAM,
    DUE_CONSUMER,
    SCHEDULES_STREAM,
    STORE_EVENTS_CONSUMER,
    STORE_EVENTS_STREAM,
    TASK_CONSUMER,
    TASKS_STREAM,
    Task,
)


log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

#: How long a worker waits on an empty queue before asking again.
_FETCH_TIMEOUT: Final = 5.0
#: The kinds whose task has a schedule of its own, which the hourly audit reads to find one nothing holds.
_SCHEDULED_KINDS: Final = frozenset({"table", "trash", "orphans"})
#: The kinds that repair the estate from a drift report, and the one claim they share.
_RECONCILING_KINDS: Final = frozenset({"audit", "scan"})
_RECONCILE_CLAIM: Final = "reconcile"
#: The bound on the redelivery delay after a failed delivery, doubling up to a ceiling: a store that is down for a while is
#: asked again at a pace it can bear. The delay itself is drawn beneath the bound (:func:`redelivery_delay`).
_RETRY_FIRST: Final = 15.0
_RETRY_CEILING: Final = 900.0
#: How long a fired follow-up waits for the task holding its table before it asks again.
_FOLLOW_UP_RETRY: Final = 60.0
#: A pass running this many times its bound is taken to be hung: the replica ends itself, and Kubernetes restarts it.
_HUNG_FACTOR: Final = 2
#: How long before its lease runs out a worker the bus does not answer gives up its task: the time it has to be gone, its
#: telemetry flushed within all of it but `_EXIT_RESERVE` (`Worker._end_process`), then `os._exit`, which is immediate.
_VOUCH_MARGIN: Final = 1.0
#: What a worker that ends itself keeps of its hold on its task after its flush: the process is gone that long before
#: JetStream can hand the task on or another pass take its table, whatever the Collector answers.
_EXIT_RESERVE: Final = 0.5
#: How soon a heartbeat or a settle the bus did not answer is asked again.
_VOUCH_RETRY: Final = 1.0


def _carried_by(headers: Mapping[str, str]) -> dict[str, str]:
    """The trace context the message with ``headers`` carried, to hand on unchanged when a fired schedule is moved onto
    the queue (a fired schedule keeps the headers its held message was published with, measured on nats-server 2.15.2,
    2026-11-13)."""
    return {field: headers[field] for field in propagate.get_global_textmap().fields if field in headers}


def _queued_under(msg: Msg) -> list[Link]:
    """A link to the span ``msg``'s task was queued under, none when its headers carry no context. A link, never a parent:
    a pass holds its table's next pass from inside its own span, and a parent would chain every pass of a table, for the
    table's life, into one trace."""
    queued = trace.get_current_span(propagate.extract(msg.headers or {})).get_span_context()
    return [Link(queued)] if queued.is_valid else []


def _record_delivery(task: Task, msg: Msg) -> None:
    """Count ``msg`` as a delivery of ``task``: its first records how long the task waited on the queue, whether a worker
    runs it or hands it on as it stops; any later one is a redelivery.

    The delivery carries when the stream stored the message, by the bus's clock (measured on nats-server 2.15.2, nats-py
    2.16.0: a task queued behind a held one reads the time it was held). This replica's clock reads the delivery, so a
    skew between the two never reads as a negative wait.
    """
    if (msg.metadata.num_delivered or 1) > 1:
        record_task_redelivered(kind=task.kind)
        return
    record_task_wait(kind=task.kind, seconds=max((datetime.now(UTC) - msg.metadata.timestamp).total_seconds(), 0.0))


def _parsed(msg: Msg) -> Task | None:
    try:
        return Task.model_validate_json(msg.data)
    except ValidationError:
        log.error("maintenance_task_unreadable", extra={"subject": msg.subject})
        return None


#: The doublings past which the bound is the ceiling whatever the delivery count: 15 s doubled six times passes 900 s.
_RETRY_DOUBLINGS: Final = 6


def redelivery_delay(delivered: int) -> float:
    """How long the ``delivered``-th failed delivery waits before it is asked again: drawn at random up to a bound that
    doubles with each delivery to :data:`_RETRY_CEILING` (full jitter), so tasks that failed together, on one store
    outage, come back spread over the bound rather than in the same second, delivery after delivery."""
    bound = min(_RETRY_FIRST * 2 ** min(max(delivered - 1, 0), _RETRY_DOUBLINGS), _RETRY_CEILING)
    return random.uniform(0.0, bound)  # noqa: S311 — spreads retries, guards nothing


def _claim_key(task: Task) -> str:
    """The key of the claim a pass of ``task`` takes: its table (or trash record), as its subject names it, and one key for
    the audit and the scan, which both repair the estate from a drift report of their own, so never at once. A table's
    orphan check takes its table's claim, so it never runs beside the table's pass, whose cleanup it would see half done,
    nor beside another check of the table."""
    if task.kind in _RECONCILING_KINDS:
        return _RECONCILE_CLAIM
    if task.kind == "orphans":
        return Task(kind="table", uuid=task.uuid).subject("task").removeprefix("maintenance.task.")
    return task.subject("task").removeprefix("maintenance.task.")


def _nak(delay: float) -> bytes:
    """A nak asking JetStream to deliver the task again after ``delay`` seconds, as nats-py's ``Msg.nak`` spells it."""
    return Msg.Ack.Nak + b" " + json.dumps({"delay": int(delay * 1_000_000_000)}).encode()


class _Lease(BaseModel):
    """Until when a running task is this worker's, on its monotonic clock: the ack wait from the delivery's arrival, then
    from when the last answered heartbeat and claim renewal were sent. The heartbeat renews it, and every word about the
    delivery is bounded by it."""

    until: float
    #: The revision of the claim this pass holds on its table; 0 until it holds one.
    claim: int = 0
    #: What this pass writes as its claim, so a claim or renewal whose answer was lost is known as its own when asked again.
    token: str = ""


#: How a hint lane's rule settles a message whose handling did not acknowledge it.
type _Unhandled = Literal["retried", "unreadable", "ended"]


class _Said(BaseModel):
    """What one hint lane calls its settlings in its log lines."""

    model_config = ConfigDict(frozen=True)

    ended: str
    unreadable: str
    retried: str


_STORE_EVENT_SAID: Final = _Said(
    ended="maintenance_store_event_ended",
    unreadable="maintenance_store_event_unreadable",
    retried="maintenance_store_event_retried",
)
_ARRIVAL_SAID: Final = _Said(
    ended="maintenance_arrival_ended",
    unreadable="maintenance_arrival_unreadable",
    retried="maintenance_arrival_retried",
)


class _HintLane[OutcomeT]:
    """One of the worker's hint lanes: the consumer it pulls, the service's handling of one message, which answers the
    outcome an acknowledged one is counted under, how a settling is counted, and what its log lines are called."""

    def __init__(
        self,
        *,
        stream: str,
        consumer: str,
        said: _Said,
        handle: Callable[[bytes], Awaitable[OutcomeT]],
        settled: Callable[[OutcomeT | _Unhandled], None],
    ) -> None:
        self.stream = stream
        self.consumer = consumer
        self.said = said
        self.handle = handle
        self.settled = settled


class Worker:
    """What one replica runs: one task at a time off the queue, every fired schedule moved onto it, the store's change
    events handed to ``store_events``, and the commit announcements handed to ``arrivals``.

    A task is acknowledged when its run returns, delivered again after a delay that grows with each failure, and ended
    on its last delivery, counted (``maintenance.tasks.ended``). While it runs the worker tells JetStream it is still
    working on it, every ``heartbeat_seconds``, and each answer restarts the consumer's ack wait (``ack_wait_seconds``);
    a replica that dies stops telling it, and the task goes to another once the ack wait runs out. A replica the bus stops
    answering cannot tell whether its task has gone to another, so it ends itself before the ack wait the last answer
    restarted runs out, and does not leave an ack, nak or term the bus did not answer to be sent after then (:meth:`_tell`),
    a stopping replica whose drain ends with a pass still running ends there
    (:meth:`drain`), and a task is only ever run off the pull it answered (:meth:`run_tasks`): never two passes of one
    table at once, a bus cut, a reconnect and a shutdown included. A pass also holds a claim on its table that outlives
    its lease (:meth:`_claim`), which another pass of the table waits out, so even a settle the network held past the
    lease and JetStream applied late starts no second pass beside it; a task that finds the claim held on every
    delivery is given up uncounted (:meth:`_give_back`). The consumer
    allows one delivery past ``max_deliver``, which only a task whose worker died on every delivery reaches: it is ended
    and counted without running, since JetStream keeps a work-queue task that ran out of deliveries unacknowledged, and
    its table would take no other (measured on nats-server 2.15.2). A run past ``_HUNG_FACTOR`` times
    its bound (its kind's own in ``max_seconds_by_kind``, else ``max_pass_seconds``) is taken to be hung, and the replica ends itself so its task goes to another, never two passes
    of one table at once. A loop outlives
    any error of the bus's: a bind, fetch, ack or heartbeat the bus refuses or drops is logged and the loop asks again, so
    a JetStream leader change or a reconnect never leaves a running replica pulling nothing. Any other end of a loop is a
    fault, which the process's supervisor answers by ending the process (`lakehouse.process.supervision`).
    """

    def __init__(
        self,
        queue: TaskQueue,
        *,
        run: Callable[[Task], Awaitable[None]],
        ready: Callable[[], bool],
        heartbeat_seconds: float,
        ack_wait_seconds: float,
        max_deliver: int,
        max_pass_seconds: float,
        retry_ended_seconds: float,
        store_events: Callable[[bytes], Awaitable[int]] | None = None,
        arrivals: Callable[[bytes], Awaitable[ArrivalOutcome]] | None = None,
        end: EndProcess = end_process,
        max_seconds_by_kind: Mapping[str, float] | None = None,
    ) -> None:
        self._queue = queue
        self._exit = end
        self._ending = False
        self._retry_ended = retry_ended_seconds
        self._run = run
        self._store_events = store_events
        self._arrivals = arrivals
        self._ready = ready
        self._heartbeat = heartbeat_seconds
        self._ack_wait = ack_wait_seconds
        if ack_wait_seconds - heartbeat_seconds <= _VOUCH_MARGIN:
            raise ValueError(f"a heartbeat every {heartbeat_seconds} s leaves no time to vouch for a task inside an ack wait of {ack_wait_seconds} s")
        #: The lease of the pass running, none between passes.
        self._pass_lease: _Lease | None = None
        self._max_deliver = max_deliver
        self._max_pass = max_pass_seconds
        self._max_by_kind = dict(max_seconds_by_kind or {})
        self._stopping = asyncio.Event()

    def _end_process(self, event: str, *, held_until: float, **extra: object) -> NoReturn:
        """End the process (``end``), its telemetry flushed only until `_EXIT_RESERVE` before ``held_until``, when this
        worker's hold on its task runs out: JetStream hands the task to another replica then, or another pass takes its
        table, while the pass, a thread nothing stops, runs on until the process is gone. It notes first that it is ending,
        so a keep-alive the end unwinds is not taken for one that ended unexpectedly (:meth:`_keep_alive`)."""
        self._ending = True
        self._exit(event, flush_seconds=max(0.0, min(FLUSH_SECONDS, held_until - monotonic() - _EXIT_RESERVE)), **extra)

    def stop(self) -> None:
        """Ask every loop to end once what it is doing is done."""
        self._stopping.set()

    def stopping(self) -> bool:
        """Whether the worker has been asked to stop."""
        return self._stopping.is_set()

    async def drain(self, loops: Collection[asyncio.Task[None]], seconds: float) -> None:
        """Stop, give ``loops`` ``seconds`` to finish what they are doing, and cancel what has not.

        A pass still running then is a thread nothing can stop, and its heartbeat ends with the drain, so JetStream would
        hand its task to another replica while it ran on: the process ends instead, and the task's next delivery runs
        alone, whatever the pod's grace.
        """
        self.stop()
        _, unfinished = await asyncio.wait(loops, timeout=seconds)
        if self._pass_lease is not None:
            self._end_process("maintenance_drain_outrun", held_until=self._pass_lease.until, seconds=seconds)
        for loop in unfinished:
            loop.cancel()
        # Waited for, so nothing of a loop still runs on the connection its caller closes next.
        await asyncio.gather(*unfinished, return_exceptions=True)

    async def pause(self, seconds: float) -> None:
        """Wait ``seconds``, or less if the worker is asked to stop meanwhile."""
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except TimeoutError:
            return

    async def run_tasks(self) -> None:
        """Pull one task at a time from the chart's durable consumer until :meth:`stop`, and none while ``ready`` says no:
        a service whose signing key is unresolved takes no task, since what its pass emits could not be signed.

        Each pull has a subscription of its own, read until the bus ends the pull (:meth:`TaskQueue.pulled`), then
        ended. A pull a reconnect held back can reach the server after the worker gave up on it, and be answered while a
        pass runs; on a subscription that outlived it, that task would wait in the client and be run once the pass ended,
        its ack wait long run out and the task perhaps another replica's by then. Sent to an inbox no one reads, it is
        handed on after its ack wait instead, so a task is only ever run off the pull it answered, and arrives as its ack
        wait starts: its lease counts from then.
        """
        while not self._stopping.is_set():
            if not self._ready():
                await self.pause(_FETCH_TIMEOUT)
                continue
            msg = await self._next(TASKS_STREAM, TASK_CONSUMER)
            if msg is None:
                continue
            received = monotonic()
            await self._safely(self._hand_on(msg, received) if self._hands_on(msg) else self._handle(msg, received), msg)

    async def _next(self, stream: str, consumer: str) -> Msg | None:
        """The message the next pull on ``consumer`` is answered with, or ``None``: an empty wait, or a pull the bus
        refused or dropped, which is logged and asked again after a pause. A reconnect is the bus's ordinary weather,
        which a loop waits out rather than ending on."""
        try:
            return await self._queue.pulled(stream, consumer, wait=_FETCH_TIMEOUT, answered_within=API_TIMEOUT)
        except NatsError as exc:
            log.warning("maintenance_fetch_failed", extra={"consumer": consumer, "error": f"{type(exc).__name__}: {exc}"[:300]})
            await self.pause(_FETCH_TIMEOUT)
            return None

    def _hands_on(self, msg: Msg) -> bool:
        """Whether ``msg`` arrived as the worker stopped with a delivery to spare. On the last delivery this worker allows,
        or the one past it, a hand-back would spend the consumer's last delivery, after which JetStream delivers the task
        no more and keeps it on the queue, where its table takes no other: that delivery is handled as any other is."""
        return self._stopping.is_set() and (msg.metadata.num_delivered or 1) < self._max_deliver

    async def _hand_on(self, msg: Msg, received: float) -> None:
        """Give back, undelayed, a task a pull answered as the worker stopped: started now, its pass would run under the
        drain and end this replica when the drain did, so another replica takes it instead."""
        if (task := _parsed(msg)) is not None:
            _record_delivery(task, msg)
        await self._tell(msg, _nak(0), _Lease(until=received + self._ack_wait))

    async def _handle(self, msg: Msg, received: float) -> None:
        lease = _Lease(until=received + self._ack_wait)
        task = _parsed(msg)
        if task is None:
            await self._tell(msg, Msg.Ack.Term, lease)
            record_task_ended(kind="unreadable")
            return
        _record_delivery(task, msg)
        if (delivered := msg.metadata.num_delivered or 1) > self._max_deliver:
            await self._end(msg, task, lease, deliveries=delivered, error="every delivery outlived its worker")
            return
        beating = asyncio.create_task(self._keep_alive(msg, task, lease))
        try:
            if not await self._claim(task, lease):
                await self._give_back(msg, task, lease, delivered=delivered)
                return
            self._pass_lease = lease
            claimed = monotonic()
            try:
                await self._traced(msg, task, final=delivered >= self._max_deliver)
            except BaseException as exc:
                # Every failure of a run is the task's, retried or ended below: an Exception, and a native library's panic,
                # which pyo3 raises as a BaseException (owner, 2026-11-12: "Pass failed, go on"). Any other BaseException is
                # a request to stop, which ends the loop, and the loop's end ends the process.
                if not (isinstance(exc, Exception) or is_panic(exc)):
                    raise
                await self._retry_or_end(msg, task, exc, lease=lease)
                return
            await self._tell(msg, Msg.Ack.Ack, lease)
            record_task_duration(kind=task.kind, seconds=monotonic() - claimed)
        finally:
            self._pass_lease = None
            # Ended before the release, so no renewal still in flight moves the claim's revision after the release read it.
            beating.cancel()
            await asyncio.gather(beating, return_exceptions=True)
            if lease.claim:
                await self._queue.release(_claim_key(task), lease.token, lease.claim)
        record_task_completed(kind=task.kind)

    async def _traced(self, msg: Msg, task: Task, *, final: bool) -> None:
        """Run ``task`` as a span of a trace of its own, linked to the span it was queued under. The span is an error only
        when the run fails on the delivery that ends the task (``final``): a failure the queue delivers again is not the
        task's outcome, and the retry's own span follows it."""
        attributes = {
            "messaging.system": "nats",
            "messaging.operation.type": "process",
            "messaging.destination.name": TASKS_STREAM,
            "maintenance.task.kind": task.kind,
            "maintenance.task.uuid": task.uuid,
            "maintenance.task.delivery": msg.metadata.num_delivered or 1,
        }
        name = f"process maintenance.task.{task.kind}"
        with tracer.start_as_current_span(
            name, kind=SpanKind.CONSUMER, links=_queued_under(msg), attributes=attributes, record_exception=False, set_status_on_exception=False
        ) as span:
            try:
                await self._run(task)
            except BaseException as exc:
                if final and (isinstance(exc, Exception) or is_panic(exc)):
                    span.set_status(StatusCode.ERROR, type(exc).__name__)
                raise

    async def _claim(self, task: Task, lease: _Lease) -> bool:
        """Claim ``task``'s table for this pass, waiting up to one ack wait while another's claim stands: a worker that died
        mid-pass leaves its claim one ack wait past its last renewal, and an ack the network held past a stale worker's
        lease can free a task's place under a live pass, whose claim this pass then waits out (ADR 0198). False when it
        still stands, and the task is given back to come again. A claim the bus does not answer is asked again: a bus that
        answers nothing is the heartbeat's to judge, and one that answers the heartbeat but not the claim holds the task
        until the claim is answered or the pass's hung bound ends the replica. A bus that holds no claims bucket, at the
        worker's start or since, answers for good: `ClaimsBucketMissingError` ends the loop, and with it the process.

        Raises:
            ClaimsBucketMissingError: The bus holds no claims bucket.
        """
        key = _claim_key(task)
        lease.token = secrets.token_hex(16)
        until = monotonic() + self._ack_wait
        while True:
            try:
                revision = await self._queue.claim(key, lease.token, within=self._heartbeat)
            except QueueUnavailableError as exc:
                log.warning("maintenance_claim_unanswered", extra={"key": key, "error": str(exc)[:300]})
            else:
                if revision is not None:
                    lease.claim = revision
                    return True
                if monotonic() >= until:
                    log.info("maintenance_claim_held", extra={"key": key})
                    return False
            await asyncio.sleep(_VOUCH_RETRY)

    async def _give_back(self, msg: Msg, task: Task, lease: _Lease, *, delivered: int) -> None:
        """Give back a task whose table another pass holds, to come again after its retry delay; on its last delivery it is
        given up instead, uncounted, since nothing failed: the pass holding the claim is what the task asked for, or, for
        the audit and the scan, the other of the two, whose next firing brings this one back. A table's or a trash
        record's next try is held a follow-up's wait out, so the hourly audit is not what finds it."""
        if delivered < self._max_deliver:
            await self._tell(msg, _nak(redelivery_delay(delivered)), lease)
            return
        log.warning("maintenance_task_given_up_to_its_claim", extra={"subject": msg.subject, "deliveries": delivered})
        await self._tell(msg, Msg.Ack.Term, lease)
        if task.kind not in _SCHEDULED_KINDS:
            return
        try:
            await self._queue.schedule_at(task, datetime.now(UTC) + timedelta(seconds=_FOLLOW_UP_RETRY))
        except QueueUnavailableError as exc:
            log.warning("maintenance_task_end_unscheduled", extra={"subject": msg.subject, "error": str(exc)[:300]})

    async def _keep_alive(self, msg: Msg, task: Task, lease: _Lease) -> None:
        """Renew ``lease`` every ``heartbeat_seconds`` from the delivery's arrival, the first at once, and end the process
        once the pass has run past twice its bound: its kind's own (``max_seconds_by_kind``), else ``max_pass_seconds``.

        A keep-alive that ends by an error nothing expected ends the process too: the pass would run on unvouched for,
        and JetStream would hand its task to another replica beside it.
        """
        try:
            await self._beat(msg, task, lease)
        except Exception as exc:  # noqa: BLE001 — any end of the heartbeat but its cancellation leaves the pass unvouched for
            if self._ending:
                raise
            self._end_process("maintenance_heartbeat_ended", held_until=lease.until, subject=msg.subject, error=f"{type(exc).__name__}: {exc}"[:300])

    async def _beat(self, msg: Msg, task: Task, lease: _Lease) -> NoReturn:
        started = monotonic()
        bound = self._max_by_kind.get(task.kind, self._max_pass)
        warned = False
        while True:
            sent = await self._tell(msg, Msg.Ack.Progress, lease)
            if lease.claim:
                sent = min(sent, await self._renew(task, lease))
            lease.until = sent + self._ack_wait
            await asyncio.sleep(self._heartbeat)
            ran = monotonic() - started
            if not warned and ran > bound:
                warned = True
                record_pass_overran(kind=task.kind)
                log.error("maintenance_task_overran", extra={"subject": msg.subject, "seconds": round(ran)})
            if ran > _HUNG_FACTOR * bound:
                self._end_process("maintenance_task_hung", held_until=lease.until, subject=msg.subject, seconds=round(ran))

    async def _tell(self, msg: Msg, said: bytes, lease: _Lease) -> float:
        """Say ``said`` about ``msg``'s delivery (:meth:`TaskQueue.told`), asking again until JetStream answers, and return
        when the answered one was sent.

        One that cannot be answered before ``lease`` runs out ends the process. A heartbeat unanswered by then would leave
        the pass running beside the replica JetStream hands the task to; an ack, nak or term the client holds through a bus
        cut would be applied once the bus is back, to a delivery JetStream has handed on, freeing the table's task under
        another replica's pass. The pass, a thread nothing can stop, ends with the process, and what the client still holds
        is never sent. What it already wrote to its socket is beyond it, and a settle the network held past the lease can
        still be applied late; the pass's claim on its table outlives the lease, so no second pass starts under it on the
        place such a settle frees (:meth:`_claim`). A reconnect the bus answers inside the lease costs nothing.
        """
        while (left := lease.until - monotonic()) > _VOUCH_MARGIN:
            sent = monotonic()
            if await self._queue.told(msg, said, within=min(self._heartbeat, left - _VOUCH_MARGIN)):
                return sent
            log.warning(
                "maintenance_bus_unanswered",
                extra={"subject": msg.subject, "said": said.split(b" ")[0].decode(), "seconds_left": round(lease.until - monotonic(), 1)},
            )
            await asyncio.sleep(min(_VOUCH_RETRY, max(lease.until - monotonic() - _VOUCH_MARGIN, 0.0)))
        return self._end_process("maintenance_task_lost", held_until=lease.until, subject=msg.subject)

    async def _renew(self, task: Task, lease: _Lease) -> float:
        """Renew this pass's claim on its table, asking again until the bus answers, and return when the answered renewal
        was sent. A claim no longer this worker's (it aged out, and another may hold it) ends the process at once, as does
        one the bus cannot answer before ``lease`` runs out: another pass of the table may start once it is gone."""
        key = _claim_key(task)
        while (left := lease.until - monotonic()) > _VOUCH_MARGIN:
            sent = monotonic()
            try:
                revision = await self._queue.renew(key, lease.token, lease.claim, within=min(self._heartbeat, left - _VOUCH_MARGIN))
            except QueueUnavailableError as exc:
                log.warning("maintenance_claim_unanswered", extra={"key": key, "error": str(exc)[:300]})
                await asyncio.sleep(min(_VOUCH_RETRY, max(lease.until - monotonic() - _VOUCH_MARGIN, 0.0)))
                continue
            if revision is None:
                # The claim is gone, so another pass may hold the table already: nothing of the hold is left to flush in.
                self._end_process("maintenance_claim_lost", held_until=monotonic(), key=key)
            lease.claim = revision
            return sent
        return self._end_process("maintenance_task_lost", held_until=lease.until, key=key)

    async def _retry_or_end(self, msg: Msg, task: Task, exc: BaseException, *, lease: _Lease) -> None:
        """Hand ``task``'s failed run back after its back-off, or end it on its last delivery, logged with ``exc``'s
        traceback either way: a native panic's traceback is the only account of where it failed."""
        delivered = msg.metadata.num_delivered or 1
        error = f"{type(exc).__name__}: {exc}"[:300]
        if delivered >= self._max_deliver:
            await self._end(msg, task, lease, deliveries=delivered, error=error, cause=exc)
            return
        log.warning("maintenance_task_retried", exc_info=exc, extra={"subject": msg.subject, "deliveries": delivered, "error": error})
        record_task_retried(kind=task.kind)
        await self._tell(msg, _nak(redelivery_delay(delivered)), lease)

    async def _end(self, msg: Msg, task: Task, lease: _Lease, *, deliveries: int, error: str, cause: BaseException | None = None) -> None:
        """End ``task`` on this delivery, counted, and hold its next try ``retry_ended_seconds`` out: the hourly audit queues
        a table or a trash record nothing holds, and would otherwise hand a task that kills its worker straight back. A bus
        that refuses the hold leaves it to that audit. ``cause``, the run's failure when one ended it, is logged with its
        traceback."""
        log.error("maintenance_task_ended", exc_info=cause, extra={"subject": msg.subject, "deliveries": deliveries, "error": error})
        record_task_ended(kind=task.kind)
        await self._tell(msg, Msg.Ack.Term, lease)
        if task.kind not in _SCHEDULED_KINDS:
            return
        try:
            await self._queue.schedule_at(task.ended(), datetime.now(UTC) + timedelta(seconds=self._retry_ended))
        except QueueUnavailableError as exc:
            log.warning("maintenance_task_end_unscheduled", extra={"subject": msg.subject, "error": str(exc)[:300]})

    async def run_due(self) -> None:
        """Move each fired schedule onto the queue until :meth:`stop`. A table that already has a task queued or in flight
        is covered by it, since its pass computes the table's next time; a fired follow-up is not, since the commit it
        follows may be one that task's pass read the table before, so it waits until nothing holds the table."""
        while not self._stopping.is_set():
            msg = await self._next(SCHEDULES_STREAM, DUE_CONSUMER)
            if msg is not None:
                await self._safely(self._move(msg), msg)

    async def run_store_events(self) -> None:
        """Hand each of the store's change events to ``store_events`` until :meth:`stop`, which answers how many tables'
        passes it held; each event's settling is counted by outcome (``maintenance.store_events.settled``) and follows
        the hint lanes' one rule (:meth:`_run_hints`).

        One event a pull: an event the bus handed over while another was handled would wait out its ack wait unread, and
        go to another replica while this one still held it.
        """
        handle = self._store_events
        if handle is None:
            await self._stopping.wait()
            return

        async def held(body: bytes) -> Literal["held", "ignored"]:
            return "held" if await handle(body) > 0 else "ignored"

        await self._run_hints(
            _HintLane(
                stream=STORE_EVENTS_STREAM,
                consumer=STORE_EVENTS_CONSUMER,
                said=_STORE_EVENT_SAID,
                handle=held,
                settled=lambda outcome: record_store_event_settled(outcome=outcome),
            )
        )

    async def run_arrivals(self) -> None:
        """Hand each commit announcement to ``arrivals`` until :meth:`stop`, which answers how it is acknowledged; each
        one's settling is counted by outcome (``maintenance.arrivals.settled``) and follows the hint lanes' one rule
        (:meth:`_run_hints`)."""
        handle = self._arrivals
        if handle is None:
            await self._stopping.wait()
            return
        await self._run_hints(
            _HintLane(
                stream=ARRIVALS_STREAM,
                consumer=ARRIVALS_CONSUMER,
                said=_ARRIVAL_SAID,
                handle=handle,
                settled=lambda outcome: record_arrival_settled(outcome=outcome),
            )
        )

    async def _run_hints[OutcomeT](self, lane: _HintLane[OutcomeT]) -> None:
        """Pull ``lane``'s consumer until :meth:`stop`, each message handed to the lane's handling and settled by one rule.

        A hint makes a table's pass due sooner and the table's recheck backs it up, so one that cannot be handled is
        ended, never kept. The handling raises ``ValueError`` for a body that is no hint of the lane's, which is ended at
        once, and ``OSError`` or :class:`QueueUnavailableError` for one whose handling the store or the bus refused,
        which is delivered again after a delay that grows with each delivery, and ended on its ``max_deliver``-th. The
        consumer allows one delivery more, on which a hint whose handling outlived its worker every time is ended
        without handling. Any other error is a fault of the handling's own: it ends the loop, and the loop's end the
        process, and the hint is delivered again after the consumer's ack wait.
        """
        while not self._stopping.is_set():
            msg = await self._next(lane.stream, lane.consumer)
            if msg is not None:
                await self._safely(self._on_hint(msg, lane), msg)

    async def _on_hint[OutcomeT](self, msg: Msg, lane: _HintLane[OutcomeT]) -> None:
        delivered = msg.metadata.num_delivered or 1
        if delivered > self._max_deliver:
            log.error(lane.said.ended, extra={"subject": msg.subject, "deliveries": delivered, "error": "every delivery outlived its worker"})
            await msg.term()
            lane.settled("ended")
            return
        try:
            outcome = await lane.handle(msg.data)
        except ValueError as exc:
            log.error(lane.said.unreadable, extra={"subject": msg.subject, "error": str(exc)[:300]})
            await msg.term()
            lane.settled("unreadable")
            return
        except (OSError, QueueUnavailableError) as exc:
            await self._retry_or_end_hint(msg, lane, exc, delivered=delivered)
            return
        await msg.ack()
        lane.settled(outcome)

    async def _retry_or_end_hint[OutcomeT](self, msg: Msg, lane: _HintLane[OutcomeT], exc: OSError | QueueUnavailableError, *, delivered: int) -> None:
        error = f"{type(exc).__name__}: {exc}"[:300]
        if delivered >= self._max_deliver:
            log.error(lane.said.ended, extra={"subject": msg.subject, "deliveries": delivered, "error": error})
            await msg.term()
            lane.settled("ended")
            return
        log.warning(lane.said.retried, extra={"subject": msg.subject, "deliveries": delivered, "error": error})
        await msg.nak(delay=redelivery_delay(delivered))
        lane.settled("retried")

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
            queued = await self._queue.enqueue(task, carried=_carried_by(msg.headers or {}))
        except QueueUnavailableError as exc:
            log.warning("maintenance_due_not_queued", extra={"subject": msg.subject, "error": f"{type(exc).__name__}: {exc}"[:300]})
            record_schedule_not_queued()
            await msg.nak(delay=redelivery_delay(msg.metadata.num_delivered or 1))
            return
        if not queued and msg.subject == task.follow_up_subject("due"):
            await msg.nak(delay=_FOLLOW_UP_RETRY)
            return
        await msg.ack()
