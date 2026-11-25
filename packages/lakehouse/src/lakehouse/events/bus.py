"""The lakehouse's one seam to the bus: a process's own connection to NATS JetStream, each event published on it once,
whole, sized, traced and under a tight timeout, and each delivery taken on it and settled ([[XC-199]]).

EVERY PUBLISH SITE GOES THROUGH :func:`publish_event`, and no other lakehouse module publishes an event. nats-py is
the client (maintenance's work queue, `maintenance.services.queue`, is the one other place that holds it, ADR 0198).

A SERVICE TAKES ITS DELIVERIES ON THE SAME CONNECTION, by pulling. :meth:`Bus.consume` pulls a durable consumer the
chart's stream Job declares, which the service binds to and never creates, and settles each message by the one rule
`lakehouse.events.bus_delivery` states: acknowledged, asked for again after the lane's delay, or parked and ended. A pull
consumer has no dead letter, so that rule is what ends a message. :meth:`Bus.follow` hands a process every message of
a subject in the stream's order, read by sequence with no consumer on the bus: what each replica must see, and none
may take from another. Both wait for a connection, and take up again on the next one when
the bus ends theirs.

A PROCESS CONNECTS AS ITS OWN BUS USER AND HOLDS NO BUS CREDENTIAL. The chart names the user (``RASK_BUS_USER``) and
the file the kubelet writes the pod's ServiceAccount token to, for the bus's audience (``RASK_BUS_TOKEN_FILE``). Each
connection presents that token as it is on the file then, under the user's name, and the bus's signer answers with a
session as that user, scoped to the user's row of the permission table and ended by the bus after
``RASK_BUS_SESSION_SECONDS`` (ADR 26 of nats-architecture-and-design, auth callout; docs/architecture/bus-credentials-findings.md).
The process holds no key and no seed: what it presents proves which pod it is, and lasts ten minutes. The token is a
bearer credential, so a bus outside development is dialled over TLS (``tls://``), verified against the CA the chart
names (``RASK_BUS_CA_FILE``) or the system's, AND THE HANDSHAKE COMES BEFORE THE SERVER'S GREETING. nats-py otherwise
encrypts only when the greeting, which arrives unencrypted, asks it to, whatever the address says: a server that leaves
the request out, or anyone on the path who removes it, is then sent the token in the clear (measured on nats-py 2.16.0,
2026-11-15). A ``tls://`` bus therefore serves the handshake first (nats-server ``tls { handshake_first }``).

The user's replies arrive under an inbox prefix of its own (``_INBOX_<user>``), which the permission table grants to it
alone, so no other user reads a publish's acknowledgement. The seam asks the bus for that inbox as it connects and
holds no connection the bus refuses it on: every publish on one would wait out its timeout unacknowledged.

A SESSION IS ASKED FOR AGAIN BEFORE THE BUS ENDS IT. nats-py presents a token only as it connects, and closes for good
when the bus ends a session (measured on nats-py 2.16.0 against nats-server 2.15.2), so the loop that keeps the
connection has the client connect again shortly before the session's time is up: the same client, its subscriptions and
the messages it holds unsettled carried over, under the token the file holds then. It asks late, since a signer that is
away then costs the rest of the session.

THE CONNECTION IS KEPT BY A LOOP, NEVER AWAITED BY A START (:meth:`Bus.keep_connected`). A service must start, serve and
stage its events while the bus is away, so nothing waits on the first connection: a publish with no connection fails at
once, and its event stays in its outbox for the relay. The one wait is for a session being asked for again: a publish
that meets it waits up to ``_RENEWED_WITHIN_SECONDS`` for the connection to be back. nats-py reconnects by itself through a short outage. Past that,
or when the bus ends the session (its time ran out unasked, or the signer's key for the user was withdrawn: the client
then closes for good), the loop opens a new client. So a process is never left with a closed client and a healthy face.

THE ENVELOPE IS THE BODY (`lakehouse.events.cloud_events`), in structured mode under ``application/cloudevents+json``, and its
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

import asyncio
import contextlib
import json
import logging
import random
import ssl
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager, contextmanager, suppress
from time import perf_counter
from typing import TYPE_CHECKING, Final, Self

from nats.aio.client import Client
from nats.aio.msg import Msg
from nats.errors import Error as NatsError
from nats.js.api import Header, PubAck
from nats.js.errors import NotFoundError
from opentelemetry import trace
from opentelemetry.trace import Link, Span, SpanKind, StatusCode
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lakehouse.events.bus_delivery import Delivery, Handle, Lane, Outcome, Park, Settlement, being_cancelled, envelope_of, judged
from lakehouse.events.bus_metrics import create_delivery_series, record_delivery, record_processing, record_publish, record_pull_failed, record_refusal
from lakehouse.events.cloud_events import STRUCTURED_CONTENT_TYPE, TRACE_ATTRIBUTES, CloudEvent, trace_context
from lakehouse.governed.projected_token import read_token_file
from lakehouse.process.correlation import continuing, current_trace_context
from lakehouse.process.health import Dependency
from lakehouse.process.lifecycle import closed_quietly
from lakehouse.process.own_threads import on_own_threads


if TYPE_CHECKING:
    from nats.js import JetStreamContext

    from lakehouse.process.supervision import Supervisor


log = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

#: Soft cap: an event with a wide schema facet stays well under it, so crossing it is worth a line, not a refusal.
WARN_PAYLOAD_BYTES: Final = 64 * 1024
#: Hard cap, just under JetStream's default ``max_payload`` of about 1 MiB. No envelope under the broker's limit is
#: refused, and none over it could have been delivered.
MAX_PAYLOAD_BYTES: Final = 900 * 1024

#: The prefix of a bus user's reply subjects, before the user's name.
_INBOX_PREFIX: Final = "_INBOX_"
#: How long before a session's time is up it is asked for again: this share of its length, within these bounds. A
#: connect the signer answers is back in milliseconds (measured 2026-11-15 on nats-server 2.15.2 with two signers: a
#: publish that met a renewal waited 3.2 ms), so the margin is what a slow answer may use; a signer that is away gives
#: none, and the bus then refuses the connect six seconds after it began.
_RENEW_SHARE: Final = 0.1
_RENEW_MARGIN_SECONDS: tuple[float, float] = (5.0, 30.0)
#: How long after a session is asked for again a publish waits for the connection before it is called undelivered.
_RENEWED_WITHIN_SECONDS: Final = 2.0

#: How long nats-py reconnects by itself: this many attempts at each server, this far apart. Past them the client
#: closes, and the loop opens another.
_RECONNECT_ATTEMPTS: Final = 10
_RECONNECT_WAIT_SECONDS: Final = 2
_CONNECT_TIMEOUT_SECONDS: Final = 2
#: How long one opening of a connection may take, its attempts at every server and the asking for its inbox together. A
#: peer that answers a TLS handshake with something else and keeps the socket open holds nats-py's connect for good
#: (measured on nats-py 2.16.0, 2026-11-15: no error past the first, and no return), and the loop must come round to
#: the next opening.
_OPEN_WITHIN_SECONDS: Final = 45.0
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
#: bus in step.
_PAUSE_SECONDS: tuple[float, float] = (2.0, 15.0)

_MESSAGING: Final = frozendict({"messaging.system": "nats", "messaging.operation.name": "publish"})
_PROCESSING: Final = frozendict({"messaging.system": "nats", "messaging.operation.name": "process"})
#: The most of an envelope's id a span carries: the id is its publisher's own text.
_MAX_ID_CHARS: Final = 200

#: How long one pull waits on a consumer that holds nothing, and how long a pull the bus refused waits before the next.
_PULL_SECONDS: Final = 5.0
#: How often a connection the keeper has not yet let go of is looked at again by a consumer waiting for the next.
_NEXT_CONNECTION_SECONDS: Final = 0.5
#: How long a readiness read waits for the first connection of a process that has held none, well inside the watch's
#: own five-second budget for a read (`lakehouse.process.health`).
_FIRST_CONNECTION_SECONDS: Final = 2.0
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

#: What a followed subject's messages are handed to, one at a time.
type Follow = Callable[[Delivery], Awaitable[None]]


class StreamPage(BaseModel):
    """What a stream holds on a subject after a place in it (:meth:`Bus.read`)."""

    model_config = ConfigDict(frozen=True)

    #: The messages after the place, in the stream's order, at most the page's limit.
    messages: list[Delivery]
    #: The sequence of the last message the stream holds on the subject; 0 for one that holds none.
    end: int
    #: Whether a message after the place may be gone: the stream's first message after it is not the next sequence, it
    #: holds none after it though its end is past it, or the place is past its end (a stream made again).
    gap: bool

    @staticmethod
    def first_place(after: int, end: int, limit: int) -> int | None:
        """Where a read after ``after`` begins on a subject whose last sequence is ``end``: at ``after``, at the last
        ``limit`` messages for a new reader (``after`` 0), and nowhere for a place past the end."""
        if after > end:
            return None
        return after if after > 0 else max(0, end - limit)

    @classmethod
    def of(cls, after: int, end: int, messages: list[Delivery]) -> StreamPage:
        """The page a read after ``after`` found: ``messages``, read on a subject whose last sequence is ``end``."""
        first = messages[0].sequence if messages else None
        gap = after > end or (after > 0 and ((first is None and end > after) or (first is not None and first > after + 1)))
        return cls(messages=messages, end=end, gap=gap)


class BusUnavailableError(RuntimeError):
    """The bus did not take a publish: nothing counts as delivered, and the caller's event stays where it staged it."""


class BusSettings(BaseSettings):
    """The bus as the chart names it to a pod, and the bus user that pod's process connects as."""

    model_config = SettingsConfigDict(extra="ignore")

    #: One NATS URL, or several separated by commas. Empty for a stack with no bus.
    url: str = Field(default="", alias="RASK_BUS_URL")
    #: One subject token: the name the connection asks the signer for a session under, and the tail of its inbox prefix.
    user: str = Field(default="", alias="RASK_BUS_USER", pattern=r"^([a-z0-9][a-z0-9-]{0,242})?$")
    #: The file the kubelet keeps this pod's ServiceAccount token in, for the bus's audience.
    token_file: str = Field(default="", alias="RASK_BUS_TOKEN_FILE")
    #: The CA bundle the bus's certificate chains to; empty for the system's trust store.
    ca_file: str = Field(default="", alias="RASK_BUS_CA_FILE")
    #: How long the bus keeps a session before it ends it, as the chart tells the signer and the pod alike.
    session_seconds: int = Field(default=300, ge=20, alias="RASK_BUS_SESSION_SECONDS")

    @model_validator(mode="after")
    def _a_bus_names_who_connects(self) -> Self:
        if self.url and not self.user:
            raise ValueError("RASK_BUS_URL names a bus and RASK_BUS_USER names no user to connect to it as")
        if self.url and not self.token_file:
            raise ValueError("RASK_BUS_URL names a bus and RASK_BUS_TOKEN_FILE names no token to present to it")
        return self

    @property
    def inbox_prefix(self) -> str:
        """The prefix of this user's reply subjects, which the permission table grants to it alone."""
        return f"{_INBOX_PREFIX}{self.user}"

    @property
    def renew_after_seconds(self) -> float:
        """How long into a session it is asked for again: its length less a margin the next connect fits in."""
        low, high = _RENEW_MARGIN_SECONDS
        return self.session_seconds - min(max(self.session_seconds * _RENEW_SHARE, low), high)

    def token(self) -> str:
        """The pod's token as the file holds it now. nats-py calls this as it builds each connect, so a token the
        kubelet has replaced is the one presented.

        Raises:
            OSError: The file is not there to read, or holds no token.
        """
        return read_token_file(self.token_file)

    @property
    def encrypted(self) -> bool:
        """Whether every server of the bus is dialled over TLS, the handshake before anything else."""
        return all(server.strip().startswith("tls://") for server in self.url.split(",") if server.strip())

    def trust(self) -> ssl.SSLContext | None:
        """What the bus's certificate is verified against: the named CA bundle as the file holds it now, or the
        system's trust store for a ``tls://`` bus that names none; None for a bus dialled in plaintext.

        Raises:
            OSError: The bundle is not there to read.
            ssl.SSLError: It holds no certificate.
        """
        if self.ca_file:
            return ssl.create_default_context(cafile=self.ca_file)
        return ssl.create_default_context() if self.encrypted else None


class _Session(BaseModel):
    """What one client tells the loop that keeps it."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    #: Set once the client has closed for good: its reconnects ran out, or the bus ended its session.
    ended: asyncio.Event = Field(default_factory=asyncio.Event)
    #: When the bus last took the client's connect, on the loop's clock: the start of the session it holds.
    began: float = 0.0


async def _open(settings: BusSettings, session: _Session, *, said: set[str]) -> Client:
    """Connect to the bus as ``settings.user``, presenting this pod's token, and hold the connection once the bus has
    granted it the user's own inbox.

    Args:
        settings: Where the bus is, who connects and what it presents.
        session: Told when the bus takes a connect of this client, and when the client has closed for good.
        said: The faults the outage in course has been told of, which this connection's own are added to.

    Raises:
        nats.errors.Error: No server took the connection, or the bus refused the token.
        OSError: The same, from the socket; or the token or the CA bundle is not there to read.
        TimeoutError: The opening took longer than ``_OPEN_WITHIN_SECONDS``.
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
        session.began = asyncio.get_running_loop().time()
        said.clear()
        log.info("bus_reconnected", extra={"user": settings.user})

    async def ended() -> None:
        session.ended.set()

    client = Client()
    async with AsyncExitStack() as failed:
        # A connect that fails, or is cancelled part-way, leaves its socket and tasks to be released here. Quietly: a
        # release that raised would be raised in place of what ended the connect, and a cancellation replaced that way
        # is one the loop never ends on.
        failed.push_async_callback(_released, client)
        opening = await failed.enter_async_context(asyncio.timeout(_OPEN_WITHIN_SECONDS))
        trust = await on_own_threads(settings.trust)
        await client.connect(
            servers=[server.strip() for server in settings.url.split(",") if server.strip()],
            # nats-py calls this as it builds each connect, inside its own coroutine: one read of the projected token,
            # 7 us measured 2026-11-22, once a connect, which a read taken here ahead of it would leave stale on reconnect.
            token=settings.token,
            tls=trust,
            tls_handshake_first=settings.encrypted,
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
        await _require_own_inbox(client, settings, unheard)
        opening.reschedule(None)
        failed.pop_all()
    session.began = asyncio.get_running_loop().time()
    return client


async def _require_own_inbox(client: Client, settings: BusSettings, unheard: list[str]) -> None:
    """Refuse the connection (``BusUnavailableError``) when the bus says, into ``unheard``, it grants no inbox of its own."""
    # THE USER'S OWN INBOX IS ASKED FOR BEFORE THE CONNECTION IS HELD. A session granted under a row without the inbox
    # connects all the same, and the process would answer as connected while each publish waited out its timeout. TWO
    # round trips: nats-py writes a flush's ping ahead of what its buffer still holds, so the first pong comes back before
    # the bus has read the subscription, and the second ping leaves after it (the broker's trace, nats-py 2.16.0 against
    # nats-server 2.15.2, 2026-11-14). The bus's refusal is in by the second pong.
    inbox = await client.subscribe(f"{settings.inbox_prefix}.>")
    await client.flush(timeout=_CONNECT_TIMEOUT_SECONDS)
    await client.flush(timeout=_CONNECT_TIMEOUT_SECONDS)
    await inbox.unsubscribe()
    if unheard:
        raise BusUnavailableError(f"the bus grants the user {settings.user} no inbox of its own ({settings.inbox_prefix}.>)")


async def _ended_within(session: _Session, due: float) -> None:
    """Wait up to ``due`` seconds for ``session`` to end."""
    with suppress(TimeoutError):
        async with asyncio.timeout(due):
            await session.ended.wait()


async def _released(client: Client) -> None:
    """Let go of a client whose opening failed, within ``_CLOSE_SECONDS``: nats-py's close waits on the socket, which a
    peer that keeps it open never lets it finish."""
    with suppress(TimeoutError):
        async with asyncio.timeout(_CLOSE_SECONDS):
            await closed_quietly(client.close)


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
        #: Until when, on the loop's clock, a publish waits for the connection a renewal is bringing back.
        self._renewed_by = 0.0

    async def keep_connected(self) -> None:
        """Hold a connection until cancelled: open one, ask for its session again before the bus ends it, and open
        another whenever the one held has closed for good.

        Each connect presents the token the pod holds at that moment. A bus that does not answer, or refuses the
        token, is tried again after a random pause, and reported once per outage.
        """
        while True:
            session = _Session()
            client = await self._opened(session)
            if client is None:
                await asyncio.sleep(random.uniform(*_PAUSE_SECONDS))  # noqa: S311 — spreads replicas, guards nothing
                continue
            self._client = client
            self._reported = False
            self._said.clear()
            self._held.set()
            log.info("bus_connected", extra={"user": self._settings.user})
            await self._kept(client, session)
            self._held.clear()
            log.warning("bus_connection_ended", extra={"user": self._settings.user})

    async def _kept(self, client: Client, session: _Session) -> None:
        """Wait until ``client`` has closed for good, having it connect again each time its session nears its end."""
        clock = asyncio.get_running_loop()
        while not session.ended.is_set():
            due = session.began + self._settings.renew_after_seconds - clock.time()
            if due > 0:
                await _ended_within(session, due)
                continue
            # Counted from now, so a connect the bus is slow to take is not asked for again on every turn; the bus
            # taking it sets the session's true start.
            session.began = clock.time()
            # The client's own dropping of its connection is no disconnection to report.
            self._said.add(_DISCONNECTED)
            self._renewed_by = clock.time() + _RENEWED_WITHIN_SECONDS
            log.info("bus_session_renewing", extra={"user": self._settings.user})
            await client.force_reconnect()

    async def _opened(self, session: _Session) -> Client | None:
        """A client connected as this process's user, or None when the bus gave no connection."""
        try:
            return await _open(self._settings, session, said=self._said)
        except (NatsError, OSError) as exc:
            self._report(f"{type(exc).__name__}: {exc}"[:300])
        except Exception as exc:  # noqa: BLE001 — the reconnect loop must outlive any connect fault; no exc_info, as the text and trace may hold the token
            # Whatever kept this attempt from a connection, the next attempt is the only way to one, and the loop this
            # runs on must outlive it: a loop that ended would end the process over a bus the service is built to do
            # without. Its type alone is said, and no traceback, since such a fault's text and frames are not known to
            # leave the token out.
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

        A read taken before this process has held any connection waits for the first one, within
        :data:`_FIRST_CONNECTION_SECONDS`: a service's watch starts beside its session, and the first connect returning
        a few milliseconds later is the bus coming up, not the bus being away. A read that meets a session being asked
        for again waits for it as a publish does (:meth:`_renewed`): the round trip of a renewal is no outage to report.

        Raises:
            ConnectionError: No connection is held, or the one held is reconnecting or closed.
        """
        if self._client is None:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(_FIRST_CONNECTION_SECONDS):
                    await self._held.wait()
        await self._renewed()
        if not self.connected:
            raise ConnectionError("this process is not connected to the bus")

    def _connected(self) -> Client | None:
        client = self._client
        return client if client is not None and client.is_connected else None

    async def _renewed(self) -> None:
        """Wait for a connection that is asking for its session again, up to ``_RENEWED_WITHIN_SECONDS`` from the
        asking: it is back in a round trip. A connection that is away for any other reason is not waited for."""
        clock = asyncio.get_running_loop()
        # nats-py raises no event for a reconnect it is still making, so its flag is read against the deadline.
        while (client := self._client) is not None and client.is_reconnecting and clock.time() < self._renewed_by:  # noqa: ASYNC110
            await asyncio.sleep(0.02)

    async def _up(self) -> Client | None:
        """The connection when it is up, once a session being asked for again is back: a publish that met the asking
        need not be left to its relay."""
        await self._renewed()
        return self._connected()

    async def send(self, topic: str, body: bytes, *, message_id: str, timeout: float) -> None:  # noqa: ASYNC109 — the wait is nats-py's own, handed this bound
        """Publish ``body`` on ``topic`` under ``message_id`` and wait up to ``timeout`` seconds for the stream's acknowledgement.

        Raises:
            BusUnavailableError: No connection is up, the bus refused the publish, or the stream acknowledged a
                duplicate and holds another message under the id.
            TimeoutError: The bus did not acknowledge in time.
        """
        client = await self._up()
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

    async def read(self, stream: str, subject: str, *, after: int, limit: int) -> StreamPage:
        """What ``stream`` holds on ``subject`` after the sequence ``after``, read by sequence as a follow reads it: at most
        ``limit`` messages, and from ``after`` 0 the last ``limit`` the stream holds. Every process reading one stream reads
        the same messages under the same sequences, so a reader's place is good with any of them.

        Raises:
            BusUnavailableError: No connection is up, or the bus did not answer the read.
        """
        client = await self._up()
        if client is None:
            raise BusUnavailableError(f"this process is not connected to the bus: {subject} was not read")
        stream_reads = client.jetstream()
        try:
            try:
                end = (await stream_reads.get_last_msg(stream, subject)).seq or 0
            except NotFoundError as exc:
                if exc.err_code != _NO_MESSAGE_FOUND:
                    raise  # the stream itself is missing: a read the bus cannot answer, not an empty one
                end = 0
            messages = await _stored_after(stream_reads, stream, subject, place=StreamPage.first_place(after, end, limit), end=end, limit=limit)
        except NatsError as exc:
            raise BusUnavailableError(f"the bus did not answer the read of {subject}: {type(exc).__name__}: {exc}"[:500]) from exc
        return StreamPage.of(after, end, messages)

    async def consume(self, lane: Lane, handle: Handle, *, park: Park) -> None:
        """Hand every message of ``lane``'s consumer to ``handle`` until cancelled, at most ``lane.in_flight`` at once,
        and tell the bus what became of each (`lakehouse.events.bus_delivery.judged`).

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
            await self._pulled(client, lane, handle, park=park)

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

    async def _pulled(self, client: Client, lane: Lane, handle: Handle, *, park: Park) -> None:
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
                    handling.add(asyncio.create_task(_settled(msg, lane, handle, park=park), name=f"bus-delivery-{lane.consumer}"))
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
                record_pull_failed(following.subject)
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
            record_pull_failed(lane.consumer)
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
    (`lakehouse.process.supervision`)."""
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
    still reads cannot be rendered at all (measured in a task on CPython 3.15.2, aarch64, 2026-11-25: `json.loads` reads
    an id 65,197 lists deep, and `str` raises `RecursionError` past 26,080).

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


async def _settled(msg: Msg, lane: Lane, handle: Handle, *, park: Park) -> None:
    """Handle one message of ``lane`` as a span of the trace it was published under, and tell the bus how it was
    settled. A settle the bus does not take is said, and the message comes again after the consumer's ack wait.
    Cancelled, the message is handed back at once."""
    delivery = _delivery_of(msg)
    with processing(delivery.subject, delivery.envelope() or {}, consumer=lane.consumer) as span:
        try:
            settlement = await judged(delivery, lane, handle, park=park)
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


async def _stored_after(stream_reads: JetStreamContext, stream: str, subject: str, *, place: int | None, end: int, limit: int) -> list[Delivery]:
    """At most ``limit`` messages ``stream`` holds on ``subject`` after sequence ``place`` up to ``end``, read by sequence."""
    messages: list[Delivery] = []
    while place is not None and len(messages) < limit and place < end:
        try:
            stored = await stream_reads.get_msg(stream, seq=place + 1, subject=subject, next=True)
        except NotFoundError as exc:
            if exc.err_code != _NO_MESSAGE_FOUND:
                raise
            break
        place = stored.seq or place + 1
        messages.append(Delivery(subject=stored.subject or subject, body=stored.data or b"", sequence=place, attempt=1))
    return messages


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
    """The publish's trace context as envelope attributes, written by the W3C propagator alone; nothing outside a span."""
    return {field: value for field, value in current_trace_context().items() if field in TRACE_ATTRIBUTES}


def _body(envelope: CloudEvent, topic: str) -> bytes:
    """``envelope`` as the bytes the bus carries, held to the claim-check sizes, with the publish's own trace context and
    no other: a trace the envelope was staged with is the publish's parent (:func:`publish_event`), never copied onto the
    wire, so a stager's text reaches no consumer.

    Raises:
        ValueError: The envelope is past ``MAX_PAYLOAD_BYTES``. The refusal is counted.
    """
    attributes = {name: value for name, value in envelope.as_wire().items() if name not in TRACE_ATTRIBUTES}
    body = json.dumps(attributes | _trace_attributes()).encode()
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
        envelope: The event in its envelope; it goes out with the publish's trace context, in the trace the envelope
            names where it names one.
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
    # In the trace the envelope was staged under, where it names one: a relay's copy of a staged event is a span of the
    # request that made it (ADR 0209), and its envelope goes out naming this span, a child of that request. The work that
    # sent it (a relay's tick) is kept as a link, so both traces reach the publish.
    sender = trace.get_current_span().get_span_context()
    links = [Link(sender)] if envelope.traceparent is not None and sender.is_valid else None
    with (
        continuing(envelope.traceparent, envelope.tracestate),
        tracer.start_as_current_span(
            f"publish {topic}",
            kind=SpanKind.PRODUCER,
            attributes={**attributes, "messaging.message.id": envelope.id},
            links=links,
            record_exception=False,
        ),
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


def bus_dependency(bus: Bus | None) -> list[Dependency]:
    """The bus the chart names this process, reported and never gating, since every announcement it carries is staged
    in an outbox first and the bus keeps what a consumer could not take; nothing for a process named no bus.

    The verdict is the state of this process's own connection: a closed or reconnecting client is the bus being away
    for this process, whatever a fresh connection would be told.
    """
    return [Dependency(name="bus", read=bus.answering, required=False)] if bus is not None else []


__all__ = [
    "MAX_PAYLOAD_BYTES",
    "WARN_PAYLOAD_BYTES",
    "Bus",
    "BusSettings",
    "BusUnavailableError",
    "Follow",
    "PullEndedError",
    "StreamPage",
    "bus_dependency",
    "bus_session",
    "processing",
    "publish_event",
    "pull",
]
