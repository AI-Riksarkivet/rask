"""The lakehouse's one seam to the bus: a process's own connection to NATS JetStream, each event published on it once,
whole, sized, traced and under a tight timeout, and each delivery taken on it and settled ([[XC-199]]).

EVERY PUBLISH SITE GOES THROUGH :func:`publish_event`, and no other lakehouse module publishes an event. nats-py is
the client (maintenance's work queue, `maintenance.services.queue`, is the one other place that holds it, ADR 0198).

A SERVICE TAKES ITS DELIVERIES ON THE SAME CONNECTION, by pulling. :meth:`Bus.consume` pulls a durable consumer the
chart's stream Job declares, which the service binds to and never creates, and settles each message by the one rule
`lakehouse.bus_delivery` states: acknowledged, asked for again after the lane's delay, or parked and ended. A pull
consumer has no dead letter, so that rule is what ends a message. :meth:`Bus.follow` hands a process every message of
a subject in the stream's order, read by sequence with no consumer on the bus: what each replica must see, and none
may take from another. Both wait for a connection, and take up again on the next one when
the bus ends theirs.

A PROCESS CONNECTS AS ITS OWN BUS USER. The chart names the user (``RASK_BUS_USER``), the store holds its credential
as ``nats-user-<user>`` (a JWT and the seed that signs the server's nonce), and the process reads it under its own store
login and hands it to nats-py in memory: no file and no environment variable ever holds the seed. The user's replies
arrive under an inbox prefix of its own (``_INBOX_<user>``), which the permission table grants to it alone, so no other
user reads a publish's acknowledgement. The seam asks the bus for that inbox as it connects and holds no connection
the bus refuses it on: every publish on one would wait out its timeout unacknowledged.

THE CONNECTION IS KEPT BY A LOOP, NEVER AWAITED BY A START (:meth:`Bus.keep_connected`). A service must start, serve and
stage its events while the bus is away, so nothing waits on the first connection: a publish with no connection fails at
once, and its event stays in its outbox for the relay. nats-py reconnects by itself through a short outage. Past that,
or when the bus ends the session (a credential it revoked or let expire, measured on nats-py 2.16: the client then
closes for good), the loop opens a new client with the credential the store holds at that moment. So a process is never
left with a closed client and a healthy face, and a re-issued credential is taken up without a restart.

THE ENVELOPE IS THE BODY (`lakehouse.cloud_events`), in structured mode under ``application/cloudevents+json``, and its
``id`` is the message's ``Nats-Msg-Id``: inside the stream's duplicate window a second publish of one event is stored
once. A DUPLICATE ACKNOWLEDGEMENT IS BELIEVED ONLY AFTER THE STORED MESSAGE IS READ BACK. A message id names nobody, so
any publisher on a topic can store a message of its own under an event's id first, and the stream then drops the event
and still acknowledges it. The acknowledgement names the sequence it kept, so the seam reads that message and counts the
event delivered only when it carries the same event; otherwise the publish fails, the event stays staged, and its relay
delivers it once the window has passed.

A TIGHT TIMEOUT PER SITE, so a bus that does not answer cannot hold its caller: each publish is bounded by the
caller's own ``timeout_seconds``, and running out is a ``TimeoutError`` the caller's failure path already handles.

THE CLAIM-CHECK GUARD. Events carry pointers (a dataset, a version, a URI), never data, and JetStream's default maximum
message is about 1 MiB, so a data-shaped payload would fail at the broker anyway, later and as an opaque transport
error. An envelope past the hard cap raises ``ValueError`` naming the rule before anything is sent, and one past the
soft cap is published and logged, the early sight of facet bloat (docs/DATA-CONTRACT.md §4).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager, suppress
from functools import partial
from time import perf_counter
from typing import TYPE_CHECKING, Final, Self

from nats.aio.client import Client, RawCredentials
from nats.aio.msg import Msg
from nats.errors import Error as NatsError
from nats.js.api import Header, PubAck
from nats.js.errors import NotFoundError
from opentelemetry import propagate, trace
from opentelemetry.trace import Span, SpanKind, StatusCode
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lakehouse.bus_delivery import Delivery, Handle, Lane, Outcome, Park, Settlement, being_cancelled, envelope_of, judged
from lakehouse.bus_metrics import create_delivery_series, record_delivery, record_processing, record_publish, record_refusal
from lakehouse.cloud_events import STRUCTURED_CONTENT_TYPE, TRACE_ATTRIBUTES, CloudEvent, trace_context
from lakehouse.governed.secrets import read_required_secret
from lakehouse.lifecycle import closed_quietly


if TYPE_CHECKING:
    from nats.js import JetStreamContext

    from lakehouse.supervision import Supervisor


log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

#: Soft cap: an event with a wide schema facet stays well under it, so crossing it is worth a line, not a refusal.
WARN_PAYLOAD_BYTES: Final = 64 * 1024
#: Hard cap, just under JetStream's default ``max_payload`` of about 1 MiB. No envelope under the broker's limit is
#: refused, and none over it could have been delivered.
MAX_PAYLOAD_BYTES: Final = 900 * 1024

#: What the store holds a bus user's credential as, before the user's name, and the prefix of its reply subjects, likewise.
_STORED_AS: Final = "nats-user-"
_INBOX_PREFIX: Final = "_INBOX_"

#: How long nats-py reconnects by itself: this many attempts at each server, this far apart. Past them the client
#: closes, and the loop opens another with a fresh read of the credential.
_RECONNECT_ATTEMPTS: Final = 10
_RECONNECT_WAIT_SECONDS: Final = 2
_CONNECT_TIMEOUT_SECONDS: Final = 2
#: How often the client asks its server whether it is still there, and how many unanswered asks drop the connection: a
#: node that dies without closing it is left within 30 s (nats-py's default, 120 s x 2, leaves it for four minutes).
_PING_INTERVAL_SECONDS: Final = 10
_MAX_OUTSTANDING_PINGS: Final = 2
#: How the bus answers a subscription its connection's user is not granted, as nats-py hands the server's text on:
#: lower-cased (nats-py 2.16.0, `nats/protocol/parser.py:137`).
_SUBSCRIPTION_REFUSED: Final = "permissions violation for subscription"
#: How many different faults one outage is told of: each server refuses in its own words, and a log line per attempt
#: would be one every two seconds for as long as the bus is away.
_FAULTS_SAID: Final = 16
#: How long a closing connection waits for the bus to take what it has sent, and then for its socket to be let go:
#: each inside a pod's teardown, whatever the bus does.
_CLOSE_SECONDS: Final = 3
#: What the faults told of an outage hold once its disconnection has been logged.
_DISCONNECTED: Final = "disconnected"
#: The bounds of the random wait between one failed connection and the next, so a release's replicas do not ask the
#: store and the bus in step.
_PAUSE_SECONDS: tuple[float, float] = (2.0, 15.0)

_MESSAGING: Final = {"messaging.system": "nats", "messaging.operation.name": "publish"}
_PROCESSING: Final = {"messaging.system": "nats", "messaging.operation.name": "process"}
#: The most of an envelope's id a span carries: the id is its publisher's own text.
_MAX_ID_CHARS: Final = 200

#: How long one pull waits on a consumer that holds nothing, and how long a pull the bus refused waits before the next.
_PULL_SECONDS: Final = 5.0
#: How often a connection the keeper has not yet let go of is looked at again by a consumer waiting for the next.
_NEXT_CONNECTION_SECONDS: Final = 0.5
#: How long past a pull's own wait the bus is given to end it before it is judged gone.
_PULL_ANSWERED_WITHIN_SECONDS: Final = 10.0
#: The status the bus ends a pull with once its wait ran out with no message.
_PULL_EXPIRED: Final = "408"
#: What every delivery's reply subject begins with, and no status the bus sends has: a status has no reply subject.
_ACK_PREFIX: Final = "$JS.ACK."
#: How often a followed subject that held nothing new is read again.
_FOLLOW_EVERY_SECONDS: Final = 1.0
#: JetStream's error code for a read by sequence that found no message (`JSNoMessageFoundErr`).
_NO_MESSAGE_FOUND: Final = 10037

type Ended = Callable[[], Awaitable[None]]
#: What a followed subject's messages are handed to, one at a time.
type Follow = Callable[[Delivery], Awaitable[None]]


class BusUnavailableError(RuntimeError):
    """The bus did not take a publish: nothing counts as delivered, and the caller's event stays where it staged it."""


class BusSettings(BaseSettings):
    """The bus as the chart names it to a pod, and the bus user that pod's process connects as."""

    model_config = SettingsConfigDict(extra="ignore")

    #: One NATS URL, or several separated by commas. Empty for a stack with no bus.
    url: str = Field(default="", alias="RASK_BUS_URL")
    #: The tail of a secret's name and one subject token: the store holds the credential as ``nats-user-<user>``, and
    #: the inbox prefix is built from it.
    user: str = Field(default="", alias="RASK_BUS_USER", pattern=r"^([a-z0-9][a-z0-9-]{0,242})?$")

    @model_validator(mode="after")
    def _a_bus_names_its_user(self) -> Self:
        if self.url and not self.user:
            raise ValueError("RASK_BUS_URL names a bus and RASK_BUS_USER names no user to connect to it as")
        return self

    @property
    def user_secret(self) -> str:
        """The secret the store holds this user's credential as."""
        return f"{_STORED_AS}{self.user}"

    @property
    def inbox_prefix(self) -> str:
        """The prefix of this user's reply subjects, which the permission table grants to it alone."""
        return f"{_INBOX_PREFIX}{self.user}"


def read_credentials(secret: str, *, attempts: int | None = None) -> str:
    """The credentials of the bus user the store holds as ``secret``, read now under this process's own store login: the
    document nats-py takes in memory (:class:`nats.aio.client.RawCredentials`), its JWT and its seed each on the line
    after its marker.

    Args:
        secret: The secret's name (``nats-user-<user>``).
        attempts: How many reads to make before giving up; the store read's start budget when not given.

    Raises:
        RuntimeError: The store did not serve the credential, or holds it without its seed or its JWT.
    """
    read = read_required_secret if attempts is None else partial(read_required_secret, retries=attempts)
    held = read(secret, require="seed")
    if not held.get("jwt"):
        raise RuntimeError(f"secret {secret!r} gave this process no 'jwt': failing closed (the secret store is the sole source)")
    return (
        f"-----BEGIN NATS USER JWT-----\n{held['jwt'].strip()}\n------END NATS USER JWT------\n\n"
        f"-----BEGIN USER NKEY SEED-----\n{held['seed'].strip()}\n------END USER NKEY SEED------\n"
    )


async def _open(settings: BusSettings, credentials: str, *, ended: Ended, said: set[str]) -> Client:
    """Connect to the bus as ``settings.user`` with ``credentials``, a credentials document held in memory, and hold the
    connection once the bus has granted it the user's own inbox.

    Args:
        settings: Where the bus is and who connects.
        credentials: The user's credentials document (:func:`read_credentials`).
        ended: Called once the client has closed for good: its reconnects ran out, or the bus ended its session.
        said: The faults the outage in course has been told of, which this connection's own are added to.

    Raises:
        nats.errors.Error: No server took the connection, or the bus refused the credential.
        OSError: The same, from the socket.
        BusUnavailableError: The bus took the connection and grants its user no inbox of its own.
    """
    unheard: list[str] = []

    async def errored(exc: Exception) -> None:
        # Each failed attempt reports here, two seconds apart and in each server's own words for as long as an outage
        # lasts, so a text is logged once per outage.
        text = f"{type(exc).__name__}: {exc}"[:300]
        if _SUBSCRIPTION_REFUSED in text.lower():
            unheard.append(text)
        if text not in said and len(said) < _FAULTS_SAID:
            said.add(text)
            log.warning("bus_error", extra={"user": settings.user, "error": text})

    async def disconnected() -> None:
        # nats-py calls this for every client it closes, one a failed attempt releases included.
        if _DISCONNECTED not in said:
            said.add(_DISCONNECTED)
            log.warning("bus_disconnected", extra={"user": settings.user})

    async def reconnected() -> None:
        said.clear()
        log.info("bus_reconnected", extra={"user": settings.user})

    client = Client()
    async with AsyncExitStack() as failed:
        # A connect that fails, or is cancelled part-way, leaves its socket and tasks to be released here. Quietly: a
        # release that raised would be raised in place of what ended the connect, and a cancellation replaced that way
        # is one the loop never ends on.
        failed.push_async_callback(closed_quietly, client.close)
        await client.connect(
            servers=[server.strip() for server in settings.url.split(",") if server.strip()],
            user_credentials=RawCredentials(credentials),
            name=settings.user,
            inbox_prefix=settings.inbox_prefix,
            allow_reconnect=True,
            max_reconnect_attempts=_RECONNECT_ATTEMPTS,
            reconnect_time_wait=_RECONNECT_WAIT_SECONDS,
            connect_timeout=_CONNECT_TIMEOUT_SECONDS,
            ping_interval=_PING_INTERVAL_SECONDS,
            max_outstanding_pings=_MAX_OUTSTANDING_PINGS,
            error_cb=errored,
            disconnected_cb=disconnected,
            reconnected_cb=reconnected,
            closed_cb=ended,
        )
        # THE USER'S OWN INBOX IS ASKED FOR BEFORE THE CONNECTION IS HELD. A credential issued before its row granted the
        # inbox connects all the same, and the process would answer as connected while each publish waited out its
        # timeout. TWO round trips: nats-py writes a flush's ping ahead of what its buffer still holds, so the first
        # pong comes back before the bus has read the subscription, and the second ping leaves after it (the broker's
        # trace, nats-py 2.16.0 against nats-server 2.15.2, 2026-11-14). The bus's refusal is in by the second pong.
        inbox = await client.subscribe(f"{settings.inbox_prefix}.>")
        await client.flush(timeout=_CONNECT_TIMEOUT_SECONDS)
        await client.flush(timeout=_CONNECT_TIMEOUT_SECONDS)
        await inbox.unsubscribe()
        if unheard:
            raise BusUnavailableError(f"the bus grants the user {settings.user} no inbox of its own ({settings.inbox_prefix}.>)")
        failed.pop_all()
    return client


class Bus:
    """This process's one connection to the bus, as its own user, for as long as the process runs."""

    def __init__(self, settings: BusSettings) -> None:
        self._settings = settings
        self._client: Client | None = None
        #: Whether the outage in course has been logged: a bus that is away is tried every few seconds.
        self._reported = False
        #: What the bus itself has said during that outage, each fault once.
        self._said: set[str] = set()
        #: Set while a connection is held, for the consumers waiting for one.
        self._held = asyncio.Event()

    async def keep_connected(self) -> None:
        """Hold a connection until cancelled: open one, and open another whenever the one held has closed for good.

        Each connection is opened with the credential the store holds at that moment, so a user the store's seed issued
        again is connected as again without a restart. A bus or a store that does not answer is tried again after a
        random pause, and reported once per outage.
        """
        while True:
            ended = asyncio.Event()
            client = await self._opened(ended)
            if client is None:
                await asyncio.sleep(random.uniform(*_PAUSE_SECONDS))  # noqa: S311 — spreads replicas, guards nothing
                continue
            self._client = client
            self._reported = False
            self._said.clear()
            self._held.set()
            log.info("bus_connected", extra={"user": self._settings.user})
            await ended.wait()
            self._held.clear()
            log.warning("bus_connection_ended", extra={"user": self._settings.user})

    async def _opened(self, ended: asyncio.Event) -> Client | None:
        """A client connected as this process's user, or None when the store gave no credential or the bus no connection."""

        async def end() -> None:
            ended.set()

        try:
            credentials = await asyncio.to_thread(read_credentials, self._settings.user_secret, attempts=1)
            return await _open(self._settings, credentials, ended=end, said=self._said)
        except (NatsError, OSError, RuntimeError) as exc:
            self._report(f"{type(exc).__name__}: {exc}"[:300])
        except Exception as exc:
            # Whatever kept this attempt from a connection, the next attempt is the only way to one. A credential nats-py
            # cannot sign with is raised outside its own errors (`nkeys`), and the loop this runs on must outlive it: a
            # loop that ended would end the process over a bus the service is built to do without. Its type alone is
            # said, since such a fault's text is not known to leave the credential out.
            self._report(type(exc).__name__)
        return None

    def _report(self, error: str) -> None:
        """Say once per outage why no connection was opened."""
        if not self._reported:
            log.warning("bus_not_connected", extra={"user": self._settings.user, "error": error})
        self._reported = True

    @property
    def connected(self) -> bool:
        """Whether this process's connection to the bus is up: one is held, and it is neither reconnecting nor closed."""
        return self._connected() is not None

    async def answering(self) -> None:
        """Raise unless this process's connection to the bus is up: the verdict a readiness report shows for the bus.

        Raises:
            ConnectionError: No connection is held, or the one held is reconnecting or closed.
        """
        if not self.connected:
            raise ConnectionError("this process is not connected to the bus")

    def _connected(self) -> Client | None:
        client = self._client
        return client if client is not None and client.is_connected else None

    async def send(self, topic: str, body: bytes, *, message_id: str, timeout: float) -> None:
        """Publish ``body`` on ``topic`` under ``message_id`` and wait up to ``timeout`` seconds for the stream's acknowledgement.

        Raises:
            BusUnavailableError: No connection is up, the bus refused the publish, or the stream acknowledged a
                duplicate and holds another message under the id.
            TimeoutError: The bus did not acknowledge in time.
        """
        client = self._connected()
        if client is None:
            # A reconnecting client would buffer the publish and send it later, after the caller had been told it failed.
            raise BusUnavailableError(f"this process is not connected to the bus: {message_id} was not published on {topic}")
        stream = client.jetstream()
        try:
            acknowledged = await stream.publish(
                topic, body, timeout=timeout, headers={Header.MSG_ID.value: message_id, "Content-Type": STRUCTURED_CONTENT_TYPE}
            )
            if acknowledged.duplicate and not await _stored_carries(stream, acknowledged, body):
                raise BusUnavailableError(f"the bus holds another message under the id of {message_id} on {topic}: the event is not delivered")
        except TimeoutError:
            raise
        except NatsError as exc:
            raise BusUnavailableError(f"the bus did not take {message_id} on {topic}: {type(exc).__name__}: {exc}"[:500]) from exc

    async def consume(self, lane: Lane, handle: Handle, *, park: Park) -> None:
        """Hand every message of ``lane``'s consumer to ``handle`` until cancelled, at most ``lane.in_flight`` at once,
        and tell the bus what became of each (`lakehouse.bus_delivery.judged`).

        The consumer is the chart's: this binds to it and creates nothing, and a consumer the bus does not hold yet is
        asked for again until it does. A pull the bus refuses or drops is said once and asked again, and a connection
        the bus ended is waited out: the consumer is pulled again on the next one, where it keeps every message this
        process had not settled. Cancelled, it hands back at once what it was still handling, for another replica or
        this one's next start.

        Args:
            lane: The consumer, and the numbers its deliveries are settled by.
            handle: The service's handling of one delivery.
            park: The lane's keeping of a delivery that is ended unhandled or refused.
        """
        create_delivery_series(lane.consumer, [Outcome.TAKEN, Outcome.AGAIN, Outcome.PARKED])
        client: Client | None = None
        while True:
            client = await self._connection(after=client)
            await self._pulled(client, lane, handle, park)

    async def follow(self, stream: str, subject: str, handle: Follow) -> None:
        """Hand every message published on ``subject`` from now on to ``handle``, one at a time and in the stream's
        order, until cancelled.

        The stream is read by sequence (``$JS.API.STREAM.MSG.GET.<stream>``, the call a publisher's read-back makes):
        the bus keeps no consumer for this process, and no other process takes a message from this one. It begins after
        the last message the stream holds as the first connection is up, and asks for the subject's next message after
        the last one handed on, again every ``_FOLLOW_EVERY_SECONDS`` while there is none, so a message is handed on
        once and none is passed over, through a read the bus refused and a connection it ended. A stream whose end is
        behind the follower's place was made again: the follower begins at its first message, and says so.
        A handling that raises is said and counted, and the next message is handed on: a followed message is read once.
        """
        create_delivery_series(subject, ["taken", "failed"])
        last: int | None = None
        client: Client | None = None
        while True:
            client = await self._connection(after=client)
            last = await self._followed(client, _Followed(stream=stream, subject=subject, last=last), handle)

    async def _connection(self, *, after: Client | None) -> Client:
        """The connection held now, once it is another than ``after`` and open."""
        while True:
            await self._held.wait()
            client = self._client
            if client is not None and client is not after and not client.is_closed:
                return client
            await asyncio.sleep(_NEXT_CONNECTION_SECONDS)

    async def _pulled(self, client: Client, lane: Lane, handle: Handle, park: Park) -> None:
        """Pull ``lane``'s consumer on ``client`` until that connection has closed for good."""
        handling: set[asyncio.Task[None]] = set()
        consumer = _Pull(client, lane)
        try:
            while not client.is_closed:
                _reaped(handling)
                if len(handling) >= lane.in_flight:
                    await asyncio.wait(handling, return_when=asyncio.FIRST_COMPLETED)
                    continue
                msg = await consumer.next()
                if msg is not None:
                    handling.add(asyncio.create_task(_settled(msg, lane, handle, park), name=f"bus-delivery-{lane.consumer}"))
        finally:
            for task in handling:
                task.cancel()
            # Waited for, so each has handed its message back before the connection its caller closes next is gone.
            await asyncio.gather(*handling, return_exceptions=True)

    async def _followed(self, client: Client, following: _Followed, handle: Follow) -> int | None:
        """Follow on ``client`` until that connection has closed for good; the sequence of the last message handed on,
        or that the follow began after, and None while the stream's end has not been read."""
        last = following.last
        said = ""
        while not client.is_closed:
            try:
                if last is None:
                    last = await _end_of(client, following.stream, following.subject)
                stored = await client.jetstream().get_msg(following.stream, seq=last + 1, subject=following.subject, next=True)
            except NatsError as exc:
                if isinstance(exc, NotFoundError) and exc.err_code == _NO_MESSAGE_FOUND and last is not None:
                    said = ""
                    last = await _place_on(client, following, last)
                    await asyncio.sleep(_FOLLOW_EVERY_SECONDS)
                    continue
                said = _said_once(said, exc, "bus_follow_failed", subject=following.subject)
                await asyncio.sleep(_PULL_SECONDS)
                continue
            said = ""
            # A message the bus answers without its sequence is passed over as the one asked for, never asked for again.
            last = stored.seq or last + 1
            await _handed_on(Delivery(subject=stored.subject or following.subject, body=stored.data or b"", sequence=last, attempt=1), handle)
        return last

    async def close(self) -> None:
        """Close the connection once the bus has taken what was sent on it, or after ``_CLOSE_SECONDS`` without. One
        that is not up is closed as it is: every publish was acknowledged or failed before its caller went on, so
        nothing is owed. Closed whatever ends the wait, a cancellation of the caller included, and left after
        ``_CLOSE_SECONDS`` more when its socket is not let go: nats-py waits for the socket's buffer to empty, which a
        peer that died without closing never lets it."""
        client, self._client = self._client, None
        if client is None:
            return
        # This process's own closing of its connection is no disconnection to report.
        self._said.add(_DISCONNECTED)
        try:
            if client.is_connected:
                await client.flush(timeout=_CLOSE_SECONDS)
        except (NatsError, OSError) as exc:
            log.warning("bus_closed_unflushed", extra={"user": self._settings.user, "error": f"{type(exc).__name__}: {exc}"[:300]})
        finally:
            try:
                async with asyncio.timeout(_CLOSE_SECONDS):
                    await client.close()
            except TimeoutError:
                log.warning("bus_close_abandoned", extra={"user": self._settings.user})


class _Followed(BaseModel):
    """One subject a process follows, and the sequence of the last message it handed on or began after (None until the
    stream's end has been read)."""

    model_config = ConfigDict(frozen=True)

    stream: str
    subject: str
    last: int | None


async def _end_of(client: Client, stream: str, subject: str) -> int:
    """The sequence of the last message ``stream`` holds on ``subject``; 0 for one that holds none."""
    try:
        return (await client.jetstream().get_last_msg(stream, subject)).seq or 0
    except NotFoundError:
        return 0


async def _place_on(client: Client, following: _Followed, last: int) -> int:
    """Where a follow that found nothing after ``last`` stands: at ``last``, or before the stream's first message when
    the subject's last one there is behind it or gone. Such a stream was made again and numbers its messages from 1, or
    has outlived every message it held: a follower kept at its old place would ask past every message a new stream
    takes until it had taken that many, and whatever the stream holds now was published after the follower began. A
    read the bus does not answer, or answers for a stream it does not hold, leaves the place as it is, to be asked
    again."""
    try:
        end = (await client.jetstream().get_last_msg(following.stream, following.subject)).seq or 0
    except NotFoundError as exc:
        if exc.err_code != _NO_MESSAGE_FOUND:
            return last
        return 0
    except NatsError:
        return last
    if end >= last:
        return last
    log.warning("bus_follow_began_again", extra={"subject": following.subject, "stream": following.stream, "was_after": last, "holds_to": end})
    return 0


class PullEndedError(NatsError):
    """The bus ended a pull with a status other than finding nothing."""


async def pull(client: Client, stream: str, consumer: str, *, wait: float, answered_within: float = _PULL_ANSWERED_WITHIN_SECONDS) -> Msg | None:
    """The message one pull on ``consumer`` of ``stream`` is answered with, or None once the bus ends the pull with none.

    The pull asks the bus to wait ``wait`` seconds, on an inbox of its own, and is read until the bus ends it: with a
    message, or with the status saying it found none. Never sooner: the bus counts the wait from the pull's arrival and
    goes on answering it until it learns of the unsubscribe, so a client that stopped reading at its own ``wait`` would
    leave the bus answering an inbox nobody reads, and a message sent then would be spent there, handed on only an ack
    wait later and a delivery short. A bus that says nothing for ``answered_within`` past the wait is judged gone, and
    the pull given up.

    A delivery is told from a status by its reply subject, which the bus writes: the subject its settling is published
    on, where a status has none. Headers say nothing here, since a stored message carries its publisher's, and a
    client that reads a ``Status`` header as the bus's word drops or raises on a message the consumer goes on holding
    (measured on nats-server 2.15.2 with nats-py 2.16.0's fetch: ``Status: 408`` read as an empty wait, ``Status: 500``
    raised ``KeyError``, the message left unsettled each time).

    Raises:
        PullEndedError: The bus ended the pull with a status other than finding nothing.
        NatsError: The connection took neither the subscription nor the request.
    """
    inbox = client.new_inbox()
    subscription = await client.subscribe(inbox)
    try:
        request = {"batch": 1, "expires": int(wait * 1_000_000_000)}
        await client.publish(f"$JS.API.CONSUMER.MSG.NEXT.{stream}.{consumer}", json.dumps(request).encode(), reply=inbox)
        try:
            answer = await subscription.next_msg(timeout=wait + answered_within)
        except TimeoutError:
            return None
    finally:
        await closed_quietly(subscription.unsubscribe)
    if answer.reply.startswith(_ACK_PREFIX):
        return answer
    headers = answer.headers or {}
    if headers.get(Header.STATUS) == _PULL_EXPIRED:
        return None
    # The status's own text, on one line: nats-py hands on whatever follows the code, line breaks included.
    said = " ".join(f"{headers.get(Header.STATUS)} {headers.get(Header.DESCRIPTION, '')}".split())
    raise PullEndedError(f"the bus ended a pull on {stream}/{consumer} with {said}"[:300])


class _Pull:
    """One connection's pulls on a lane's consumer, one message at a time."""

    def __init__(self, client: Client, lane: Lane) -> None:
        self._client = client
        self._lane = lane
        #: The fault the outage in course was told of: a consumer the bus does not hold is asked for every few seconds.
        self._said = ""

    async def next(self) -> Msg | None:
        """The next message, or None: an empty wait, or a pull the bus refused or dropped, which is said once and
        waited out."""
        lane = self._lane
        try:
            msg = await pull(self._client, lane.stream, lane.consumer, wait=_PULL_SECONDS)
        except NatsError as exc:
            self._said = _said_once(self._said, exc, "bus_pull_failed", stream=lane.stream, consumer=lane.consumer)
            await asyncio.sleep(_PULL_SECONDS)
            return None
        if msg is not None:
            self._said = ""
        return msg


def _said_once(said: str, exc: NatsError, event: str, **named: str) -> str:
    """Log ``exc`` under ``event`` unless it is the fault ``said`` already; the fault's text, to pass as ``said`` next."""
    text = f"{type(exc).__name__}: {exc}"[:300]
    if text != said:
        log.warning(event, extra={**named, "error": text})
    return text


def _reaped(handling: set[asyncio.Task[None]]) -> None:
    """Forget each handling that has ended. One that ended by what no settle answers (a native library's panic, or a
    cancellation nobody asked of it) raises here, which ends the loop, and the loop's end the process
    (`lakehouse.supervision`)."""
    for task in [task for task in handling if task.done()]:
        handling.discard(task)
        if task.cancelled():
            # Its result would raise CancelledError into the loop, whose end a supervisor would then read as a stop it
            # asked for.
            raise RuntimeError(f"a handling of {task.get_name()} was cancelled while its lane runs")
        task.result()


def _delivery_of(msg: Msg) -> Delivery:
    return Delivery(subject=msg.subject, body=msg.data, sequence=msg.metadata.sequence.stream, attempt=msg.metadata.num_delivered or 1)


@contextmanager
def processing(subject: str, envelope: Mapping[str, object], *, consumer: str | None = None) -> Generator[Span]:
    """One delivery's handling as a CONSUMER span of the trace its envelope was published under, with how long it took
    recorded (`messaging.process.duration`). An exception the handling raises is the handler's to say: the span records
    none and takes no status from it.

    NOTHING HERE RENDERS WHAT THE PUBLISHER SENT. This runs before any rule can settle the delivery, so a failure here
    would end the loop that took it, and the bus would hand the same message to the next replica. The span carries the
    envelope's id only when it is a string: an id of another kind names nothing, and one nested as deep as the parser
    still reads cannot be rendered at all (measured on CPython 3.13.15, 2026-11-14, in a task: `json.loads` reads an id
    9,993 lists deep, and `str` of one 9,992 deep raises `RecursionError`).

    Args:
        subject: The subject the message was published on.
        envelope: The envelope the message carries, or an empty mapping for a body that is none.
        consumer: The durable consumer the delivery was taken from, which a service's replicas share; None for a
            subject a process follows.
    """
    started = perf_counter()
    attributes = {**_PROCESSING, "messaging.destination.name": subject}
    if consumer is not None:
        attributes["messaging.consumer.group.name"] = consumer
    named = {"messaging.message.id": message_id[:_MAX_ID_CHARS]} if isinstance(message_id := envelope.get("id"), str) else {}
    with tracer.start_as_current_span(
        f"process {subject}",
        context=trace_context(envelope),
        kind=SpanKind.CONSUMER,
        attributes={**attributes, **named},
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        try:
            yield span
        finally:
            record_processing(attributes, perf_counter() - started)


async def _settled(msg: Msg, lane: Lane, handle: Handle, park: Park) -> None:
    """Handle one message of ``lane`` as a span of the trace it was published under, and tell the bus how it was
    settled. A settle the bus does not take is said, and the message comes again after the consumer's ack wait.
    Cancelled, the message is handed back at once."""
    delivery = _delivery_of(msg)
    with processing(delivery.subject, delivery.envelope() or {}, consumer=lane.consumer) as span:
        try:
            settlement = await judged(delivery, lane, handle, park)
        except asyncio.CancelledError:
            await _handed_back(msg)
            raise
        if settlement.outcome is Outcome.PARKED:
            span.set_status(StatusCode.ERROR, "the delivery was parked")
    try:
        await _tell(msg, settlement)
    except NatsError as exc:
        log.warning("bus_delivery_unsettled", extra={"consumer": lane.consumer, "sequence": delivery.sequence, "error": f"{type(exc).__name__}: {exc}"[:300]})
        return
    record_delivery(lane.consumer, settlement.outcome)


async def _tell(msg: Msg, settlement: Settlement) -> None:
    """Tell the bus what became of ``msg``: acknowledged, to be delivered again after a delay, or ended."""
    match settlement.outcome:
        case Outcome.TAKEN:
            await msg.ack()
        case Outcome.AGAIN:
            await msg.nak(delay=settlement.delay_seconds)
        case Outcome.PARKED:
            await msg.term()


async def _handed_back(msg: Msg) -> None:
    """Ask for ``msg`` again at once, as its handling is cut by the process stopping; a bus that does not take the ask
    delivers it again after the consumer's ack wait."""
    with suppress(NatsError, TimeoutError):
        async with asyncio.timeout(_CLOSE_SECONDS):
            await msg.nak()


async def _handed_on(delivery: Delivery, handle: Follow) -> None:
    """Hand one followed message to ``handle`` as a span of the trace it was published under."""
    outcome = "taken"
    with processing(delivery.subject, delivery.envelope() or {}) as span:
        try:
            await handle(delivery)
        except (Exception, asyncio.CancelledError) as exc:
            # A followed message is read once, so a handling that failed has lost that message and must not lose the
            # ones behind it: said, counted, and the next is handed on. A cancellation nobody asked of this
            # task is such a failure; one asked of it is the process stopping.
            if isinstance(exc, asyncio.CancelledError) and being_cancelled():
                raise
            span.set_status(StatusCode.ERROR, type(exc).__name__)
            log.exception("bus_followed_message_not_handled", extra={"subject": delivery.subject, "sequence": delivery.sequence})
            outcome = "failed"
    record_delivery(delivery.subject, outcome)


async def _stored_carries(stream: JetStreamContext, acknowledged: PubAck, body: bytes) -> bool:
    """Whether the message the stream kept at the acknowledged sequence carries the event ``body`` does.

    The two bodies differ in their trace, since each publish carries its own, so the events are compared: ``data``, the
    event as its producer signed it.
    """
    try:
        stored = await stream.get_msg(acknowledged.stream, acknowledged.seq)
    except NotFoundError:
        return False
    kept = _event(stored.data or b"")
    return kept is not None and kept == _event(body)


def _event(body: bytes) -> str | None:
    """The event a structured envelope carries, in one spelling, or None for bytes that are no envelope.

    Spelled out as JSON with its keys in order, because two parsed documents compare equal across types JSON tells
    apart (``1``, ``1.0`` and ``true``), and a signature over the one does not verify over the other.
    """
    envelope = envelope_of(body)
    if envelope is None or "data" not in envelope:
        return None
    return json.dumps(envelope["data"], sort_keys=True, separators=(",", ":"))


def _trace_attributes() -> dict[str, str]:
    """The publish's trace context as envelope attributes; nothing outside a span."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return {field: carrier[field] for field in TRACE_ATTRIBUTES if field in carrier}


def _body(envelope: CloudEvent, topic: str) -> bytes:
    """``envelope`` as the bytes the bus carries, with the current trace context, held to the claim-check sizes.

    Raises:
        ValueError: The envelope is past ``MAX_PAYLOAD_BYTES``. The refusal is counted.
    """
    body = json.dumps(envelope.as_wire() | _trace_attributes()).encode()
    if len(body) > MAX_PAYLOAD_BYTES:
        # Counted because this ends before anything is sent: the bus never sees it, so nothing downstream can.
        record_refusal(topic, "oversize")
        raise ValueError(
            f"event payload is {len(body)} bytes (> {MAX_PAYLOAD_BYTES}): events carry POINTERS, never data "
            f"(claim-check invariant, docs/DATA-CONTRACT.md) — topic {topic}"
        )
    if len(body) > WARN_PAYLOAD_BYTES:
        log.warning("bus_publish_payload_large", extra={"bytes": len(body), "topic": topic})
    return body


async def publish_event(bus: Bus, envelope: CloudEvent, *, topic: str, timeout_seconds: float) -> None:
    """Publish ``envelope`` on ``topic`` over this process's connection, within ``timeout_seconds``.

    Args:
        bus: This process's connection to the bus.
        envelope: The event in its envelope; it goes out with the publish's trace context added.
        topic: The NATS subject. A settings field or a constant, never a value off a payload: it labels the telemetry.
        timeout_seconds: How long the bus has to acknowledge the publish.

    Raises:
        ValueError: The envelope is past ``MAX_PAYLOAD_BYTES``. Nothing was sent, and the refusal is counted.
        TimeoutError: The bus did not acknowledge the publish in time.
        BusUnavailableError: No connection is up, the bus refused the publish, or it holds another message under the
            event's id.
    """
    started = perf_counter()
    attributes = {**_MESSAGING, "messaging.destination.name": topic}
    with tracer.start_as_current_span(
        f"publish {topic}", kind=SpanKind.PRODUCER, attributes={**attributes, "messaging.message.id": envelope.id}, record_exception=False
    ):
        try:
            body = _body(envelope, topic)
            async with asyncio.timeout(timeout_seconds):
                await bus.send(topic, body, message_id=envelope.id, timeout=timeout_seconds)
        except Exception as exc:
            record_publish(attributes, perf_counter() - started, failure=type(exc).__name__)
            raise
        record_publish(attributes, perf_counter() - started, failure=None)


@asynccontextmanager
async def bus_session(loops: Supervisor, settings: BusSettings | None = None) -> AsyncGenerator[Bus | None]:
    """This process's connection to the bus for the length of the block, kept on a loop of ``loops``; None for a
    process the chart names no bus.

    The block starts at once, connected or not: the loop connects, and connects again, behind it. On the way out the
    loop is ended first and the connection closed after, so the client's own closing is not read as the bus ending it.
    """
    resolved = settings or BusSettings()
    if not resolved.url:
        yield None
        return
    bus = Bus(resolved)
    keeping = loops.keep("bus-connection", bus.keep_connected)
    try:
        yield bus
    finally:
        keeping.cancel()
        try:
            # `gather`, not an await under `suppress(CancelledError)`: a cancellation of THIS task that lands while the
            # loop ends is its own to end on, and `gather` hands it on where the bare await would swallow it.
            await asyncio.gather(keeping, return_exceptions=True)
        finally:
            await bus.close()
