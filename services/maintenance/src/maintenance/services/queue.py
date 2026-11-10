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
import secrets
from collections.abc import Awaitable, Callable, Collection
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Final, Literal, NoReturn

import nats
from nats.aio.client import Client, RawCredentials
from nats.aio.msg import Msg
from nats.errors import Error as NatsError
from nats.js.errors import APIError, KeyDeletedError, KeyNotFoundError, KeyWrongLastSequenceError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from maintenance.core.metrics import record_pass_overran, record_task_completed, record_task_ended


if TYPE_CHECKING:
    from nats.js import JetStreamContext
    from nats.js.kv import KeyValue


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
#: The key-value bucket holding the claim a pass takes on its table (the chart's stream Job declares it, its max age the
#: consumer's ack wait).
CLAIMS_BUCKET: Final = "MAINTENANCE_CLAIMS"
#: How long a worker waits on an empty queue before asking again, and how many fired schedules it moves at once.
_FETCH_TIMEOUT: Final = 5.0
_DUE_BATCH: Final = 10
#: The kinds whose task has a schedule of its own, which the hourly audit reads to find one nothing holds.
_SCHEDULED_KINDS: Final = frozenset({"table", "trash"})
#: The kinds that repair the estate from a drift report, and the one claim they share.
_RECONCILING_KINDS: Final = frozenset({"audit", "scan"})
_RECONCILE_CLAIM: Final = "reconcile"
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
#: while a task was queued and while it was in flight, stored once it was acknowledged). A stream copied across a cluster
#: (R3 on three nodes, as on the dev estate) answers 10071 too, and 10164 while another put for the subject is still being
#: agreed, as when a burst of a table's store events puts its follow-ups at once: 40 of 40 racing puts.
_HELD_ALREADY: Final = frozenset({10071, 10164})
_EXPECT_NOTHING: Final = {"Nats-Expected-Last-Subject-Sequence": "0"}
#: How long a JetStream API request is waited for.
_API_TIMEOUT: Final = 10.0
#: A pass running this many times its bound is taken to be hung: the replica ends itself, and Kubernetes restarts it.
_HUNG_FACTOR: Final = 2
_HUNG_EXIT: Final = 70
#: How long before its lease runs out a worker the bus does not answer gives up its task: the reserve between deciding and
#: the process being gone, which `os._exit` makes immediate.
_VOUCH_MARGIN: Final = 1.0
#: How soon a heartbeat or a settle the bus did not answer is asked again.
_VOUCH_RETRY: Final = 1.0
#: How often the client asks the server it is connected to whether it is still there, and how many unanswered asks
#: drop the connection: a node that dies without closing it is left within 30 s, inside the 60 s ack wait, so the client
#: reconnects to a live node and its heartbeats are answered again (nats-py's default, 120 s x 2, outlasts the lease).
_PING_INTERVAL: Final = 10.0
_MAX_OUTSTANDING_PINGS: Final = 2
#: What nats-server answers a connect whose credential it refuses (revoked, unknown, or past its expiry); nats-py hands it
#: to the error callback while it reconnects. A session the server ends for its credential closes the client instead.
_REFUSED: Final = "authorization violation"

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


async def _standing(claims: KeyValue, key: str, token: str) -> int | None:
    """The revision of the claim standing on ``key`` when it holds ``token``; None when it is another's, or none stands."""
    try:
        entry = await claims.get(key)
    except (KeyNotFoundError, KeyDeletedError):
        return None
    return entry.revision if entry.value == token.encode() else None


def _claim_key(task: Task) -> str:
    """The key of the claim a pass of ``task`` takes: its table (or trash record), as its subject names it, and one key for
    the audit and the scan, which both repair the estate from a drift report of their own, so never at once."""
    if task.kind in _RECONCILING_KINDS:
        return _RECONCILE_CLAIM
    return task.subject("task").removeprefix("maintenance.task.")


def _nak(delay: float) -> bytes:
    """A nak asking JetStream to deliver the task again after ``delay`` seconds, as nats-py's ``Msg.nak`` spells it."""
    return Msg.Ack.Nak + b" " + json.dumps({"delay": int(delay * 1_000_000_000)}).encode()


def end_replica(event: str, **extra: object) -> NoReturn:
    """End this replica's process at once, logging ``event``: a pass is a thread nothing can stop, and a hung one would
    hold the interpreter's exit. Its task is left to the consumer's ack wait. The stdout handler writes each record
    through as it is emitted (`lakehouse.app.setup_logging`), so the line outlives the process."""
    log.critical(event, extra=extra)
    os._exit(_HUNG_EXIT)


async def _unsubscribe(subscription: JetStreamContext.PullSubscription) -> None:
    """End ``subscription``; one the bus already dropped is logged and left."""
    try:
        await subscription.unsubscribe()
    except NatsError as exc:
        log.warning("maintenance_unsubscribe_failed", extra={"error": f"{type(exc).__name__}: {exc}"[:300]})


def creds_document(*, jwt: str, seed: str) -> str:
    """The NATS credentials document nats-py reads (:class:`nats.aio.client.RawCredentials`), from the two values the store
    holds for a bus user: each sits on the line after its marker."""
    return f"-----BEGIN NATS USER JWT-----\n{jwt.strip()}\n------END NATS USER JWT------\n\n-----BEGIN USER NKEY SEED-----\n{seed.strip()}\n------END USER NKEY SEED------\n"


class _Lease(BaseModel):
    """Until when a running task is this worker's, on its monotonic clock: the ack wait from the delivery's arrival, then
    from when the last answered heartbeat and claim renewal were sent. The heartbeat renews it, and every word about the
    delivery is bounded by it."""

    until: float
    #: The revision of the claim this pass holds on its table; 0 until it holds one.
    claim: int = 0
    #: What this pass writes as its claim, so a claim or renewal whose answer was lost is known as its own when asked again.
    token: str = ""


class TaskQueue:
    """The worker's one connection to the bus, as its own user, and the publishes the passes make on it."""

    def __init__(self, client: Client, closing: asyncio.Event) -> None:
        self._client = client
        self._closing = closing
        self._js: JetStreamContext = client.jetstream()
        self._claims: KeyValue | None = None

    @classmethod
    async def connect(cls, url: str, credentials: str, *, name: str, end_process: Callable[..., NoReturn] = end_replica) -> TaskQueue:
        """Connect as the user ``credentials`` names (a NATS creds document, held in memory), reconnecting for ever: a bus
        outage must leave the worker waiting on the bus, never closed for good.

        A bus that refuses the credential, or ends the connection for good, ends the process (``end_process``). The
        store's seed issues a user a new credential when its grants change and revokes the old one, and nats-py keeps the
        credential it was given: it closes for good on a session the server ends, and retries a credential the server
        refuses for ever (nats-py 2.16 ``Client._process_err`` and ``_attempt_reconnect``). Started again, the replica
        reads the credential the store holds now."""
        closing = asyncio.Event()

        async def errored(exc: Exception) -> None:
            if _REFUSED in str(exc).lower():
                end_process("maintenance_bus_refused", error=str(exc)[:300])
            log.warning("maintenance_bus_error", extra={"error": f"{type(exc).__name__}: {exc}"[:300]})

        async def closed() -> None:
            if not closing.is_set():
                end_process("maintenance_bus_closed")

        client = await nats.connect(
            servers=[url],
            user_credentials=RawCredentials(credentials),
            name=name,
            max_reconnect_attempts=-1,
            reconnect_time_wait=2,
            ping_interval=_PING_INTERVAL,
            max_outstanding_pings=_MAX_OUTSTANDING_PINGS,
            inbox_prefix=INBOX_PREFIX,
            error_cb=errored,
            closed_cb=closed,
        )
        return cls(client, closing)

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

    async def told(self, msg: Msg, said: bytes, *, within: float) -> bool:
        """Say ``said`` about the delivery ``msg``, a heartbeat (``Msg.Ack.Progress``) or how its task ended (an ack, a nak
        with its delay, a term); whether JetStream answered within ``within`` seconds.

        Each is sent as a request, which the consumer answers once it has applied it (measured on nats-server 2.15.2, three
        nodes, R3, 2026-11-09: a heartbeat, an ack, a nak and a term). An answered heartbeat restarts the delivery's ack wait
        from no earlier than when it was sent. JetStream answers the same for a delivery it has already handed to another
        consumer, and applies that stale delivery's late ack, so no one answer says the task is still this worker's: only an
        unbroken run of answers, each inside the ack wait the one before restarted, does (:meth:`Worker._tell`).
        """
        try:
            await self._client.request(msg.reply, said, timeout=within)
        except (NatsError, TimeoutError):
            return False
        return True

    async def claim(self, key: str, token: str, *, within: float) -> int | None:
        """Claim ``key``, a task's table, for this worker, writing ``token``: the claim's revision, or None while another's
        stands. A claim lives one ack wait past its last renewal (the bucket's max age), so it outlives its holder's lease.
        Asked again after an answer was lost, it finds its own ``token`` standing and takes that revision.

        Raises:
            QueueUnavailableError: The bus did not answer within ``within``.
        """
        try:
            claims = await self._bucket()
            return await asyncio.wait_for(self._create(claims, key, token), within)
        except (NatsError, TimeoutError) as exc:
            raise QueueUnavailableError(f"the bus did not answer the claim on {key}: {exc}") from exc

    async def renew(self, key: str, token: str, revision: int, *, within: float) -> int | None:
        """The next revision of this worker's claim on ``key``, or None when it is no longer this worker's: it aged out,
        and another may hold it. A stale holder's renewal cannot match the revision standing (measured on nats-server
        2.15.2, three nodes, R3, 2026-11-09); one asked again after its answer was lost finds its own ``token`` standing at
        the next revision and takes it.

        Raises:
            QueueUnavailableError: The bus did not answer within ``within``.
        """
        try:
            claims = await self._bucket()
            return await asyncio.wait_for(self._update(claims, key, token, revision=revision), within)
        except (NatsError, TimeoutError) as exc:
            raise QueueUnavailableError(f"the bus did not answer the renewal of the claim on {key}: {exc}") from exc

    @staticmethod
    async def _create(claims: KeyValue, key: str, token: str) -> int | None:
        try:
            return await claims.create(key, token.encode())
        except KeyWrongLastSequenceError:
            return await _standing(claims, key, token)

    @staticmethod
    async def _update(claims: KeyValue, key: str, token: str, *, revision: int) -> int | None:
        try:
            return await claims.update(key, token.encode(), last=revision)
        except KeyWrongLastSequenceError:
            return await _standing(claims, key, token)

    async def release(self, key: str, revision: int) -> None:
        """Give up the claim on ``key`` while it is still this worker's: a revision that no longer stands (the claim aged
        out, or another holds it) is refused, and a release the bus does not answer leaves the claim to age out."""
        try:
            claims = await self._bucket()
            await asyncio.wait_for(claims.delete(key, last=revision), _API_TIMEOUT)
        except (NatsError, TimeoutError) as exc:
            log.warning("maintenance_claim_kept", extra={"key": key, "error": f"{type(exc).__name__}: {exc}"[:300]})

    async def _bucket(self) -> KeyValue:
        """The claims bucket, bound once: the chart's stream Job declares it, and the worker never creates it."""
        if self._claims is None:
            self._claims = await self._js.key_value(CLAIMS_BUCKET)
        return self._claims

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
        """Drain, so the acknowledgements and publishes in flight are sent before the connection goes. A connection the
        bus cannot take a drain on (it is reconnecting, or gone) is closed as it is: nothing in flight can be sent on
        it, and an error here would hide whatever ended the block that closes it."""
        self._closing.set()
        try:
            await self._client.drain()
        except NatsError as exc:
            log.warning("maintenance_queue_closed_undrained", extra={"error": f"{type(exc).__name__}: {exc}"[:300]})
            await self._client.close()


class Worker:
    """What one replica runs: one task at a time off the queue, every fired schedule moved onto it, and the store's change
    events handed to ``store_events``.

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
        ack_wait_seconds: float,
        max_deliver: int,
        max_pass_seconds: float,
        retry_ended_seconds: float,
        store_events: Callable[[bytes], Awaitable[object]] | None = None,
        end_process: Callable[..., NoReturn] = end_replica,
    ) -> None:
        self._queue = queue
        self._end_process = end_process
        self._retry_ended = retry_ended_seconds
        self._run = run
        self._store_events = store_events
        self._ready = ready
        self._heartbeat = heartbeat_seconds
        self._ack_wait = ack_wait_seconds
        if ack_wait_seconds - heartbeat_seconds <= _VOUCH_MARGIN:
            raise ValueError(f"a heartbeat every {heartbeat_seconds} s leaves no time to vouch for a task inside an ack wait of {ack_wait_seconds} s")
        self._passing = False
        self._max_deliver = max_deliver
        self._max_pass = max_pass_seconds
        self._stopping = asyncio.Event()

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
        if self._passing:
            self._end_process("maintenance_drain_outrun", seconds=seconds)
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

        Each pull has a subscription of its own, ended once the pull is answered or given up on. A pull a reconnect held
        back can reach the server after the worker gave up on it, and be answered while a pass runs; on a subscription
        that outlived it, that task would wait in the client and be run once the pass ended, its ack wait long run out and
        the task perhaps another replica's by then. Sent to an inbox no one reads, it is handed on after its ack wait
        instead, so a task is only ever run off the pull it answered, and arrives as its ack wait starts: its lease counts
        from then.
        """
        while not self._stopping.is_set():
            if not self._ready():
                await self.pause(_FETCH_TIMEOUT)
                continue
            subscription = await self._queue.bind(TASKS_STREAM, TASK_CONSUMER)
            try:
                msgs = await self._fetch(subscription, 1)
                received = monotonic()
            finally:
                await _unsubscribe(subscription)
            for msg in msgs:
                await self._safely(self._hand_on(msg, received) if self._hands_on(msg) else self._handle(msg, received), msg)

    def _hands_on(self, msg: Msg) -> bool:
        """Whether ``msg`` arrived as the worker stopped with a delivery to spare. On the last delivery this worker allows,
        or the one past it, a hand-back would spend the consumer's last delivery, after which JetStream delivers the task
        no more and keeps it on the queue, where its table takes no other: that delivery is handled as any other is."""
        return self._stopping.is_set() and (msg.metadata.num_delivered or 1) < self._max_deliver

    async def _hand_on(self, msg: Msg, received: float) -> None:
        """Give back, undelayed, a task a pull answered as the worker stopped: started now, its pass would run under the
        drain and end this replica when the drain did, so another replica takes it instead."""
        await self._tell(msg, _nak(0), _Lease(until=received + self._ack_wait))

    async def _handle(self, msg: Msg, received: float) -> None:
        lease = _Lease(until=received + self._ack_wait)
        task = _parsed(msg)
        if task is None:
            await self._tell(msg, Msg.Ack.Term, lease)
            record_task_ended(kind="unreadable")
            return
        if (delivered := msg.metadata.num_delivered or 1) > self._max_deliver:
            await self._end(msg, task, lease, deliveries=delivered, error="every delivery outlived its worker")
            return
        beating = asyncio.create_task(self._keep_alive(msg, task, lease))
        try:
            if not await self._claim(task, lease):
                await self._give_back(msg, task, lease, delivered=delivered)
                return
            self._passing = True
            try:
                await self._run(task)
            except Exception as exc:  # noqa: BLE001 — every failure of a run is the task's, retried or ended below
                await self._retry_or_end(msg, task, exc, lease=lease)
                return
            await self._tell(msg, Msg.Ack.Ack, lease)
        finally:
            self._passing = False
            beating.cancel()
            if lease.claim:
                await self._queue.release(_claim_key(task), lease.claim)
        record_task_completed(kind=task.kind)

    async def _claim(self, task: Task, lease: _Lease) -> bool:
        """Claim ``task``'s table for this pass, waiting up to one ack wait while another's claim stands: a worker that died
        mid-pass leaves its claim one ack wait past its last renewal, and an ack the network held past a stale worker's
        lease can free a task's place under a live pass, whose claim this pass then waits out (ADR 0198). False when it
        still stands, and the task is given back to come again. A claim the bus does not answer is asked again: a bus that
        answers nothing is the heartbeat's to judge, and one that answers the heartbeat but not the claim (a bucket the
        stream Job has not made yet) holds the task until the claim is answered or the pass's hung bound ends the replica."""
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
            await self._tell(msg, _nak(_redelivery_delay(delivered)), lease)
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
        once the pass has run past twice its bound."""
        started = monotonic()
        warned = False
        while True:
            sent = await self._tell(msg, Msg.Ack.Progress, lease)
            if lease.claim:
                sent = min(sent, await self._renew(task, lease))
            lease.until = sent + self._ack_wait
            await asyncio.sleep(self._heartbeat)
            ran = monotonic() - started
            if not warned and ran > self._max_pass:
                warned = True
                record_pass_overran(kind=task.kind)
                log.error("maintenance_task_overran", extra={"subject": msg.subject, "seconds": round(ran)})
            if ran > _HUNG_FACTOR * self._max_pass:
                self._end_process("maintenance_task_hung", subject=msg.subject, seconds=round(ran))

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
        return self._end_process("maintenance_task_lost", subject=msg.subject)

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
                self._end_process("maintenance_claim_lost", key=key)
            lease.claim = revision
            return sent
        return self._end_process("maintenance_task_lost", key=key)

    async def _retry_or_end(self, msg: Msg, task: Task, exc: Exception, *, lease: _Lease) -> None:
        delivered = msg.metadata.num_delivered or 1
        if delivered >= self._max_deliver:
            await self._end(msg, task, lease, deliveries=delivered, error=f"{type(exc).__name__}: {exc}"[:300])
            return
        log.warning("maintenance_task_retried", extra={"subject": msg.subject, "deliveries": delivered, "error": f"{type(exc).__name__}: {exc}"[:300]})
        await self._tell(msg, _nak(_redelivery_delay(delivered)), lease)

    async def _end(self, msg: Msg, task: Task, lease: _Lease, *, deliveries: int, error: str) -> None:
        """End ``task`` on this delivery, counted, and hold its next try ``retry_ended_seconds`` out: the hourly audit queues
        a table or a trash record nothing holds, and would otherwise hand a task that kills its worker straight back. A bus
        that refuses the hold leaves it to that audit."""
        log.error("maintenance_task_ended", extra={"subject": msg.subject, "deliveries": deliveries, "error": error})
        record_task_ended(kind=task.kind)
        await self._tell(msg, Msg.Ack.Term, lease)
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
        try:
            while not self._stopping.is_set():
                for msg in await self._fetch(subscription, _DUE_BATCH):
                    await self._safely(self._move(msg), msg)
        finally:
            # Ended with the loop: the connection's drain waits on every subscription still open, for the client's whole
            # drain timeout (measured on the estate: an idle worker took 34 s to stop).
            await _unsubscribe(subscription)

    async def run_store_events(self) -> None:
        """Hand each of the store's change events to ``store_events`` until :meth:`stop`.

        ``store_events`` raises ``ValueError`` for a body that is no store event, which is ended at once, and ``OSError``
        or :class:`QueueUnavailableError` for one whose handling the store or the bus refused, which is delivered again
        after a delay that grows with each delivery, and ended on its ``max_deliver``-th: an event is a hint the table's
        recheck backs up. The consumer allows one delivery more, on which an event whose handling outlived its worker
        every time is ended without handling. Any other error is a fault of the handling's own: it ends the loop, which
        runs again, and the event is delivered again after the consumer's ack wait.
        """
        handle = self._store_events
        if handle is None:
            return
        subscription = await self._queue.bind(STORE_EVENTS_STREAM, STORE_EVENTS_CONSUMER)
        try:
            while not self._stopping.is_set():
                for msg in await self._fetch(subscription, _DUE_BATCH):
                    await self._safely(self._on_store_event(msg, handle), msg)
        finally:
            await _unsubscribe(subscription)

    async def _on_store_event(self, msg: Msg, handle: Callable[[bytes], Awaitable[object]]) -> None:
        delivered = msg.metadata.num_delivered or 1
        if delivered > self._max_deliver:
            log.error("maintenance_store_event_ended", extra={"subject": msg.subject, "deliveries": delivered, "error": "every delivery outlived its worker"})
            await msg.term()
            return
        try:
            await handle(msg.data)
        except ValueError as exc:
            log.error("maintenance_store_event_unreadable", extra={"subject": msg.subject, "error": str(exc)[:300]})
            await msg.term()
            return
        except (OSError, QueueUnavailableError) as exc:
            await self._retry_or_end_store_event(msg, exc, delivered=delivered)
            return
        await msg.ack()

    async def _retry_or_end_store_event(self, msg: Msg, exc: OSError | QueueUnavailableError, *, delivered: int) -> None:
        error = f"{type(exc).__name__}: {exc}"[:300]
        if delivered >= self._max_deliver:
            log.error("maintenance_store_event_ended", extra={"subject": msg.subject, "deliveries": delivered, "error": error})
            await msg.term()
            return
        log.warning("maintenance_store_event_retried", extra={"subject": msg.subject, "deliveries": delivered, "error": error})
        await msg.nak(delay=_redelivery_delay(delivered))

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
