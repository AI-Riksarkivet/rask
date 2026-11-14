"""A process publishes to the bus as itself: one connection as its own bus user, and each event once under its own id.

[[XC-199]] (#787). The seam is `lakehouse.bus`: every publish site hands it an envelope, and it is the only lakehouse
module that publishes one. What a real broker does with it is the nats-auth lane's to show
(`tests/e2e-py/test_nats_admits_each_client_only_to_its_own_subjects.py`): a service connected as its own user under the
permission table, its event kept once under its id as the structured envelope, an id another message holds, a bus that
goes away and comes back, a credential the bus ends. Here are the rules no broker is needed for, or that one cannot be
made to show: the sizes an envelope is held to, how a stored copy is compared, what a publish is timed and counted as,
and what the loop keeping the connection does with each way an attempt can fail and a session can end. The broker's
client is a double carrying nats-py's own signatures, and the secret store is the suite's double.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any, cast
from unittest.mock import MagicMock, call, create_autospec

import nkeys
import pytest
import pytest_asyncio
from nats.aio.client import Client
from nats.errors import FlushTimeoutError, NoServersError
from nats.js import JetStreamContext
from nats.js.api import PubAck, RawStreamMsg
from nats.js.errors import NoStreamResponseError, NotFoundError
from opentelemetry import context, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import HistogramDataPoint, InMemoryMetricReader, NumberDataPoint
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

from lakehouse import bus, bus_metrics
from lakehouse.cloud_events import CloudEvent
from lakehouse.supervision import EndProcess, Supervisor
from tests.secret_store_double import SecretStoreDouble


#: The W3C Trace Context specification's own example.
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
TOPIC = "lineage.events.v1"
#: What the store holds for the bus user `catalog`: public test values no bus issued.
CREDENTIAL = {"jwt": "eyJ0eXAiOiJKV1QifQ.catalog", "seed": "SUATESTSEEDCATALOG"}
#: The same credential as the document nats-py reads, spelt out as nsc writes one: each value on the line after its marker.
CREDENTIALS_DOCUMENT = (
    "-----BEGIN NATS USER JWT-----\neyJ0eXAiOiJKV1QifQ.catalog\n------END NATS USER JWT------\n\n"
    "-----BEGIN USER NKEY SEED-----\nSUATESTSEEDCATALOG\n------END USER NKEY SEED------\n"
)

type Points = Callable[[str], list[tuple[dict[str, object], int]]]


def _client(*, stored_as: int = 7) -> MagicMock:
    """A connected client as nats-py's own class specifies it, whose stream stores each publish at ``stored_as``."""
    client = create_autospec(Client, instance=True)
    client.is_connected = True
    stream = create_autospec(JetStreamContext, instance=True)
    stream.publish.return_value = PubAck(stream="LINEAGE", seq=stored_as)
    client.jetstream.return_value = stream
    return client


class _Opened:
    """One connection the seam opened: with which credential, and the callback that tells it the client ended."""

    def __init__(self, credentials: str, ended: Callable[[], Awaitable[None]]) -> None:
        self.credentials = credentials
        self.ended = ended


class _Broker:
    """Stands where the seam opens a connection: answers each open with the next outcome a test queued, a client or the
    failure to raise, and keeps how each was asked for."""

    def __init__(self) -> None:
        self._outcomes: list[MagicMock | Exception] = []
        self.opened: list[_Opened] = []

    def will(self, *outcomes: MagicMock | Exception) -> None:
        """Answer the next opens with ``outcomes``, in order."""
        self._outcomes.extend(outcomes)

    async def open(self, settings: bus.BusSettings, credentials: str, *, ended: Callable[[], Awaitable[None]], said: set[str]) -> Client:
        del settings, said
        self.opened.append(_Opened(credentials, ended))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def _until(holds: Callable[[], bool], *, within: float = 5.0) -> None:
    deadline = time.monotonic() + within
    while not holds():
        assert time.monotonic() < deadline, "the bus session did not get there in time"
        await asyncio.sleep(0.005)


@pytest.fixture
def as_catalog(monkeypatch: pytest.MonkeyPatch, secret_store: SecretStoreDouble) -> SecretStoreDouble:
    """This process is the bus user `catalog`, as the chart names it to a pod, and the store holds that user's credential."""
    monkeypatch.setenv("RASK_BUS_URL", "nats://bus.test:4222")
    monkeypatch.setenv("RASK_BUS_USER", "catalog")
    monkeypatch.setattr(bus, "_PAUSE_SECONDS", (0.0, 0.0))
    secret_store.hold("nats-user-catalog", CREDENTIAL)
    return secret_store


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> _Broker:
    """Where the seam opens its connections for the length of the test."""
    standing = _Broker()
    monkeypatch.setattr(bus, "_open", standing.open)
    return standing


@pytest_asyncio.fixture
async def connected(broker: _Broker, as_catalog: SecretStoreDouble) -> AsyncIterator[tuple[bus.Bus, MagicMock]]:
    """A session connected as `catalog`, and the client it holds."""
    del as_catalog
    client = _client()
    broker.will(client)
    async with Supervisor() as loops, bus.bus_session(loops) as session:
        assert session is not None
        await _until(lambda: session.connected)
        yield session, client


@pytest.fixture
def measured(monkeypatch: pytest.MonkeyPatch) -> Iterator[Points]:
    """What the bus's instruments recorded, by instrument name: each point's attributes and how often it was recorded.

    The module holds its instruments from the process's meter, so they are swapped for ones of the same names under a
    provider this test reads.
    """
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("lance.bus")
    monkeypatch.setattr(bus_metrics, "_refused", meter.create_counter("bus.publish.refused", unit="{event}"))
    monkeypatch.setattr(bus_metrics, "_duration", meter.create_histogram("messaging.client.operation.duration", unit="s"))

    def points(name: str) -> list[tuple[dict[str, object], int]]:
        data = reader.get_metrics_data()
        found: list[tuple[dict[str, object], int]] = []
        for resource in data.resource_metrics if data is not None else ():
            for scope in resource.scope_metrics:
                for metric in (metric for metric in scope.metrics if metric.name == name):
                    for point in metric.data.data_points:
                        if isinstance(point, HistogramDataPoint):
                            found.append((dict(point.attributes or {}), point.count))
                        elif isinstance(point, NumberDataPoint):
                            found.append((dict(point.attributes or {}), int(point.value)))
        return found

    yield points
    provider.shutdown()


def _envelope(**data: Any) -> CloudEvent:
    return CloudEvent(id="e1", source="rask/test", type="rask.test.v1", data=data)


def _envelope_of(size: int) -> CloudEvent:
    """An envelope that goes out as exactly ``size`` bytes."""
    empty = len(json.dumps(_envelope(blob="").as_wire()).encode())
    return _envelope(blob="x" * (size - empty))


def _sent(client: MagicMock) -> list[dict[str, Any]]:
    """Each message the client was handed: its subject, its headers and its body as the envelope it is."""
    return [
        {"subject": call.args[0], "headers": call.kwargs["headers"], "within": call.kwargs["timeout"], "envelope": json.loads(call.args[1])}
        for call in client.jetstream.return_value.publish.await_args_list
    ]


@pytest.mark.parametrize(
    ("size", "published", "warned", "refused"),
    [
        pytest.param(bus.WARN_PAYLOAD_BYTES, 1, [], [], id="at-the-soft-cap"),
        pytest.param(bus.WARN_PAYLOAD_BYTES + 1, 1, ["bus_publish_payload_large"], [], id="past-the-soft-cap"),
        pytest.param(bus.MAX_PAYLOAD_BYTES, 1, ["bus_publish_payload_large"], [], id="at-the-hard-cap"),
        pytest.param(bus.MAX_PAYLOAD_BYTES + 1, 0, [], [({"lance.bus.topic": TOPIC, "lance.bus.reason": "oversize"}, 1)], id="past-the-hard-cap"),
    ],
)
@pytest.mark.asyncio
async def test_an_envelope_is_held_to_the_claim_check_sizes(
    connected: tuple[bus.Bus, MagicMock],
    measured: Points,
    caplog: pytest.LogCaptureFixture,
    size: int,
    published: int,
    warned: list[str],
    refused: list[tuple[dict[str, object], int]],
) -> None:
    """Up to the soft cap an envelope goes out and nothing is said; past it, it goes out and is logged; past the hard
    cap nothing reaches the bus, so no publish span and no acknowledgement exists for it, and the counter is the
    refusal's only trace."""
    session, client = connected

    with caplog.at_level(logging.WARNING, logger=bus.__name__), contextlib.suppress(ValueError):
        await bus.publish_event(session, _envelope_of(size), topic=TOPIC, timeout_seconds=5)

    assert (len(_sent(client)), [record.message for record in caplog.records], measured("bus.publish.refused")) == (published, warned, refused)


@pytest.mark.asyncio
async def test_a_publish_inside_a_span_carries_its_trace_context_in_the_envelope(connected: tuple[bus.Bus, MagicMock]) -> None:
    """The envelope goes out whole, with ``traceparent`` and ``tracestate`` beside its own attributes, as CloudEvents'
    distributed tracing extension names them, and nothing else."""
    session, client = connected
    envelope = _envelope(run="r1")
    span = SpanContext(
        trace_id=0x0AF7651916CD43DD8448EB211C80319C,
        span_id=0xB7AD6B7169203331,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=TraceState([("vendor", "1")]),
    )
    token = context.attach(trace.set_span_in_context(NonRecordingSpan(span)))

    try:
        await bus.publish_event(session, envelope, topic=TOPIC, timeout_seconds=5)
    finally:
        context.detach(token)

    assert [sent["envelope"] for sent in _sent(client)] == [envelope.as_wire() | {"traceparent": TRACEPARENT, "tracestate": "vendor=1"}]


@pytest.mark.asyncio
async def test_a_bus_that_does_not_acknowledge_a_publish_in_time_raises_a_timeout(connected: tuple[bus.Bus, MagicMock]) -> None:
    session, client = connected

    async def never(*_args: object, **_kwargs: object) -> PubAck:
        await asyncio.sleep(60)
        raise AssertionError("unreachable")

    client.jetstream.return_value.publish.side_effect = never

    with pytest.raises(TimeoutError):
        await bus.publish_event(session, _envelope(run="r1"), topic=TOPIC, timeout_seconds=0.05)


@pytest.mark.asyncio
async def test_a_publish_the_bus_refuses_is_not_delivered_and_keeps_its_cause(connected: tuple[bus.Bus, MagicMock]) -> None:
    """No stream captures the topic: the bus's own refusal, told as the seam's one error with nats-py's chained."""
    session, client = connected
    client.jetstream.return_value.publish.side_effect = NoStreamResponseError()

    with pytest.raises(bus.BusUnavailableError, match=TOPIC) as refused:
        await bus.publish_event(session, _envelope(run="r1"), topic=TOPIC, timeout_seconds=5)

    assert isinstance(refused.value.__cause__, NoStreamResponseError)


@pytest.mark.parametrize(
    "stored",
    [
        pytest.param(RawStreamMsg(seq=41, data=json.dumps(_envelope(run="r1", attempt=1.0).as_wire()).encode()), id="its-number-spelt-as-a-float"),
        pytest.param(RawStreamMsg(seq=41, data=b"not an envelope"), id="bytes-that-are-no-envelope"),
        pytest.param(RawStreamMsg(seq=41, data=b'["an", "array"]'), id="json-that-is-no-envelope"),
        pytest.param(NotFoundError(), id="nothing-stored-at-that-sequence"),
    ],
)
@pytest.mark.asyncio
async def test_a_duplicate_acknowledgement_is_believed_only_for_a_stored_copy_of_the_event_as_it_was_signed(
    connected: tuple[bus.Bus, MagicMock], stored: RawStreamMsg | Exception
) -> None:
    """The stored message is read back at the sequence the acknowledgement names, and the event counts as delivered only
    when that message carries it in the same spelling: a signature over ``1`` does not verify over ``1.0``, which two
    parsed documents would call equal. Anything else stored there, or nothing, leaves the event staged."""
    session, client = connected
    stream = client.jetstream.return_value
    stream.publish.return_value = PubAck(stream="LINEAGE", seq=41, duplicate=True)
    stream.get_msg.side_effect = [stored]

    with pytest.raises(bus.BusUnavailableError, match="e1"):
        await bus.publish_event(session, _envelope(run="r1", attempt=1), topic=TOPIC, timeout_seconds=5)


@pytest.mark.parametrize(
    ("refusal", "ended"),
    [
        pytest.param(None, {}, id="delivered"),
        pytest.param(NoStreamResponseError(), {"error.type": "BusUnavailableError"}, id="not-delivered"),
    ],
)
@pytest.mark.asyncio
async def test_each_publish_is_timed_under_its_topic_and_how_it_ended(
    connected: tuple[bus.Bus, MagicMock], measured: Points, refusal: Exception | None, ended: dict[str, str]
) -> None:
    """One sample per publish, under OpenTelemetry's messaging attributes: its count is the traffic and its
    ``error.type`` the publishes the bus did not take."""
    session, client = connected
    client.jetstream.return_value.publish.side_effect = refusal

    with contextlib.suppress(bus.BusUnavailableError):
        await bus.publish_event(session, _envelope(run="r1"), topic=TOPIC, timeout_seconds=5)

    assert measured("messaging.client.operation.duration") == [
        ({"messaging.system": "nats", "messaging.operation.name": "publish", "messaging.destination.name": TOPIC} | ended, 1)
    ]


@pytest.mark.parametrize(
    ("said", "failure", "logged"),
    [
        pytest.param(
            ConnectionRefusedError("no server at bus.test"),
            NoServersError(),
            ["bus_error", "bus_disconnected", "bus_not_connected"],
            id="no-server-takes-the-connection",
        ),
        pytest.param(None, nkeys.ErrInvalidSeed(), ["bus_disconnected", "bus_not_connected"], id="the-store-holds-a-seed-that-signs-nothing"),
    ],
)
@pytest.mark.asyncio
async def test_a_connection_that_cannot_be_opened_is_tried_again_and_each_fault_reported_once(
    monkeypatch: pytest.MonkeyPatch,
    as_catalog: SecretStoreDouble,
    caplog: pytest.LogCaptureFixture,
    said: Exception | None,
    failure: Exception,
    logged: list[str],
) -> None:
    """Whatever kept an attempt from a connection, the next attempt is the only way to one: a bus that takes none, and a
    credential nats-py cannot sign with (it raises that outside its own errors), each leave the process running and
    unconnected. What the bus says on every attempt of one outage is logged once, and so are the attempts' closed
    clients and the outage; the session's own closing of its connection, at the end, is not told as a disconnection."""
    del as_catalog
    outcomes: list[Exception | None] = [failure, failure, None]
    clients: list[MagicMock] = []

    def client() -> MagicMock:
        made = _client()
        outcome = outcomes[len(clients)]

        async def connect(**options: Any) -> None:
            # nats-py tells of every client it closes, one whose connect failed included.
            made.close.side_effect = options["disconnected_cb"]
            if outcome is not None:
                if said is not None:
                    await options["error_cb"](said)
                raise outcome

        made.connect.side_effect = connect
        clients.append(made)
        return made

    monkeypatch.setattr(bus, "Client", client)
    ended: list[str] = []

    with caplog.at_level(logging.WARNING, logger=bus.__name__):
        # A loop that ends would end the process: recorded here, so that it fails this test and not the run.
        async with Supervisor(end=cast(EndProcess, lambda event, **_: ended.append(event))) as loops, bus.bus_session(loops) as session:
            assert session is not None
            await _until(lambda: session.connected)

    assert (ended, len(clients), [record.message for record in caplog.records]) == ([], 3, logged)


@pytest.mark.asyncio
async def test_a_store_that_holds_no_credential_for_the_user_leaves_the_bus_unconnected_and_is_asked_again(
    broker: _Broker, as_catalog: SecretStoreDouble, caplog: pytest.LogCaptureFixture
) -> None:
    """A user the seed has not issued yet, or issued without its JWT: nothing is opened on half a credential, and the
    store is read again until it holds one, which the connection is then opened with."""
    as_catalog.hold("nats-user-catalog", {"seed": CREDENTIAL["seed"]})
    broker.will(_client())

    with caplog.at_level(logging.WARNING, logger=bus.__name__):
        async with Supervisor() as loops, bus.bus_session(loops) as session:
            assert session is not None
            await _until(lambda: [record.message for record in caplog.records] == ["bus_not_connected"])
            opened_on_half = len(broker.opened)
            as_catalog.hold("nats-user-catalog", CREDENTIAL)
            await _until(lambda: session.connected)

    assert (opened_on_half, [opened.credentials for opened in broker.opened]) == (0, [CREDENTIALS_DOCUMENT])


@pytest.mark.asyncio
async def test_a_process_whose_connection_is_not_up_says_so_and_publishes_nothing(broker: _Broker, as_catalog: SecretStoreDouble) -> None:
    """The verdict a readiness report shows for the bus, and what a publish is answered, while no server takes the connection."""
    del as_catalog
    broker.will(*[NoServersError() for _ in range(10_000)])

    async with Supervisor() as loops, bus.bus_session(loops) as session:
        assert session is not None
        await _until(lambda: len(broker.opened) > 0)
        with pytest.raises(ConnectionError, match="not connected"):
            await session.answering()
        with pytest.raises(bus.BusUnavailableError, match="not connected"):
            await bus.publish_event(session, _envelope(run="r1"), topic=TOPIC, timeout_seconds=5)


@pytest.mark.parametrize(
    ("up", "flushed", "flushes"),
    [
        pytest.param(True, None, [call(timeout=3)], id="the-bus-takes-what-was-sent"),
        pytest.param(True, FlushTimeoutError(), [call(timeout=3)], id="the-bus-does-not-answer"),
        pytest.param(True, ConnectionResetError(), [call(timeout=3)], id="the-socket-is-gone"),
        pytest.param(False, None, [], id="the-connection-is-not-up"),
    ],
)
@pytest.mark.asyncio
async def test_a_session_that_ends_closes_its_connection_within_a_bound_and_opens_no_other(
    broker: _Broker, as_catalog: SecretStoreDouble, up: bool, flushed: Exception | None, flushes: list[Any]
) -> None:
    """The bus is given a bounded time to take what was sent on a connection that is up, and the connection is closed
    whether or not it answered; nats-py then calls the closed callback, which must not be read as the bus ending the
    session."""
    del as_catalog
    client = _client()
    broker.will(client)
    client.flush.side_effect = flushed

    async def closed() -> None:
        await broker.opened[0].ended()

    client.close.side_effect = closed

    async with Supervisor() as loops, bus.bus_session(loops) as session:
        assert session is not None
        await _until(lambda: session.connected)
        client.is_connected = up

    assert (client.flush.await_args_list, client.close.await_count, len(broker.opened)) == (flushes, 1, 1)


@pytest.mark.parametrize("lands", [pytest.param("as-its-loop-ends", id="as-its-loop-ends"), pytest.param("as-it-flushes", id="as-it-flushes")])
@pytest.mark.asyncio
async def test_a_session_cancelled_as_it_ends_still_closes_its_connection(broker: _Broker, as_catalog: SecretStoreDouble, lands: str) -> None:
    """A cancellation that lands while the session ends is its task's to end on, and the connection is closed first all
    the same: while the session waits for its loop to end, and while it waits for the bus to take what was sent."""
    del as_catalog
    client = _client()
    broker.will(client)
    flushing = asyncio.Event()

    async def flush(**_bound: object) -> None:
        flushing.set()
        await asyncio.sleep(60)

    if lands == "as-it-flushes":
        client.flush.side_effect = flush

    async def hold_and_leave() -> None:
        async with Supervisor() as loops, bus.bus_session(loops) as session:
            assert session is not None
            await _until(lambda: session.connected)
            if lands == "as-its-loop-ends":
                asyncio.get_running_loop().call_soon(leaving.cancel)

    leaving = asyncio.create_task(hold_and_leave())
    if lands == "as-it-flushes":
        await flushing.wait()
        leaving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leaving

    assert client.close.await_count == 1


@pytest.mark.asyncio
async def test_a_session_that_ends_leaves_a_connection_whose_socket_is_never_let_go(
    monkeypatch: pytest.MonkeyPatch, broker: _Broker, as_catalog: SecretStoreDouble, caplog: pytest.LogCaptureFixture
) -> None:
    """nats-py closes by waiting for the socket's buffer to empty, which a peer that died without closing never lets
    it: the session ends inside its bound all the same, and says what it left."""
    del as_catalog
    monkeypatch.setattr(bus, "_CLOSE_SECONDS", 0.05)
    client = _client()
    broker.will(client)

    async def never() -> None:
        await asyncio.sleep(60)

    client.close.side_effect = never

    with caplog.at_level(logging.WARNING, logger=bus.__name__):
        async with asyncio.timeout(5), Supervisor() as loops, bus.bus_session(loops) as session:
            assert session is not None
            await _until(lambda: session.connected)

    assert [record.message for record in caplog.records] == ["bus_close_abandoned"]


@pytest.mark.asyncio
async def test_a_session_that_ends_while_it_connects_ends_whatever_releasing_the_connection_raises(
    monkeypatch: pytest.MonkeyPatch, as_catalog: SecretStoreDouble
) -> None:
    """A connect cancelled part-way releases its socket, and a release that raises must not stand in for the
    cancellation: the loop would take it for one more failed attempt and never end, and the session with it."""
    del as_catalog
    client = create_autospec(Client, instance=True)
    connecting = asyncio.Event()

    async def connect(**_options: object) -> None:
        connecting.set()
        await asyncio.sleep(60)

    client.connect.side_effect = connect
    client.close.side_effect = OSError("the socket was already gone")
    monkeypatch.setattr(bus, "Client", lambda: client)

    async with asyncio.timeout(5), Supervisor() as loops, bus.bus_session(loops) as session:
        assert session is not None
        await connecting.wait()

    assert client.close.await_count == 1


@pytest.mark.asyncio
async def test_a_process_the_chart_names_no_bus_holds_no_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RASK_BUS_URL", raising=False)
    monkeypatch.delenv("RASK_BUS_USER", raising=False)

    async with Supervisor() as loops, bus.bus_session(loops) as session:
        held = session

    assert held is None


@pytest.mark.parametrize(
    "user",
    [
        pytest.param("", id="no-user"),
        pytest.param("Catalog_1", id="a-user-the-store-can-hold-no-credential-for"),
    ],
)
def test_a_bus_named_without_a_user_it_can_connect_as_is_refused_at_start(monkeypatch: pytest.MonkeyPatch, user: str) -> None:
    """A half-configured bus must not read as a process that simply publishes nowhere, and a user whose name is no
    secret name has no credential the store could hold: both are refused when the settings are read, not in the loop
    that connects."""
    monkeypatch.setenv("RASK_BUS_URL", "nats://bus.test:4222")
    monkeypatch.setenv("RASK_BUS_USER", user)

    with pytest.raises(ValueError, match="RASK_BUS_USER"):
        bus.BusSettings()
