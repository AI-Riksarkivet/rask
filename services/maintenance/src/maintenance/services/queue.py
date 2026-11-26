"""Maintenance's connection to its work queue on NATS JetStream (ADR 0198 §2): the publishes that put a task on the
queue or hold it as a schedule, the claims a pass takes on its table, and the reads of what the bus holds.

THE WORK QUEUE IS HELD HERE, ON A CONNECTION OF ITS OWN (ADR 0198 decision 5); what a pass emits goes out on the
service's other connection (`lakehouse.events.bus`). A pass tells the bus it is still working, so one longer than its ack
wait is never delivered again beneath itself, and a held task is a message whose headers name when it fires. nats-py is
the client. The streams and their durable consumers are the chart's (:mod:`maintenance.services.queue_wire` names them),
so this binds to them and creates nothing, under a bus user of its own, presenting its pod's token at each connect.

A commit puts no task on the queue itself: it holds its table's FOLLOW-UP (:meth:`TaskQueue.follow_up`) a delay out,
beside the table's schedule on ``maintenance.schedule.followup.<kind>.<uuid>``, unless one is already held there. So a
burst of commits makes one pass, at most one per delay, and that pass reads the table after every commit it covers; the
follow-up never replaces the schedule a pass writes. Fired, it waits while a task holds the table, since a pass already
running may have read the table before the commits it follows.

What a replica does with the tasks it pulls is :mod:`maintenance.services.queue_worker`.
"""

import asyncio
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, Literal

import nats
from nats.aio.client import Client
from nats.aio.msg import Msg
from nats.errors import Error as NatsError
from nats.js.errors import (
    APIError,
    BucketNotFoundError,
    KeyDeletedError,
    KeyNotFoundError,
    KeyWrongLastSequenceError,
    NoStreamResponseError,
    NotFoundError,
)
from opentelemetry import propagate
from pydantic import BaseModel, Field, ValidationError

from lakehouse.events.bus import BusSettings, pull
from lakehouse.process.supervision import EndProcess, Supervisor, end_process
from maintenance.services.queue_wire import CLAIMS_BUCKET, INBOX_PREFIX, SCHEDULES_STREAM, TASKS_STREAM, HeldKind, Task


if TYPE_CHECKING:
    from nats.js import JetStreamContext
    from nats.js.kv import KeyValue


log = logging.getLogger(__name__)

#: JetStream's answers to a publish that expects a subject to hold nothing (`Nats-Expected-Last-Subject-Sequence: 0`) while
#: it holds a message, both "wrong last sequence". Measured on nats-server 2.15.2 (2026-11-09): one server answers 10071
#: (on a held `@at` follow-up, the held one kept unreplaced and the next stored once it fired; on the work queue, refused
#: while a task was queued and while it was in flight, stored once it was acknowledged). A stream copied across a cluster
#: (R3 on three nodes, as on the dev estate) answers 10071 too, and 10164 while another put for the subject is still being
#: agreed, as when a burst of a table's store events puts its follow-ups at once: 40 of 40 racing puts.
_HELD_ALREADY: Final = frozenset({10071, 10164})
_EXPECT_NOTHING: Final = frozendict({"Nats-Expected-Last-Subject-Sequence": "0"})
#: How long a JetStream API request is waited for.
API_TIMEOUT: Final = 10.0
#: How long a readiness read waits for a connection that is reconnecting. A session asked for again is back in a round
#: trip (measured 2026-11-15 on nats-server 2.15.2: 3.2 ms), so a connection still away after this is reported away.
_RECONNECTED_WITHIN_SECONDS: Final = 2.0
#: How often the client asks the server it is connected to whether it is still there, and how many unanswered asks
#: drop the connection: a node that dies without closing it is left within 30 s, inside the 60 s ack wait, so the client
#: reconnects to a live node and its heartbeats are answered again (nats-py's default, 120 s x 2, outlasts the lease).
_PING_INTERVAL: Final = 10.0
_MAX_OUTSTANDING_PINGS: Final = 2


class QueueUnavailableError(RuntimeError):
    """The bus did not take a publish, or did not answer it: nothing was queued, and the caller retries."""


class QueueUnreachableError(QueueUnavailableError):
    """No answer came from the bus at all: a connection that dropped or a request that ran out its bound. It says nothing
    of the stream, where a refusal (the plain :class:`QueueUnavailableError`) is the bus answering, and lasts."""


class ClaimsBucketMissingError(RuntimeError):
    """The claims bucket the chart's stream Job declares is not on the bus, so no pass can claim its table.

    Raised rather than retried: waiting mends nothing a deployment left undone, and a worker that kept asking would hold
    its task until the pass's hung bound ended it. A worker without the bucket does not start, and one whose claim finds it
    gone ends (:meth:`TaskQueue.claim`).
    """


class _ApiRefusal(BaseModel):
    """The error a JetStream API answer carries when it refuses a request."""

    error: dict[str, object] | None = None


class _SubjectsState(BaseModel):
    subjects: dict[str, int] = Field(default_factory=dict)


class _SubjectsPage(BaseModel):
    """One page of a filtered stream info: the subjects it names, and how many match in all."""

    state: _SubjectsState = Field(default_factory=_SubjectsState)
    total: int = 0


class StreamState(BaseModel):
    """What a stream holds, as its info states it: its message count, and when its oldest message was stored (the zero
    time, year 1, while it holds none)."""

    messages: int = 0
    first_ts: datetime | None = None

    def oldest_seconds(self, now: datetime) -> float:
        """How long the oldest message has been held at ``now``; 0 while the stream holds none."""
        if not self.messages or self.first_ts is None:
            return 0.0
        return max((now - self.first_ts).total_seconds(), 0.0)


class _StreamInfo(BaseModel):
    state: StreamState = Field(default_factory=StreamState)


class ConsumerState(BaseModel):
    """What a durable consumer awaits, as its info states it: messages not yet delivered, deliveries not yet acknowledged,
    and messages delivered more than once and not yet settled."""

    #: Required: read as 0 when the answer lacks it, a backlog would read as an empty queue and the stalled-queue alert
    #: never fire; an answer without it is an unread sample instead (:func:`maintenance.services.queue_sampler.sample_queue`).
    num_pending: int
    num_ack_pending: int = 0
    num_redelivered: int = 0


def _carried() -> dict[str, str]:
    """The caller's own trace context, as a message carries it in its headers."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


def at_schedule(at: datetime) -> str:
    """An ``@at`` schedule for the instant ``at``, in UTC to the second (ADR-51), held to the second after an instant
    between two: one held to the second before fires short of ``at``, and a task that holds itself until ``at`` (a trash
    record's, until its release) would find it still ahead and hold itself again, at once, until the clock passed it."""
    utc = at.astimezone(UTC)
    held = utc.replace(microsecond=0) + (timedelta(seconds=1) if utc.microsecond else timedelta(0))
    return f"@at {held.strftime('%Y-%m-%dT%H:%M:%SZ')}"


async def _standing(claims: KeyValue, key: str, token: str) -> int | None:
    """The revision of the claim standing on ``key`` when it holds ``token``; None when it is another's, or none stands."""
    try:
        entry = await claims.get(key)
    except KeyNotFoundError, KeyDeletedError:
        return None
    return entry.revision if entry.value == token.encode() else None


class TaskQueue:
    """The worker's one connection to the bus, as its own user, and the publishes the passes make on it."""

    def __init__(self, client: Client, closing: asyncio.Event, renewing: asyncio.Task[None]) -> None:
        self._client = client
        self._closing = closing
        self._renewing = renewing
        self._js: JetStreamContext = client.jetstream()
        self._claims: KeyValue | None = None

    @classmethod
    async def connect(cls, url: str, presenting: BusSettings, *, name: str, loops: Supervisor, end: EndProcess = end_process) -> TaskQueue:
        """Connect as the bus user ``name``, presenting this pod's token (`lakehouse.events.bus`), and reconnect for ever: a bus
        outage must leave the worker waiting on the bus, never closed for good.

        The worker holds no bus credential. Each connect presents the token the kubelet last wrote, and the bus's signer
        answers it with a session as ``name`` that the bus ends after ``presenting.session_seconds``; the connection is
        made again shortly before that, on the same client, so the tasks it holds and its subscriptions carry over. A
        connect the bus does not take is tried again and said once for as long as the connection is away, whatever the
        fault: a signer that is away shows as a connect that times out, since the bus refuses it only six seconds on
        and nats-py gives a connect two (measured 2026-11-15, nats-server 2.15.2, nats-py 2.16.0). A pass in course is
        held to its lease as through any outage. A session the bus ends all the same closes the client for good
        (nats-py 2.16 ``Client._process_err``), which ends the process (``end``): started again, the replica asks for a
        session again. The loop that asks for the session again is one of ``loops``, so a fault that ends it ends the
        process as any kept loop's does (`lakehouse.process.supervision`): a connection nothing renews would be closed
        by the bus at the session's end.

        Args:
            url: The bus.
            presenting: The token this pod presents, the CA the bus's certificate is verified against, and how long a
                session lasts.
            name: The bus user the signer is asked for a session as.
            loops: The supervisor that keeps the session's renewal.
            end: Ends the process.

        Raises:
            OSError: The token, or the CA bundle, is not there to read.
        """
        closing = asyncio.Event()
        clock = asyncio.get_running_loop()
        began = clock.time()
        held: Client | None = None
        #: Whether the outage in course has been said: nats-py reports every failed attempt, two seconds apart.
        away = False

        async def errored(exc: Exception) -> None:
            nonlocal away
            if held is None or not held.is_connected:
                if away:
                    return
                away = True
            log.warning("maintenance_bus_error", extra={"error": f"{type(exc).__name__}: {exc}"[:300]})

        async def reconnected() -> None:
            nonlocal began, away
            began = clock.time()
            away = False

        async def closed() -> None:
            if not closing.is_set():
                end("maintenance_bus_closed")

        client = await nats.connect(
            servers=[url],
            token=presenting.token,
            tls=presenting.trust(),
            tls_handshake_first=url.startswith("tls://"),
            name=name,
            max_reconnect_attempts=-1,
            reconnect_time_wait=2,
            ping_interval=_PING_INTERVAL,
            max_outstanding_pings=_MAX_OUTSTANDING_PINGS,
            inbox_prefix=INBOX_PREFIX,
            error_cb=errored,
            reconnected_cb=reconnected,
            closed_cb=closed,
        )
        held = client
        away = False
        began = clock.time()

        async def renew() -> None:
            nonlocal began
            while True:
                due = began + presenting.renew_after_seconds - clock.time()
                if due > 0:
                    await asyncio.sleep(due)
                    continue
                # Counted from now, so a connect the bus is slow to take is not asked for again on every turn.
                began = clock.time()
                await client.force_reconnect()

        return cls(client, closing, loops.keep("maintenance-bus-session", renew))

    async def enqueue(self, task: Task, *, carried: Mapping[str, str] | None = None) -> bool:
        """Put ``task`` on the queue; ``False`` when its table already has one queued or in flight, which covers it. The
        publish expects the task's subject to hold nothing, so JetStream refuses it while a task holds it; the stream's
        one-message-per-subject limit stands behind that. The message carries the caller's trace context, or ``carried``,
        the context of the schedule being moved.

        Raises:
            QueueUnavailableError: JetStream refused the publish for another reason, or did not answer it.
        """
        try:
            headers = {**_EXPECT_NOTHING, **(_carried() if carried is None else carried)}
            await self._js.publish(task.subject("task"), task.model_dump_json().encode(), stream=TASKS_STREAM, headers=headers)
        except APIError as exc:
            if exc.err_code in _HELD_ALREADY:
                return False
            raise QueueUnavailableError(f"the queue refused {task.subject('task')}: {exc}") from exc
        except (NatsError, TimeoutError) as exc:
            raise QueueUnreachableError(f"the bus did not take {task.subject('task')}: {exc}") from exc
        return True

    async def told(self, msg: Msg, said: bytes, *, within: float) -> bool:
        """Say ``said`` about the delivery ``msg``, a heartbeat (``Msg.Ack.Progress``) or how its task ended (an ack, a nak
        with its delay, a term); whether JetStream answered within ``within`` seconds.

        Each is sent as a request, which the consumer answers once it has applied it (measured on nats-server 2.15.2, three
        nodes, R3, 2026-11-09: a heartbeat, an ack, a nak and a term). An answered heartbeat restarts the delivery's ack wait
        from no earlier than when it was sent. JetStream answers the same for a delivery it has already handed to another
        consumer, and applies that stale delivery's late ack, so no one answer says the task is still this worker's: only an
        unbroken run of answers, each inside the ack wait the one before restarted, does (:meth:`maintenance.services.queue_worker.Worker._tell`).
        """
        try:
            await self._client.request(msg.reply, said, timeout=within)
        except NatsError, TimeoutError:
            return False
        return True

    async def claim(self, key: str, token: str, *, within: float) -> int | None:
        """Claim ``key``, a task's table, for this worker, writing ``token``: the claim's revision, or None while another's
        stands. A claim lives one ack wait past its last renewal (the bucket's max age), so it outlives its holder's lease.
        Asked again after an answer was lost, it finds its own ``token`` standing and takes that revision.

        A claim no stream answers is asked of a bucket the bus holds no longer, or of one whose leader is being chosen;
        the bucket's own lookup tells the two apart, the first for good.

        Raises:
            ClaimsBucketMissingError: The bus holds the claims bucket no longer.
            QueueUnavailableError: The bus did not answer within ``within``.
        """
        try:
            claims = await self._bucket()
            return await asyncio.wait_for(self._create(claims, key, token), within)
        except NoStreamResponseError as exc:
            await self._require_claims_stream()
            raise QueueUnavailableError(f"no stream answered the claim on {key}: {exc}") from exc
        except (NatsError, TimeoutError) as exc:
            raise QueueUnavailableError(f"the bus did not answer the claim on {key}: {exc}") from exc

    async def _require_claims_stream(self) -> None:
        """Return unless the bus answers that it holds no claims bucket; a lookup it does not answer decides nothing.

        Raises:
            ClaimsBucketMissingError: The bus holds no claims bucket.
        """
        try:
            await asyncio.wait_for(self._js.stream_info(f"KV_{CLAIMS_BUCKET}"), API_TIMEOUT)
        except NotFoundError as exc:
            raise ClaimsBucketMissingError(f"the bus holds no {CLAIMS_BUCKET} bucket; the chart's stream Job declares it") from exc
        except NatsError, TimeoutError:
            return

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

    async def release(self, key: str, token: str, revision: int) -> None:
        """Give up the claim on ``key`` while it is still this worker's: a claim another holds, or one that aged out, is
        left. A release whose ``revision`` a renewal moved since, applied by the bus with its answer lost, finds its own
        ``token`` standing and releases that revision. A release the bus does not answer leaves the claim to age out."""
        try:
            claims = await self._bucket()
            await asyncio.wait_for(self._delete(claims, key, token, revision=revision), API_TIMEOUT)
        except (NatsError, TimeoutError) as exc:
            log.warning("maintenance_claim_kept", extra={"key": key, "error": f"{type(exc).__name__}: {exc}"[:300]})

    @staticmethod
    async def _delete(claims: KeyValue, key: str, token: str, *, revision: int) -> None:
        try:
            await claims.delete(key, last=revision)
        except APIError as exc:
            # A delete's refusal is the publish's own (`nats.js.kv.KeyValue.delete` maps no code), the codes an update's is.
            if exc.err_code not in _HELD_ALREADY:
                raise
            if (standing := await _standing(claims, key, token)) is not None:
                await claims.delete(key, last=standing)

    async def bind_claims(self) -> None:
        """Bind the claims bucket, which a worker does before it takes a task.

        Raises:
            ClaimsBucketMissingError: The bus holds no such bucket.
            QueueUnavailableError: The bus did not answer.
        """
        try:
            await asyncio.wait_for(self._bucket(), API_TIMEOUT)
        except (NatsError, TimeoutError) as exc:
            raise QueueUnavailableError(f"the bus did not answer the claims bucket's lookup: {exc}") from exc

    async def _bucket(self) -> KeyValue:
        """The claims bucket, bound once: the chart's stream Job declares it, and the worker never creates it.

        Raises:
            ClaimsBucketMissingError: The bus holds no such bucket.
        """
        if self._claims is None:
            try:
                self._claims = await self._js.key_value(CLAIMS_BUCKET)
            except BucketNotFoundError as exc:
                raise ClaimsBucketMissingError(f"the bus holds no {CLAIMS_BUCKET} bucket; the chart's stream Job declares it") from exc
        return self._claims

    async def schedule_at(self, task: Task, at: datetime) -> None:
        """Hold ``task`` until ``at``, then put it on the queue, replacing any time it was held for before (ADR-51). A time
        already past fires at once."""
        await self._hold(task, at_schedule(at), held=task.subject("schedule"), fires=task.subject("due"))

    async def schedule_every(self, task: Task, cadence: str) -> None:
        """Put ``task`` on the queue at every firing of ``cadence`` (``@hourly``, ``@every 30m``, a six-field cron), once
        per firing whatever the replica count, replacing any cadence it held before."""
        await self._hold(task, cadence, held=task.subject("schedule"), fires=task.subject("due"))

    async def schedule_first(self, task: Task, at: datetime) -> bool:
        """Hold ``task`` until ``at`` unless a schedule of it is held already, which it then leaves standing; ``False`` then.

        Raises:
            QueueUnavailableError: JetStream refused the schedule for another reason, or did not answer it.
        """
        return await self._hold_unless_held(task, at_schedule(at), held=task.subject("schedule"), fires=task.subject("due"))

    async def follow_up(self, task: Task, at: datetime) -> bool:
        """Hold a commit's ``task`` until ``at``, beside its schedule and never replacing it, unless a follow-up is held
        for it already, which covers this commit: its pass reads the table after it. ``False`` then. Fired, it is queued
        once no task holds the table (:meth:`maintenance.services.queue_worker.Worker.run_due`).

        Raises:
            QueueUnavailableError: JetStream refused the follow-up for another reason, or did not answer it.
        """
        return await self._hold_unless_held(task, at_schedule(at), held=task.follow_up_subject("schedule"), fires=task.follow_up_subject("due"))

    async def _hold(self, task: Task, when: str, *, held: str, fires: str) -> None:
        """Publish ``task`` on ``held`` as a schedule that fires into ``fires`` at ``when``, replacing what ``held`` holds.

        Raises:
            QueueUnavailableError: JetStream refused the schedule, or did not answer it.
        """
        try:
            await self._publish_schedule(task, {"Nats-Schedule": when, "Nats-Schedule-Target": fires}, held=held)
        except APIError as exc:
            raise QueueUnavailableError(f"the queue refused {held}: {exc}") from exc

    async def _hold_unless_held(self, task: Task, when: str, *, held: str, fires: str) -> bool:
        """Publish ``task`` on ``held`` as a schedule that fires into ``fires`` at ``when`` while ``held`` holds no message;
        ``False`` when it holds one, which is left standing.

        Raises:
            QueueUnavailableError: JetStream refused the schedule for another reason, or did not answer it.
        """
        try:
            await self._publish_schedule(task, {"Nats-Schedule": when, "Nats-Schedule-Target": fires, **_EXPECT_NOTHING}, held=held)
        except APIError as exc:
            if exc.err_code in _HELD_ALREADY:
                return False
            raise QueueUnavailableError(f"the queue refused {held}: {exc}") from exc
        return True

    async def _publish_schedule(self, task: Task, schedule: Mapping[str, str], *, held: str) -> None:
        """Publish ``task`` on ``held`` under the ``schedule`` headers and the caller's trace context.

        Raises:
            APIError: JetStream refused the schedule; the caller reads its code.
            QueueUnreachableError: The bus did not answer.
        """
        try:
            await self._js.publish(held, task.model_dump_json().encode(), stream=SCHEDULES_STREAM, headers={**schedule, **_carried()})
        except APIError:
            raise
        except (NatsError, TimeoutError) as exc:
            raise QueueUnreachableError(f"the bus did not take {held}: {exc}") from exc

    async def held(self, area: Literal["task", "schedule"], kind: HeldKind) -> set[str]:
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
            page = await self._api(f"$JS.API.STREAM.INFO.{stream}", {"subjects_filter": f"{prefix}*", "offset": len(found)}, _SubjectsPage)
            before = len(found)
            found |= {subject.removeprefix(prefix) for subject in page.state.subjects}
            if not page.state.subjects or len(found) >= page.total:
                return found
            if len(found) == before:
                # A page that names only subjects already read would be asked for again at the same offset for ever.
                raise QueueUnavailableError(f"the bus answered {stream}'s subjects with a page that adds nothing to the {before} read of {page.total}")

    async def stream_state(self, stream: str) -> StreamState:
        """What ``stream`` holds now, read off its info.

        Raises:
            QueueUnavailableError: JetStream refused the read, or did not answer it.
        """
        return (await self._api(f"$JS.API.STREAM.INFO.{stream}", {}, _StreamInfo)).state

    async def consumer_state(self, stream: str, durable: str) -> ConsumerState:
        """What the durable consumer ``durable`` of ``stream`` awaits now, read off its info.

        Raises:
            QueueUnavailableError: JetStream refused the read, or did not answer it.
        """
        return await self._api(f"$JS.API.CONSUMER.INFO.{stream}.{durable}", {}, ConsumerState)

    async def pulled(self, stream: str, consumer: str, *, wait: float, answered_within: float) -> Msg | None:
        """The message one pull on the chart's durable ``consumer`` of ``stream`` is answered with, or ``None`` once the
        bus ends the pull with none (:func:`lakehouse.events.bus.pull`, which reads each pull on an inbox of its own until the bus
        ends it).

        Raises:
            NatsError: The bus ended the pull with a status other than finding nothing, or took neither the pull's
                subscription nor its request.
        """
        return await pull(self._client, stream, consumer, wait=wait, answered_within=answered_within)

    async def _api[M: BaseModel](self, subject: str, body: dict[str, object], answer: type[M]) -> M:
        """One JetStream API request's answer, read as ``answer``.

        Raises:
            QueueUnavailableError: JetStream answered with an error, answered something that is not that answer, or did
                not answer.
        """
        try:
            reply = await self._client.request(subject, json.dumps(body).encode(), timeout=API_TIMEOUT)
        except (NatsError, TimeoutError) as exc:
            raise QueueUnreachableError(f"the bus did not answer {subject}: {exc}") from exc
        try:
            refusal = _ApiRefusal.model_validate_json(reply.data)
            read = answer.model_validate_json(reply.data)
        except ValidationError as exc:
            raise QueueUnavailableError(f"the bus answered {subject} with something that is not its answer: {exc}"[:500]) from exc
        if refusal.error:
            raise QueueUnavailableError(f"the bus refused {subject}: {refusal.error}")
        return read

    async def answering(self) -> None:
        """Raise unless the connection to the bus is up: the verdict a readiness report shows for the bus. A connection
        that is reconnecting is waited for, up to ``_RECONNECTED_WITHIN_SECONDS``: the round trip in which a session is
        asked for again is no outage to report.

        Raises:
            QueueUnavailableError: The connection is still reconnecting after the wait, or gone.
        """
        clock = asyncio.get_running_loop()
        until = clock.time() + _RECONNECTED_WITHIN_SECONDS
        # nats-py raises no event for a reconnect it is still making, so its flag is read against the deadline.
        while self._client.is_reconnecting and clock.time() < until:  # noqa: ASYNC110
            await asyncio.sleep(0.02)
        if not self._client.is_connected:
            raise QueueUnavailableError("the connection to the bus is not up")

    async def close(self) -> None:
        """Drain, so the acknowledgements and publishes in flight are sent before the connection goes. A connection the
        bus cannot take a drain on (it is reconnecting, or gone) is closed as it is: nothing in flight can be sent on
        it, and an error here would hide whatever ended the block that closes it."""
        self._closing.set()
        self._renewing.cancel()
        await asyncio.gather(self._renewing, return_exceptions=True)
        try:
            await self._client.drain()
        except NatsError as exc:
            log.warning("maintenance_queue_closed_undrained", extra={"error": f"{type(exc).__name__}: {exc}"[:300]})
            await self._client.close()
