"""The lakehouse's one seam to the bus: a process's own connection to NATS JetStream, and each event published on it
once, whole, sized, traced and under a tight timeout ([[XC-199]]).

EVERY PUBLISH SITE GOES THROUGH :func:`publish_event`, and no other lakehouse module publishes an event. nats-py is
the client (maintenance's work queue, `maintenance.services.queue`, is the one other place that holds it, ADR 0198).

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
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from time import perf_counter
from typing import TYPE_CHECKING, Final, Self

from nats.aio.client import Client, RawCredentials
from nats.errors import Error as NatsError
from nats.js.api import Header, PubAck
from nats.js.errors import NotFoundError
from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lakehouse.bus_metrics import record_publish, record_refusal
from lakehouse.cloud_events import STRUCTURED_CONTENT_TYPE, CloudEvent
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

#: The W3C Trace Context fields CloudEvents' distributed tracing extension carries as attributes of the same names.
_TRACE_FIELDS: Final = ("traceparent", "tracestate")

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

type Ended = Callable[[], Awaitable[None]]


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
            log.info("bus_connected", extra={"user": self._settings.user})
            await ended.wait()
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
    try:
        envelope = json.loads(body)
    except ValueError:
        return None
    if not isinstance(envelope, dict) or "data" not in envelope:
        return None
    return json.dumps(envelope["data"], sort_keys=True, separators=(",", ":"))


def _trace_attributes() -> dict[str, str]:
    """The publish's trace context as envelope attributes; nothing outside a span."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return {field: carrier[field] for field in _TRACE_FIELDS if field in carrier}


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
