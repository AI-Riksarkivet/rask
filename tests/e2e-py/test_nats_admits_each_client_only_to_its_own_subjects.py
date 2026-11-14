"""Each NATS client reaches exactly its own subjects, a client with no credential reaches nothing, and each service takes
its deliveries on its own connection ([[XC-078]], [[XC-199]]).

Closes-when clause 1 ("a publish from a pod without its app's credential is refused on every cascade, control and
lineage subject"), proved offline: the nats-server the NATS fixture deploys (deploy/fixtures/nats, a prerequisite rask
never installs) in operator mode with a MEMORY resolver and JetStream, test-only keys minted with nsc, and one user per
row of the permission table the chart's dev seed issues (read off the render, so a values.yaml `nats.auth.flagged`
grant counts only while its flag is on).

The table is held to a declaration written independently of it and to what the run measures, because a table checked
only against itself can never be too broad. `PUBLISHERS` is who may publish each data subject, read off every publish
call site; a table that lets anyone else publish a subject fails, and so does one that withholds one. Every call a
client makes is read off the broker's trace: a call the table refuses fails the run, and so does a grant of a user
this module drives that no call of that user used.

Each service's own connection is the seam itself: the real `lakehouse.bus.Bus`, connected as the bus user the render
names that service, with the credential a stand-in of the secret store holds for it.

* It publishes each of its topics once under its event's id, under an inbox no other user may read; an id another
  publisher stored a message under is not taken as delivered; a bus it cannot reach fails its publishes at once and is
  connected to again, by nats-py through a short cut and by the seam's own loop past it; and a credential the bus ends
  is replaced by the one the store holds then, with no restart.
* Lineage pulls its ingest consumer (`Bus.consume`): a delivery is handled once under the trace it was published
  under; a body no parser reads is handed on and ends no lane; one its handling asks for again comes back after the
  lane's delay and is parked on its last; one whose
  handling is cut is handed back at once; two replicas share the consumer within its `max_ack_pending`; a replica
  handles no more at once than its own lane allows; a consumer the stream Job has not declared yet is waited for.
  Lineage may pull nothing else and create, edit or delete no consumer.
* The catalog follows its control topic (`Bus.follow`): every replica gets every event once and in order, from where it
  began, through a handling that fails, messages whose publisher gave them the headers of a bus status, and a burst;
  it leaves no consumer on the stream and may create none anywhere.
* Through an outage longer than nats-py's own reconnects, a pulling lane and a following one deliver again on the rebuilt
  connection, nothing lost and nothing handled twice.

The chart's stream Job runs as `admin`, both phases with their rendered scripts, on a bus prepared as the previous
revision left it: the declare phase creates and converges every stream and consumer, starts lineage's ingest consumer
after the last event the subscription it replaces acknowledged, and edits a changed lane in place; the retire phase
removes what this release declares no longer and what the previous revision's subscriptions held, leaves what another
subscriber holds, removes no stream, and removes nothing it could not read.

Run by `dagger call nats-auth` (`make nats-auth`), which provides the binaries and selects this file by path.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import subprocess
from collections.abc import AsyncGenerator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from itertools import pairwise
from pathlib import Path
from typing import Any

import nats
import pytest
import respx
import yaml
from nats.js.errors import APIError
from nats_auth_rig import (
    VALUES_ENV,
    Broker,
    Client,
    Credential,
    Link,
    Mint,
    Permissions,
    Tools,
    client_calls,
    issue,
    matches,
    mint,
    run_cli,
    run_stream_job,
    stream_job_script,
    unused_grants,
    violations,
)
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from pydantic import BaseModel, ConfigDict

from lakehouse import bus as seam
from lakehouse.bus import Bus, BusSettings, BusUnavailableError, Follow, bus_session, publish_event
from lakehouse.bus_delivery import MAX_ENVELOPE_DEPTH, Delivery, Handle, Lane, Verdict
from lakehouse.cloud_events import STRUCTURED_CONTENT_TYPE, TRACE_ATTRIBUTES, CloudEvent
from lakehouse.supervision import Supervisor
from lineage.api.ingest_lane import CONSUMER as INGEST_CONSUMER
from lineage.api.ingest_lane import STREAM as INGEST_STREAM
from tests.secret_store_double import SecretStoreDouble
from tests.unit.chart_render import DEFAULT_ARGS, containers, env_of, nats_users, render, render_fixture


pytestmark = pytest.mark.nats_auth

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[2]
#: The rig's own observer, every JetStream call and every ack granted: it reads stream and consumer state and lays out
#: the bus the previous revision left, so no table user's call set carries the test's bookkeeping. It is minted beside
#: the table and is not a row of it.
RIG = "rask-test-rig"
#: `admin`'s grants without `$JS.API.CONSUMER.INFO`, so every consumer read the stream Job makes fails as a read during a
#: JetStream leader move does, while it may still list and delete ([[LH-343]]). Minted beside the table, not a row of it.
BLIND_ADMIN = "rask-test-blind-admin"
#: How many times the stream Job runs before the lane gives up on it, inside the chart's `backoffLimit` of 20.
_JOB_RUNS = 3
#: The topics a service publishes on its own connection: its lineage events (`lakehouse.outbox`) and the control lane's.
OWN_CONNECTION_TOPICS = ("lineage.events.v1", "catalog.control.v1")
LINEAGE_TOPIC, CONTROL_TOPIC = OWN_CONNECTION_TOPICS
#: The stream the catalog's control events are kept on, which each catalog replica follows (`catalog.api.control_lane`).
CONTROL_STREAM = "CATALOG_CONTROL"
#: A bus user of the rig's own whose first credential the bus ends: issued for a minute, then issued again in the store.
#: Minted beside the table and not a row of it: it may publish lineage, read a stored message back, and receive under an
#: inbox of its own.
REISSUED = "rask-test-reissued"
#: A bus user of the rig's own whose first credential grants it only the shared inbox, as one issued before its row gave it
#: an inbox of its own would: the bus takes the connection and would hand it no acknowledgement. Minted beside the table.
UNHEARD = "rask-test-unheard"
#: How long `REISSUED`'s first credential is good for (nsc's least), and how much longer the seam may take to be told and
#: to connect again.
_EXPIRY_SECONDS = 60.0
_EXPIRY_SLACK_SECONDS = 30.0
#: The seam's reconnect budget in this run, shortened from the service's (ten attempts two seconds apart, then a pause of
#: 2 to 15 s) so that a cut of seconds outlasts nats-py's own reconnects: attempts, seconds apart, and the pause's bounds.
#: `lakehouse.bus` takes them from no setting, so the run sets the module's own.
_RECONNECT_ATTEMPTS = 3
_RECONNECT_WAIT_SECONDS = 1
_PAUSE_SECONDS = (0.5, 1.0)

#: Lineage's ingest consumer as the run converges it (`services.lineage.delivery.ackWaitSeconds`,
#: `services.lineage.deliveriesInFlight`): an ack wait that leaves a handling ten seconds, and a consumer that hands out
#: two deliveries at once, so a cap reached and a handing back told from an ack wait run out fit in the run.
_ACK_WAIT_SECONDS = 20
_DELIVERIES_IN_FLIGHT = 2
#: The lane the run's lineage replicas pull with: three deliveries, two seconds apart, and more handled at once by each
#: replica than the consumer hands out, so the consumer's own cap is what holds them.
_LANE = Lane(
    stream=INGEST_STREAM, consumer=INGEST_CONSUMER, attempts=3, retry_seconds=2.0, ack_wait_seconds=_ACK_WAIT_SECONDS, in_flight=2 * _DELIVERIES_IN_FLIGHT
)
#: The render's values the stream Job's later runs are given: the lane converged above, and then the commit lane retired.
_CHANGED_LANE = (
    *("--set", f"services.lineage.delivery.ackWaitSeconds={_ACK_WAIT_SECONDS}"),
    *("--set", f"services.lineage.deliveriesInFlight={_DELIVERIES_IN_FLIGHT}"),
)
_ARRIVAL_RETIRED = ("--set", "maintenance.arrivalEnabled=false")
#: The release mark the stream Job writes on each consumer it declares, and another release's.
_MARK = "rask.dev/release"
_RELEASE = "rask"
#: How many messages the previous revision's lineage subscription had acknowledged, of how many on the stream.
_ACKED_BEFORE, _HELD_BEFORE = 3, 5
#: How many control events a burst carries, and how large each is.
_BURST, _BURST_BYTES = 2000, 2048
#: Headers a user the bus admits to a topic can give a message, each of which a client that reads headers as the bus's
#: word takes for a status of the bus's own (measured on nats-py 2.16.0: an idle heartbeat, an empty pull, a failure),
#: with the body each is published with.
_STATUS_HEADED: tuple[tuple[bytes, dict[str, str]], ...] = (
    (b"", {"Status": "100", "Description": "Idle Heartbeat"}),
    (b"", {"Status": "408", "Description": "Request Timeout"}),
    (b"no envelope", {"Status": "500"}),
)
#: How long a replica bounded to one delivery at a time is watched while it holds one.
_ONE_AT_A_TIME_SECONDS = 3.0
#: Bodies a user the bus admits to the topic can publish and no reader may trust: one nested deeper than a JSON parser
#: reads, one nested a level past what the seam reads as an envelope, and an envelope whose id is no text.
_HOSTILE_BODIES = (b"[" * 20_000, b'{"in":' * MAX_ENVELOPE_DEPTH + b"{}" + b"}" * MAX_ENVELOPE_DEPTH, b'{"id": ["no", "text"], "data": {}}')
#: How long the outage leg waits, its links down, for nats-py to give its connections up and the seam's loop to open new
#: clients: the shortened reconnect budget above, with room for a loaded host.
_OUTAGE_WITHIN = 30.0


#: Who may publish each data subject; every other table user must be refused on it.
#:
#: lineage: catalog/core/lineage_emit.py, lineage/services/staged.py (recovered signed bytes), maintenance/core/
#: lineage_emit.py. control: catalog's control_emit and control_relay.py, plus maintenance under its flag. maintenance's
#: tasks, schedules and claims on tables: maintenance/services/queue.py, as the worker's own user. store.events: the
#: development store's change notifications (deploy/fixtures/rustfs), as its own user. dlq.*: a subscriber outside the
#: release parks there on its own name; nothing of the lakehouse's publishes there.


def _values() -> Path:
    return Path(os.environ.get(VALUES_ENV) or REPO / "chart" / "values.yaml")


def _control_emitters(values: Path) -> frozenset[str]:
    """Maintenance announces on catalog.control.v1 only while the flag that grants it is on (maintenance.controlEmit,
    false in the chart's values)."""
    chart = yaml.safe_load((REPO / "chart" / "values.yaml").read_text()) or {}
    given = yaml.safe_load(values.read_text()) or {}
    on = (given.get("maintenance") or {}).get("controlEmit", (chart.get("maintenance") or {}).get("controlEmit"))
    return frozenset({"maintenance"}) if on else frozenset()


PUBLISHERS: dict[str, frozenset[str]] = {
    LINEAGE_TOPIC: frozenset({"catalog", "lineage", "maintenance"}),
    CONTROL_TOPIC: frozenset({"catalog", *_control_emitters(_values())}),
    "dlq.lineage.events": frozenset(),
    "maintenance.task.probe": frozenset({"maintenance-worker"}),
    "maintenance.schedule.probe": frozenset({"maintenance-worker"}),
    "$KV.MAINTENANCE_CLAIMS.probe": frozenset({"maintenance-worker"}),
    "store.events": frozenset({"object-store"}),
}
#: The clients beside the services the table must also name: the stream Job, nats-box, maintenance's worker and the dev
#: store.
OTHER_CLIENTS = frozenset({"admin", "monitor", "maintenance-worker", "object-store"})
#: The users every grant of which this module's legs exercise; the worker's queue calls are its own module's.
DRIVEN = frozenset({"admin", "monitor", "catalog", "lineage", "maintenance", "object-store"})
#: Calls outside a service's row, each of which the broker must refuse: another service's consumer pulled, a consumer
#: created, edited or deleted, a durable consumer where only a connection's own may be made, and one on another stream.
REFUSED_CALLS: dict[str, tuple[str, ...]] = {
    "lineage": (
        "$JS.API.CONSUMER.MSG.NEXT.MAINTENANCE_TASKS.maintenance-worker",
        "$JS.API.CONSUMER.MSG.NEXT.LINEAGE.lane-stranger",
        f"$JS.API.CONSUMER.CREATE.LINEAGE.lane-own.{LINEAGE_TOPIC}",
        f"$JS.API.CONSUMER.CREATE.LINEAGE.{INGEST_CONSUMER}.{LINEAGE_TOPIC}",
        "$JS.API.CONSUMER.DURABLE.CREATE.LINEAGE.lane-own",
        "$JS.API.CONSUMER.CREATE.LINEAGE",
        f"$JS.API.CONSUMER.DELETE.LINEAGE.{INGEST_CONSUMER}",
    ),
    "catalog": (
        f"$JS.API.CONSUMER.CREATE.{CONTROL_STREAM}",
        f"$JS.API.CONSUMER.CREATE.{CONTROL_STREAM}.lane-own.{CONTROL_TOPIC}",
        f"$JS.API.CONSUMER.DURABLE.CREATE.{CONTROL_STREAM}.lane-own",
        "$JS.API.CONSUMER.CREATE.LINEAGE",
        "$JS.API.CONSUMER.CREATE.DLQ",
    ),
}
#: JetStream's answer to a publish on a subject already holding the one message its stream keeps.
_ONE_PER_SUBJECT = 10077


def _is_data_subject(subject: str) -> bool:
    """A subject that carries an application's data: not the JetStream API, not a reply inbox, and not $SYS.REQ.USER.INFO,
    the request in which a client asks the server who it is (monitor's `nats account info`)."""
    return not subject.startswith(("$JS.", "_INBOX")) and subject != "$SYS.REQ.USER.INFO"


def _lane_runs_the_charts_images(tools: Tools, docs: tuple[dict, ...]) -> list[str]:
    """Every binary the lane runs comes from an image the estate deploys: rask's render, or the NATS fixture's server."""
    deployed = {c["image"] for _, _, c in containers((*docs, *render_fixture("nats")))}
    return [
        f"the lane runs {tool} from {image}, which neither the chart nor a fixture deploys"
        for tool, image in sorted(tools.images.items())
        if image not in deployed
    ]


def _table_names_every_client(table: dict[str, Permissions], services: list[str]) -> list[str]:
    """The table issues one user per client and none beyond: each service the render names a bus user, and the others."""
    clients = OTHER_CLIENTS | set(services)
    findings = [f"no table user for {user}, which the render needs a credential for" for user in sorted(clients - set(table))]
    return findings + [f"the table issues {user}, which no client connects as" for user in sorted(set(table) - clients)]


def _data_grants_beyond_the_publishers(table: dict[str, Permissions]) -> list[str]:
    """A user's every data-subject grant must cover a subject `PUBLISHERS` gives it: a grant past them is one no call site
    makes."""
    return [
        f"{user} may publish {grant}, which covers no data subject PUBLISHERS gives it"
        for user, permissions in sorted(table.items())
        for grant in permissions.publish
        if _is_data_subject(grant) and not any(user in owners and matches(grant, subject) for subject, owners in PUBLISHERS.items())
    ]


def _services(docs: tuple[dict, ...]) -> list[str]:
    """The bus users the render names its services, each of which publishes on a connection of its own."""
    return sorted({env["RASK_BUS_USER"] for _, _, container in containers(docs) if "RASK_BUS_USER" in (env := env_of(container))})


def _lane_event(**data: str) -> CloudEvent:
    """An event of this run's own, under an id no other event carries."""
    return CloudEvent(id=f"lane-{secrets.token_hex(12)}", source="rask/lane", type="rask.test.lane.v1", data=data)


async def _until(holds: Callable[[], bool], within: float) -> bool:
    """Whether ``holds`` came true within ``within`` seconds."""
    deadline = asyncio.get_running_loop().time() + within
    while not holds():
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.1)
    return True


async def _up(session: Bus) -> bool:
    """The seam's own readiness verdict of its connection."""
    try:
        await session.answering()
    except ConnectionError:
        return False
    return True


async def _settles(session: Bus, *, up: bool, within: float) -> bool:
    """Whether the seam's connection came ``up``, or went down, within ``within`` seconds."""
    deadline = asyncio.get_running_loop().time() + within
    while await _up(session) is not up:
        if asyncio.get_running_loop().time() > deadline:
            return False
        await asyncio.sleep(0.1)
    return True


@asynccontextmanager
async def _connected_as(user: str, url: str) -> AsyncGenerator[Bus]:
    """A service's own connection to the bus, opened as the chart names it to a pod (`lakehouse.bus.bus_session`) and
    up: the seam reads ``user``'s credential from the store's stand-in and connects with it."""
    settings = BusSettings.model_validate({"RASK_BUS_URL": url, "RASK_BUS_USER": user})
    async with Supervisor() as loops, bus_session(loops, settings) as session:
        assert session is not None, "the seam opens no connection for a process named a bus"
        assert await _settles(session, up=True, within=20), f"the seam never connected to the bus as {user}"
        yield session


def _headers(message: dict[str, Any]) -> dict[str, str]:
    """A stored message's NATS headers, which the stream's API answers as one base64 block of `Name: value` lines under a
    version line."""
    lines = base64.b64decode(message.get("hdrs", "")).decode().split("\r\n")[1:]
    return {name.strip(): value.strip() for name, _, value in (line.partition(":") for line in lines if line)}


async def _stream_of(rig: Client, topic: str) -> str:
    """The one stream that captures ``topic``."""
    (stream,) = (await rig.request("$JS.API.STREAM.NAMES", {"subject": topic}) or {}).get("streams") or [""]
    return stream


async def _kept_under(rig: Client, stream: str, since: int, message_id: str) -> list[dict[str, Any]]:
    """The messages ``stream`` took after sequence ``since`` under ``message_id``."""
    last = (await _last_seqs(rig)).get(stream, since)
    kept = []
    for seq in range(since + 1, last + 1):
        message = ((await rig.request(f"$JS.API.STREAM.MSG.GET.{stream}", {"seq": seq})) or {}).get("message") or {}
        if _headers(message).get("Nats-Msg-Id") == message_id:
            kept.append(message)
    return kept


async def _published_once_under_its_id(session: Bus, user: str, topic: str, rig: Client) -> list[str]:
    """One event published twice on ``user``'s own connection, as its producer and then its relay would: the stream keeps
    it once, under the event's id and as the structured envelope, and the second publish, which the stream answers as a
    duplicate, is taken as delivered once the seam has read the stored message back."""
    stream = await _stream_of(rig, topic)
    since = (await _last_seqs(rig)).get(stream, -1)
    event = _lane_event(probe=user)
    try:
        await publish_event(session, event, topic=topic, timeout_seconds=5)
        await publish_event(session, event, topic=topic, timeout_seconds=5)
    except (BusUnavailableError, TimeoutError) as exc:
        return [f"{user} publishes {topic} on its own connection, and the bus did not take it: {type(exc).__name__}: {exc}"]
    kept = await _kept_under(rig, stream, since, event.id)
    if len(kept) != 1:
        return [f"{stream} keeps {len(kept)} messages under the id of the one event {user} published twice on {topic}"]
    stored = json.loads(base64.b64decode(kept[0]["data"]))
    # The envelope carries the publish's own trace beside the event, which each publish stamps afresh.
    carried = (_headers(kept[0]).get("Content-Type"), {name: value for name, value in stored.items() if name not in TRACE_ATTRIBUTES})
    return [] if carried == (STRUCTURED_CONTENT_TYPE, event.as_wire()) else [f"{stream} keeps {user}'s event on {topic} as {carried}"]


async def _an_id_another_message_holds_is_not_delivered(session: Bus, user: str, topic: str, squatter: Client) -> list[str]:
    """A message id names nobody, so a publisher of ``topic`` can store a message of its own under an event's id first, and
    the stream then drops the event and still acknowledges it. The seam reads the stored message back, finds another
    event, and does not count its own delivered, so the event stays staged for its relay."""
    event = _lane_event(probe=user)
    other = {"specversion": "1.0", "id": event.id, "source": "rask/lane", "type": "rask.test.lane.v1", "data": {"probe": "another publisher"}}
    await squatter.js.publish(topic, json.dumps(other).encode(), timeout=5, headers={"Nats-Msg-Id": event.id})
    try:
        await publish_event(session, event, topic=topic, timeout_seconds=5)
    except BusUnavailableError:
        return []
    return [f"{user}'s event on {topic} was taken as delivered while the stream holds another message under its id"]


async def _services_publish_as_themselves(url: str, services: list[str], rig: Client, probes: dict[str, Client]) -> list[str]:
    """Each service on the seam its events are published with, as the bus user the render names it: every topic it
    publishes, kept once under its event's id, and an id another publisher of the topic took first."""
    findings: list[str] = []
    for user in services:
        async with _connected_as(user, url) as session:
            for topic in (topic for topic in OWN_CONNECTION_TOPICS if user in PUBLISHERS[topic]):
                findings += await _published_once_under_its_id(session, user, topic, rig)
                squatter = probes[min(PUBLISHERS[topic] - {user}, default=user)]
                findings += await _an_id_another_message_holds_is_not_delivered(session, user, topic, squatter)
    return findings


async def _a_service_outlasts_a_bus_it_cannot_reach(broker: Broker, user: str, reads: respx.Route, rig: Client) -> list[str]:
    """``user``'s own connection through a `Link` the test cuts. While the bus cannot be reached a publish fails at once,
    so its event stays staged and no caller waits. After a short cut nats-py reconnects by itself, on the credential it
    holds. Past its own attempts it closes for good, and the seam's loop opens another client with a fresh read of the
    credential, so the process publishes again with no restart."""
    findings: list[str] = []
    topic, clock = LINEAGE_TOPIC, asyncio.get_running_loop().time
    link = Link(broker.port)
    await link.start()
    try:
        async with _connected_as(user, link.url) as session:
            read_once = reads.call_count
            await link.stop()
            if not await _settles(session, up=False, within=10):
                findings.append(f"{user}'s connection still answers as up 10 s after its bus was cut off")
            asked = clock()
            try:
                await publish_event(session, _lane_event(probe=user), topic=topic, timeout_seconds=30)
            except BusUnavailableError:
                if (waited := clock() - asked) > 1:
                    findings.append(f"a publish with no connection to the bus took {waited:.1f} s to fail")
            except TimeoutError:
                findings.append("a publish with no connection to the bus waited out its timeout")
            else:
                findings.append("a publish with no connection to the bus was taken as delivered")
            await link.start()
            if not await _settles(session, up=True, within=15) or reads.call_count != read_once:
                findings.append(f"nats-py did not reconnect {user} by itself after a short cut ({reads.call_count - read_once} more credential reads)")
            findings += await _published_once_under_its_id(session, user, topic, rig)

            await link.stop()
            if not await _until(lambda: reads.call_count > read_once, 30):
                findings.append(f"the seam never read {user}'s credential again once nats-py had given its connection up")
            await link.start()
            if not await _settles(session, up=True, within=20):
                findings.append(f"the seam did not connect {user} again once its bus was back")
            else:
                findings += await _published_once_under_its_id(session, user, topic, rig)
    finally:
        await link.stop()
    return findings


async def _a_credential_the_bus_ends_is_replaced_by_the_stores_current_one(
    tools: Tools, home: Path, broker: Broker, store: SecretStoreDouble, rig: Client
) -> list[str]:
    """A credential good for a minute, issued again in the store meanwhile: the bus ends the session when the first runs
    out, nats-py closes that client for good, and the seam connects again with the credential the store holds by then,
    so the process publishes again with no restart."""
    topic = LINEAGE_TOPIC
    grants = Permissions(publish=(topic, f"$JS.API.STREAM.MSG.GET.{await _stream_of(rig, topic)}"), subscribe=(f"_INBOX_{REISSUED}.>",))
    first = await asyncio.to_thread(issue, tools.nsc, home, f"{REISSUED}-first", grants.model_copy(update={"expiry": "1m"}))
    again = await asyncio.to_thread(issue, tools.nsc, home, f"{REISSUED}-again", grants)
    reads = store.hold(f"nats-user-{REISSUED}", {"jwt": first.jwt, "seed": first.seed()})
    async with _connected_as(REISSUED, broker.url) as session:
        findings = await _published_once_under_its_id(session, REISSUED, topic, rig)
        read_once = reads.call_count
        store.hold(f"nats-user-{REISSUED}", {"jwt": again.jwt, "seed": again.seed()})
        if not await _until(lambda: reads.call_count > read_once, _EXPIRY_SECONDS + _EXPIRY_SLACK_SECONDS):
            return [*findings, "the seam never read its credential again after the bus ended the session of the one that ran out"]
        if not await _settles(session, up=True, within=20):
            return [*findings, "the seam did not connect again with the credential the store held once its first had run out"]
        return findings + await _published_once_under_its_id(session, REISSUED, topic, rig)


async def _a_credential_with_no_inbox_of_its_own_is_not_connected_with(
    tools: Tools, home: Path, broker: Broker, store: SecretStoreDouble, rig: Client
) -> tuple[list[str], set[tuple[str, str]]]:
    """A credential that grants its user no inbox of its own would connect, and every publish on it would wait out its
    timeout unacknowledged with the process answering as connected. The seam asks the bus for its inbox when it connects,
    so it does not connect with such a credential, fails its publishes at once, and connects once the store holds one
    that grants the inbox. Returns its findings and the subscription the broker must have refused.
    """
    findings: list[str] = []
    topic, clock = LINEAGE_TOPIC, asyncio.get_running_loop().time
    grants = Permissions(publish=(topic, f"$JS.API.STREAM.MSG.GET.{await _stream_of(rig, topic)}"), subscribe=(f"_INBOX_{UNHEARD}.>",))
    shared = await asyncio.to_thread(issue, tools.nsc, home, f"{UNHEARD}-shared", grants.model_copy(update={"subscribe": ("_INBOX.>",)}))
    own = await asyncio.to_thread(issue, tools.nsc, home, f"{UNHEARD}-own", grants)
    reads = store.hold(f"nats-user-{UNHEARD}", {"jwt": shared.jwt, "seed": shared.seed()})
    settings = BusSettings.model_validate({"RASK_BUS_URL": broker.url, "RASK_BUS_USER": UNHEARD})
    async with Supervisor() as loops, bus_session(loops, settings) as session:
        assert session is not None, "the seam opens no connection for a process named a bus"
        if not await _until(lambda: reads.call_count >= 2, 20):
            findings.append("the seam did not read its credential again after one the bus grants no inbox of its own")
        if await _up(session):
            findings.append("the seam answers as connected on a credential that would bring it no acknowledgement")
        asked = clock()
        try:
            await publish_event(session, _lane_event(probe=UNHEARD), topic=topic, timeout_seconds=30)
        except BusUnavailableError:
            if (waited := clock() - asked) > 1:
                findings.append(f"a publish on a credential with no inbox of its own took {waited:.1f} s to fail")
        except TimeoutError:
            findings.append("a publish on a credential with no inbox of its own waited out its timeout")
        else:
            findings.append("a publish on a credential with no inbox of its own was taken as delivered")
        store.hold(f"nats-user-{UNHEARD}", {"jwt": own.jwt, "seed": own.seed()})
        if not await _settles(session, up=True, within=20):
            findings.append("the seam did not connect once the store held a credential that grants its inbox")
        else:
            findings += await _published_once_under_its_id(session, UNHEARD, topic, rig)
    return findings, {(shared.public_key, f"_INBOX_{UNHEARD}.>")}


# --- the stream Job ----------------------------------------------------------------------------------------------------


def _stream_job(tools: Tools, script: str, broker: Broker, mint_: Mint, home: Path, user: str = "admin") -> tuple[list[str], str]:
    """One phase of the chart's stream Job, run as ``user`` against this broker; it must exit 0 within the runs Kubernetes
    would give it: a failed run is run again (the chart's `backoffLimit`), since creating a stream copied to three
    servers can outlast the CLI's request deadline on a loaded host (measured 2026-11-09), and each phase is idempotent.
    Returns the findings and what the runs printed."""
    printed = ""
    for _ in range(_JOB_RUNS):
        try:
            done = run_stream_job(tools.job_cli, script, mint_.credentials[user], home, 300.0)
        except subprocess.TimeoutExpired as exc:
            return [f"the stream Job did not finish as {user} (its readiness loop retries a refused connection): {str(exc.stdout or '')[-600:]}"], printed
        printed += done.stdout + done.stderr
        if done.returncode == 0:
            return [], printed
    return [f"the stream Job exited {done.returncode} as {user} on each of {_JOB_RUNS} runs: {(done.stdout + done.stderr)[-800:]}"], printed


class _Object(BaseModel):
    """One stream or consumer as the bus holds it: its config, and when it was made."""

    model_config = ConfigDict(frozen=True)

    config: dict[str, Any]
    created: str


async def _bus_objects(rig: Client) -> dict[str, _Object]:
    """Every stream (by name) and consumer (`<stream>/<consumer>`) the bus holds."""
    held: dict[str, _Object] = {}
    for stream in (await rig.request("$JS.API.STREAM.NAMES") or {}).get("streams") or []:
        info = await rig.request(f"$JS.API.STREAM.INFO.{stream}") or {}
        held[stream] = _Object(config=info.get("config") or {}, created=str(info.get("created")))
        for consumer in (await rig.request(f"$JS.API.CONSUMER.NAMES.{stream}") or {}).get("consumers") or []:
            got = await rig.request(f"$JS.API.CONSUMER.INFO.{stream}.{consumer}") or {}
            held[f"{stream}/{consumer}"] = _Object(config=got.get("config") or {}, created=str(got.get("created")))
    return held


def _declared(docs: tuple[dict, ...]) -> set[str]:
    """The streams and consumers a render's declare phase names: each stream it declares, and each consumer
    (`<stream>/<consumer>`) it converges."""
    script = stream_job_script(docs, "nats://unused:4222")
    streams = {line.split()[1] for line in script.splitlines() if line.strip().startswith(("converge_stream ", "converge_bucket "))}
    return streams | {"/".join(line.split()[1:3]) for line in script.splitlines() if line.strip().startswith("converge_consumer ")}


def _prepared(home: Path, name: str, config: dict[str, Any]) -> str:
    path = home / "previous-revision" / f"{name.replace('/', '-')}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(config))
    return str(path)


async def _a_bus_the_previous_revision_left(tools: Tools, broker: Broker, mint_: Mint, home: Path, rig: Client) -> list[str]:
    """The bus as the revision before pull consumers left it, laid out with the nats CLI as the rig's superuser: lineage's
    push subscription `lineage-durable` having acknowledged the first `_ACKED_BEFORE` of `_HELD_BEFORE` events,
    maintenance's `maintenance-durable`, lineage's parking `DLQ/lineage-dlq-durable` and the catalog's mirror
    `CATALOG_CONTROL_BROADCAST`, none carrying a release mark; beside them a consumer of another subscriber's with no
    mark (`lane-stranger`) and one carrying another release's (`lane-other-release`)."""
    limits = {"retention": "limits", "max_consumers": -1, "max_msgs": -1, "max_bytes": -1, "max_msgs_per_subject": -1, "max_msg_size": -1}
    stored = {**limits, "storage": "file", "discard": "old", "num_replicas": 3, "duplicate_window": 120_000_000_000}
    streams = {
        "LINEAGE": {**stored, "name": "LINEAGE", "subjects": ["lineage.events.>"]},
        "DLQ": {**stored, "name": "DLQ", "subjects": ["dlq.>"]},
        CONTROL_STREAM: {**stored, "name": CONTROL_STREAM, "subjects": ["catalog.control.>"]},
        "CATALOG_CONTROL_BROADCAST": {**stored, "name": "CATALOG_CONTROL_BROADCAST", "mirror": {"name": CONTROL_STREAM}},
    }
    pushed = {"ack_policy": "explicit", "replay_policy": "instant", "ack_wait": 60_000_000_000}
    pulled = {"ack_policy": "explicit", "replay_policy": "instant", "deliver_policy": "all", "filter_subject": LINEAGE_TOPIC}
    consumers = {
        "LINEAGE/lineage-durable": {
            **pushed,
            **{"durable_name": "lineage-durable", "deliver_subject": "_INBOX.previous-lineage", "deliver_group": "lineage"},
            **{"deliver_policy": "all", "filter_subject": LINEAGE_TOPIC},
        },
        "LINEAGE/maintenance-durable": {
            **pushed,
            **{"durable_name": "maintenance-durable", "deliver_subject": "_INBOX.previous-maintenance", "deliver_group": "maintenance"},
            **{"deliver_policy": "new", "filter_subject": LINEAGE_TOPIC},
        },
        "DLQ/lineage-dlq-durable": {
            **pushed,
            **{"durable_name": "lineage-dlq-durable", "deliver_subject": "_INBOX.previous-dlq", "deliver_group": "lineage"},
            **{"deliver_policy": "all", "filter_subject": "dlq.lineage.events"},
        },
        "LINEAGE/lane-stranger": {**pulled, "durable_name": "lane-stranger"},
        "LINEAGE/lane-other-release": {**pulled, "durable_name": "lane-other-release", "metadata": {_MARK: "another"}},
    }
    findings: list[str] = []
    rigged = mint_.credentials[RIG]

    async def laid_out(name: str, args: list[str], info: str) -> list[str]:
        """Add one stream or consumer with the nats CLI, and again when the bus did not answer in time: one copied to
        three servers can outlast the CLI's request deadline (as a stream the Job creates can, `_JOB_RUNS`), and an add
        the bus finished after the CLI gave up shows as the object being there."""
        printed = ""
        for _ in range(_JOB_RUNS):
            done = await asyncio.to_thread(run_cli, tools.box_cli, args, rigged, broker.url, home)
            if done.returncode == 0 or "config" in (await rig.request(info) or {}):
                return []
            printed = done.stdout + done.stderr
        return [f"the previous revision's {name} could not be laid out: {printed[-400:]}"]

    for name, config in streams.items():
        findings += await laid_out(name, ["stream", "add", name, "--config", _prepared(home, name, config)], f"$JS.API.STREAM.INFO.{name}")
    for name, config in consumers.items():
        stream, consumer = name.split("/")
        args = ["consumer", "add", stream, consumer, "--config", _prepared(home, name, config)]
        findings += await laid_out(name, args, f"$JS.API.CONSUMER.INFO.{stream}.{consumer}")
    if findings:
        return findings
    catalog = await Client.connect(broker.url, mint_.credentials["catalog"], "previous-catalog", "_INBOX_catalog", answer_within=10.0)
    subscription = await rig.nc.subscribe("_INBOX.previous-lineage", queue="lineage")
    try:
        for n in range(_HELD_BEFORE):
            await catalog.js.publish(LINEAGE_TOPIC, json.dumps(_lane_event(previous=str(n)).as_wire()).encode(), timeout=10)
        for _ in range(_HELD_BEFORE):
            message = await subscription.next_msg(timeout=10)
            if message.metadata.sequence.stream <= _ACKED_BEFORE:
                await message.ack()
        await rig.settle()
        floor = 0
        for _ in range(50):
            info = await rig.request("$JS.API.CONSUMER.INFO.LINEAGE.lineage-durable") or {}
            if (floor := int((info.get("ack_floor") or {}).get("stream_seq", 0))) == _ACKED_BEFORE:
                break
            await asyncio.sleep(0.1)
        findings += [] if floor == _ACKED_BEFORE else [f"the previous revision's lineage-durable acknowledged up to {floor}, not {_ACKED_BEFORE}"]
    finally:
        await subscription.unsubscribe()
        await catalog.close()
    return findings


class _Handled(BaseModel):
    """One delivery a lineage replica's handling was handed."""

    model_config = ConfigDict(frozen=True)

    replica: str
    event_id: str
    sequence: int
    attempt: int
    at: float


def _id_of(delivery: Delivery) -> str:
    """The id of the envelope ``delivery`` carries; empty for a body that is no envelope, or whose id is no string."""
    event_id = (delivery.envelope() or {}).get("id")
    return event_id if isinstance(event_id, str) else ""


class _IngestLane:
    """Lineage's ingest lane as the run's replicas handle it: every delivery recorded, an event's answer the run's to
    choose (taken unless told), an event held while its gate is shut, and every park kept."""

    def __init__(self) -> None:
        self.handled: list[_Handled] = []
        self.answers: dict[str, Verdict] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.parked: list[tuple[str, str]] = []
        self.holding = 0
        self.most_held = 0

    def handle_of(self, replica: str) -> Handle:
        loop = asyncio.get_running_loop()

        async def handle(delivery: Delivery) -> Verdict:
            event_id = _id_of(delivery)
            self.handled.append(_Handled(replica=replica, event_id=event_id, sequence=delivery.sequence, attempt=delivery.attempt, at=loop.time()))
            if (gate := self.gates.get(event_id)) is not None:
                self.holding += 1
                self.most_held = max(self.most_held, self.holding)
                try:
                    await gate.wait()
                finally:
                    self.holding -= 1
            return self.answers.get(event_id, Verdict.TAKEN)

        return handle

    async def park(self, delivery: Delivery, reason: str) -> None:
        self.parked.append((_id_of(delivery), reason))

    def of(self, event_id: str) -> list[_Handled]:
        return [handled for handled in self.handled if handled.event_id == event_id]


class _Replicas:
    """Lineage replicas each pulling the ingest consumer on a connection of its own, as the bus user `lineage`."""

    def __init__(self, stack: AsyncExitStack, url: str, lane: _IngestLane) -> None:
        self._stack, self._url, self.lane = stack, url, lane
        self.pulling: dict[str, asyncio.Task[None]] = {}

    async def start(self, replica: str, lane: Lane = _LANE) -> Bus:
        session = await self._stack.enter_async_context(_connected_as("lineage", self._url))
        self.pulling[replica] = asyncio.create_task(session.consume(lane, self.lane.handle_of(replica), park=self.lane.park), name=f"lane-{replica}")
        return session

    async def stop(self, replica: str) -> None:
        pulling = self.pulling.pop(replica)
        pulling.cancel()
        await asyncio.gather(pulling, return_exceptions=True)

    async def stop_all(self) -> None:
        for replica in list(self.pulling):
            await self.stop(replica)


async def _consumer(rig: Client, stream: str, consumer: str) -> dict[str, Any]:
    return await rig.request(f"$JS.API.CONSUMER.INFO.{stream}.{consumer}") or {}


async def _settled_ingest(rig: Client, sequence: int, within: float = 15.0) -> dict[str, Any]:
    """The ingest consumer once its ack floor has passed ``sequence`` and it holds nothing pending, or as it stands at
    the deadline."""
    deadline = asyncio.get_running_loop().time() + within
    while True:
        info = await _consumer(rig, INGEST_STREAM, INGEST_CONSUMER)
        floor = int((info.get("ack_floor") or {}).get("stream_seq", 0))
        if (floor >= sequence and info.get("num_ack_pending") == 0 and info.get("num_pending") == 0) or asyncio.get_running_loop().time() > deadline:
            return info
        await asyncio.sleep(0.2)


async def _a_consumer_not_declared_yet_is_pulled_once_declared(
    tools: Tools, docs: tuple[dict, ...], broker: Broker, mint_: Mint, home: Path, rig: Client, caplog: pytest.LogCaptureFixture
) -> tuple[list[str], str]:
    """A lineage replica that starts before the stream Job has declared its consumer asks for it again, said once, and
    pulls it once the Job declares it there. The Job's first run on the previous revision's bus begins the consumer
    after the last event the subscription it replaces acknowledged, so the replica is handed exactly the events that
    subscription had not. Returns the findings and what the Job printed."""
    findings: list[str] = []
    lane = _IngestLane()
    async with AsyncExitStack() as stack:
        replicas = _Replicas(stack, broker.url, lane)
        await replicas.start("early")
        if not await _until(lambda: any(r.name == seam.__name__ and r.getMessage() == "bus_pull_failed" for r in caplog.records), 20):
            findings.append("a consumer the Job had not declared yet: the replica never said its pull failed")
        # Nothing marks a line that is not logged again, so the replica is left one more pull at the absent consumer
        # before the Job declares it: the seam pauses that long between a pull the bus refused and the next.
        await asyncio.sleep(seam._PULL_SECONDS + 2)
        job, printed = await asyncio.to_thread(_stream_job, tools, stream_job_script(docs, broker.url), broker, mint_, home)
        findings += [f"the declare phase, first run on the previous revision's bus: {finding}" for finding in job]
        wanted = list(range(_ACKED_BEFORE + 1, _HELD_BEFORE + 1))
        if not await _until(lambda: sorted(h.sequence for h in lane.handled) == wanted, 30):
            findings.append(
                f"a consumer the Job had not declared yet: once declared the replica was handed {sorted(h.sequence for h in lane.handled)}, not {wanted}"
            )
        said = [r for r in caplog.records if r.name == seam.__name__ and r.getMessage() == "bus_pull_failed"]
        if len(said) != 1:
            findings.append(
                f"a consumer the Job had not declared yet: the replica said its pull failed {len(said)} times, not once: {[r.__dict__.get('error') for r in said]}"
            )
        await replicas.stop_all()
    start = (await _consumer(rig, INGEST_STREAM, INGEST_CONSUMER)).get("config", {}).get("opt_start_seq")
    if start != _ACKED_BEFORE + 1:
        findings.append(f"the declare phase began {INGEST_CONSUMER} at stream sequence {start}, not after lineage-durable's ack floor {_ACKED_BEFORE}")
    return findings, printed


def _the_first_run_made_what_the_previous_revision_lacked(printed: str, declared: set[str]) -> list[str]:
    """The declare phase's first run creates every declared object the previous revision's bus lacked, and converges the
    ones it held (LINEAGE, DLQ and CATALOG_CONTROL)."""
    held = {"LINEAGE", "DLQ", CONTROL_STREAM}
    created = {line.split()[1].removesuffix(":") for line in printed.splitlines() if line.startswith(("stream ", "consumer ")) and line.endswith(": created")}
    converged = {
        line.split()[1].removesuffix(":")
        for line in printed.splitlines()
        if line.startswith(("stream ", "consumer ")) and line.endswith(": converged to the declared config")
    }
    if (created, converged) != (declared - held, held & declared):
        return [
            f"the declare phase's first run created {sorted(created)} and converged {sorted(converged)}, not {sorted(declared - held)} and {sorted(held & declared)}"
        ]
    return []


async def _a_second_declare_changes_nothing(tools: Tools, docs: tuple[dict, ...], broker: Broker, mint_: Mint, home: Path, rig: Client) -> list[str]:
    """The declare phase run again on a bus it declared converges every object to the config it already holds: no
    stream or consumer is made again or changed."""
    before = await _bus_objects(rig)
    findings, _ = await asyncio.to_thread(_stream_job, tools, stream_job_script(docs, broker.url), broker, mint_, home)
    after = await _bus_objects(rig)
    changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    return [f"the declare phase, second run: {finding}" for finding in findings] + (
        [f"the declare phase's second run changed {changed}: {[(before.get(n), after.get(n)) for n in changed][:2]}"] if changed else []
    )


async def _a_changed_lane_is_edited_in_place(tools: Tools, changed: tuple[dict, ...], broker: Broker, mint_: Mint, home: Path, rig: Client) -> list[str]:
    """The declare phase on a render whose lane changed (`services.lineage.delivery.ackWaitSeconds`,
    `services.lineage.deliveriesInFlight`) edits the ingest consumer in place: the same consumer, its new ack wait and
    cap, its start kept, and nothing else changed."""
    before = await _bus_objects(rig)
    findings, _ = await asyncio.to_thread(_stream_job, tools, stream_job_script(changed, broker.url), broker, mint_, home)
    after = await _bus_objects(rig)
    name = f"{INGEST_STREAM}/{INGEST_CONSUMER}"
    was, now = before.get(name), after.get(name)
    if was is None or now is None:
        return [*findings, f"the declare phase on a changed lane: {name} is not on the bus ({was}, {now})"]
    held = (now.created, now.config.get("ack_wait"), now.config.get("max_ack_pending"), now.config.get("opt_start_seq"))
    if held != (was.created, _ACK_WAIT_SECONDS * 1_000_000_000, _DELIVERIES_IN_FLIGHT, _ACKED_BEFORE + 1):
        findings.append(f"the declare phase on a changed lane left {name} at (created, ack_wait, max_ack_pending, opt_start_seq) {held}")
    others = sorted(n for n in before.keys() | after.keys() if n != name and before.get(n) != after.get(n))
    return findings + ([f"the declare phase on a changed lane changed {others} besides {name}"] if others else [])


async def _the_retire_phase_removes_nothing_it_could_not_read(
    tools: Tools, retired: tuple[dict, ...], broker: Broker, mint_: Mint, home: Path, rig: Client
) -> list[str]:
    """A retire run that can read no consumer fails and removes nothing: a consumer it could not read is not one it knows
    to be this release's."""
    before = await _bus_objects(rig)
    try:
        done = await asyncio.to_thread(
            run_stream_job, tools.job_cli, stream_job_script(retired, broker.url, retire=True), mint_.credentials[BLIND_ADMIN], home, 300.0
        )
    except subprocess.TimeoutExpired as exc:
        return [f"the retire phase that can read no consumer did not finish: {str(exc.stdout or '')[-600:]}"]
    gone = sorted(before.keys() - (await _bus_objects(rig)).keys())
    findings = [f"the retire phase removed {gone}, though it could read no consumer"] if gone else []
    return findings + ([f"the retire phase exited 0 though it could read no consumer: {(done.stdout + done.stderr)[-600:]}"] if done.returncode == 0 else [])


async def _the_retire_phase_removes_what_git_declares_no_longer(
    tools: Tools, retired: tuple[dict, ...], broker: Broker, mint_: Mint, home: Path, rig: Client
) -> list[str]:
    """The retire phase on a render that no longer declares the commit lane (`maintenance.arrivalEnabled=false`) removes
    that consumer, which carries the release's mark, and the previous revision's three subscriptions and its mirror
    stream (the one-deploy migration), each under admin's own grants. It leaves the consumer that carries no mark and
    the one that carries another release's, and removes no other stream."""
    before = await _bus_objects(rig)
    findings, _ = await asyncio.to_thread(_stream_job, tools, stream_job_script(retired, broker.url, retire=True), broker, mint_, home)
    after = await _bus_objects(rig)
    removed = {"LINEAGE/maintenance-arrival", "LINEAGE/lineage-durable", "LINEAGE/maintenance-durable", "DLQ/lineage-dlq-durable", "CATALOG_CONTROL_BROADCAST"}
    if sorted(before.keys() - after.keys()) != sorted(removed):
        findings.append(f"the retire phase removed {sorted(before.keys() - after.keys())}, not {sorted(removed)}")
    kept = {"LINEAGE/lane-stranger", "LINEAGE/lane-other-release", *(_declared(retired))}
    if missing := sorted(kept - after.keys()):
        findings.append(f"the retire phase left no {missing}")
    return findings


# --- the permission table ----------------------------------------------------------------------------------------------


async def _last_seqs(rig: Client) -> dict[str, int]:
    names = await rig.request("$JS.API.STREAM.NAMES") or {}
    infos = {name: await rig.request(f"$JS.API.STREAM.INFO.{name}") or {} for name in names.get("streams") or []}
    return {name: int(info.get("state", {}).get("last_seq", -1)) for name, info in infos.items()}


async def _without_credential(url: str) -> list[str]:
    """A client presenting nothing must be turned away at CONNECT, for the reason auth gives and no other."""
    try:
        nc = await nats.connect(url, name="no-credential", allow_reconnect=False, max_reconnect_attempts=0, connect_timeout=3)
    except Exception as exc:  # noqa: BLE001 — which exception carries the refusal is the client's choice; the text is the server's
        return [] if "authorization violation" in repr(exc).lower() else [f"a client with no credential failed to connect, but not on authorization: {exc!r}"]
    landed = []
    for subject in PUBLISHERS:
        try:
            ack = await nc.jetstream().publish(subject, b"{}", timeout=2)
        except Exception:  # noqa: BLE001, S112 — a refusal of this one publish is the outcome being looked for
            continue
        landed.append(f"a client with no credential published {subject} (stream {ack.stream} seq {ack.seq})")
    await nc.close()
    return landed or ["a client with no credential connected"]


async def _owner_publish(client: Client, user: str, subject: str) -> list[str]:
    """An owner's publish of one of its subjects, acknowledged by the stream that captures it. Nothing when it lands; the
    finding when it is refused."""
    try:
        await client.js.publish(subject, json.dumps({"probe": user}).encode(), timeout=3)
    except APIError as exc:
        # The stream answered, so the broker admitted the publish: a task subject keeps one message (MAINTENANCE_TASKS),
        # and the owner's earlier probe holds it. A refused publish draws no answer from any stream.
        return [] if exc.err_code == _ONE_PER_SUBJECT else [f"{user} publishes {subject} and its stream refused it: {exc}"]
    except Exception as exc:  # noqa: BLE001 — any failure to land is the finding
        return [f"{user} publishes {subject} and was refused ({type(exc).__name__})"]
    return []


async def _permitted_inside_the_window(clients: dict[str, Client]) -> list[str]:
    """One acknowledged publish by an owner of every owned subject, sent inside the refused-publish window.

    A deploy's own producers keep publishing while the matrix runs, so a stream can take a permitted message at any
    moment of the window. Every run carries one on each stream, so the check is held to telling a permitted message from
    a foreign one rather than to an empty window.
    """
    findings: list[str] = []
    for subject, owners in sorted(PUBLISHERS.items()):
        if (owner := next((user for user in sorted(owners) if user in clients), None)) is not None:
            findings += await _owner_publish(clients[owner], owner, subject)
    return findings


async def _publish_matrix(
    url: str, mint_: Mint, table: dict[str, Permissions], rig: Client
) -> tuple[list[str], set[tuple[str, str]], set[tuple[str, str]], dict[str, Client]]:
    """Every table user against every data subject, and against every inbox another user holds alone: an owner's publish
    is acked, anyone else's must not land, and nobody else may listen for the replies to a user's own connection.

    Returns the publishes and the subscriptions that must have been refused; the broker's log, read once it stops, says
    whether they were.
    """
    clients = {user: await Client.connect(url, mint_.credentials[user], f"probe-{user}", table[user].inbox) for user in table}
    findings: list[str] = []
    # The development store reads its stream's info before it publishes there (deploy/fixtures/rustfs).
    if await clients["object-store"].request("$JS.API.STREAM.INFO.STORE_EVENTS") is None:
        findings.append("the store's read of STORE_EVENTS's info was not answered")
    for user, client in clients.items():
        for subject in (s for s in PUBLISHERS if user in PUBLISHERS[s]):
            findings += await _owner_publish(client, user, subject)
    foreign = {(user, s) for user in clients for s in PUBLISHERS if user not in PUBLISHERS[s]}
    marker = secrets.token_hex(16)
    before = await _last_seqs(rig)
    findings += await _permitted_inside_the_window(clients)
    for user, subject in sorted(foreign):
        await clients[user].nc.publish(subject, json.dumps({"probe": user, "refused": marker}).encode())
    overheard = {(user, f"{table[owner].inbox}.>") for owner in table if table[owner].inbox != "_INBOX" for user in table if user != owner}
    for user, inbox in sorted(overheard):
        await clients[user].nc.subscribe(inbox)
    for client in clients.values():
        await client.settle()
    findings += await _landed_in_the_window(rig, before, await _last_seqs(rig), marker)
    return findings, foreign, overheard, clients


async def _landed_in_the_window(rig: Client, before: dict[str, int], after: dict[str, int], marker: str) -> list[str]:
    """Each message a stream took inside the refused-publish window that a foreign probe sent, or that cannot be read.

    A permitted publisher's message may land in the window and proves nothing; one carrying this run's marker is a
    publish the broker had to refuse, and one the rig cannot read is not shown to be anything else.
    """
    findings: list[str] = []
    for name in sorted(before.keys() | after.keys()):
        first, last = before.get(name, 0), after.get(name, -1)
        if first < 0 or last < first:
            findings.append(f"stream {name} cannot be read across the refused-publish window (last_seq {first} -> {last})")
            continue
        for seq in range(first + 1, last + 1):
            got = await rig.request(f"$JS.API.STREAM.MSG.GET.{name}", {"seq": seq})
            message = (got or {}).get("message")
            if message is None:
                findings.append(f"stream {name} took seq {seq} inside the refused-publish window, and the rig cannot read it: {got}")
            elif marker.encode() in base64.b64decode(message.get("data", "")):
                findings.append(f"stream {name} took a publish that must be refused, at seq {seq} on {message.get('subject')}")
    return findings


async def _calls_outside_a_row_are_refused(probes: dict[str, Client]) -> tuple[list[str], set[tuple[str, str]]]:
    """Each of `REFUSED_CALLS`, sent by its user: none is answered, and the broker's log must hold each refusal. Returns
    the findings and the calls the broker must have refused."""
    findings: list[str] = []
    refused: set[tuple[str, str]] = set()
    for user, subjects in REFUSED_CALLS.items():
        for subject in subjects:
            if (answer := await probes[user].request(subject)) is not None:
                findings.append(f"{user} sent {subject}, outside its row, and the bus answered {answer}")
            refused.add((user, subject))
    return findings, refused


# --- lineage's ingest lane ---------------------------------------------------------------------------------------------


async def _published(publisher: Bus, **data: str) -> CloudEvent:
    event = _lane_event(**data)
    await publish_event(publisher, event, topic=LINEAGE_TOPIC, timeout_seconds=10)
    return event


async def _sequence_of(rig: Client, event: CloudEvent, since: int) -> int:
    (kept,) = await _kept_under(rig, INGEST_STREAM, since, event.id)
    return int(kept["seq"])


async def _a_delivery_is_handled_once_under_its_publishs_trace(publisher: Bus, lane: _IngestLane, exporter: InMemorySpanExporter, rig: Client) -> list[str]:
    """An event the catalog published reaches a lineage replica once, handled as a CONSUMER span that is a child of the
    publish's own span, and is acknowledged."""
    since = (await _last_seqs(rig))[INGEST_STREAM]
    event = await _published(publisher, leg="traced")
    if not await _until(lambda: bool(lane.of(event.id)), 15):
        return ["ingest lane, traced: the event never reached a replica"]
    sequence = await _sequence_of(rig, event, since)
    info = await _settled_ingest(rig, sequence)
    spans = [span for span in exporter.get_finished_spans() if (span.attributes or {}).get("messaging.message.id") == event.id]
    produced = [span for span in spans if span.kind == SpanKind.PRODUCER]
    consumed = [span for span in spans if span.kind == SpanKind.CONSUMER]
    findings = (
        []
        if [h.attempt for h in lane.of(event.id)] == [1]
        else [f"ingest lane, traced: the event was handled on attempts {[h.attempt for h in lane.of(event.id)]}"]
    )
    if len(produced) != 1 or len(consumed) != 1:
        return [*findings, f"ingest lane, traced: spans {[(s.name, s.kind) for s in spans]}, not one publish and one process"]
    parent = consumed[0].parent
    carried = (consumed[0].name, parent.span_id if parent else None, consumed[0].context.trace_id)
    if carried != (f"process {LINEAGE_TOPIC}", produced[0].context.span_id, produced[0].context.trace_id):
        findings.append(f"ingest lane, traced: the delivery was handled as {carried}, not a child of its publish's span")
    if info.get("num_ack_pending") != 0 or int((info.get("ack_floor") or {}).get("stream_seq", 0)) < sequence:
        findings.append(f"ingest lane, traced: the handled event was not acknowledged ({info.get('ack_floor')}, {info.get('num_ack_pending')} pending)")
    return findings


async def _a_delivery_asked_for_again_comes_back_after_the_delay_and_is_parked_on_its_last(publisher: Bus, lane: _IngestLane, rig: Client) -> list[str]:
    """A handling that answers AGAIN has its delivery come back after the lane's delay and not sooner, and on the lane's
    last attempt the delivery is parked, then ended: the consumer's ack floor passes it and nothing is pending."""
    since = (await _last_seqs(rig))[INGEST_STREAM]
    event = _lane_event(leg="again")
    lane.answers[event.id] = Verdict.AGAIN
    await publish_event(publisher, event, topic=LINEAGE_TOPIC, timeout_seconds=10)
    if not await _until(lambda: [event.id] == [parked for parked, _ in lane.parked if parked == event.id], 30):
        return [f"ingest lane, again: the event was never parked: handled {lane.of(event.id)}"]
    sequence = await _sequence_of(rig, event, since)
    info = await _settled_ingest(rig, sequence)
    handled = lane.of(event.id)
    findings = []
    if [h.attempt for h in handled] != list(range(1, _LANE.attempts + 1)):
        findings.append(f"ingest lane, again: the event was handled on attempts {[h.attempt for h in handled]}, not 1 to {_LANE.attempts}")
    gaps = [round(later.at - earlier.at, 2) for earlier, later in pairwise(handled)]
    if not all(_LANE.retry_seconds <= gap < _LANE.retry_seconds + 3 for gap in gaps):
        findings.append(f"ingest lane, again: the event came back after {gaps} s, not after the lane's {_LANE.retry_seconds} s")
    reasons = [reason for parked, reason in lane.parked if parked == event.id]
    if reasons != [f"asked for again on its last delivery ({_LANE.attempts} of {_LANE.attempts})"]:
        findings.append(f"ingest lane, again: the event was parked with {reasons}")
    floor = int((info.get("ack_floor") or {}).get("stream_seq", 0))
    if (floor >= sequence, info.get("num_ack_pending"), info.get("num_pending")) != (True, 0, 0):
        findings.append(f"ingest lane, again: the parked event was not ended (ack floor {floor} for seq {sequence}, {info.get('num_ack_pending')} pending)")
    return findings


async def _a_handling_cut_hands_its_delivery_back_at_once(publisher: Bus, replicas: _Replicas, rig: Client) -> list[str]:
    """A replica stopped while it handles a delivery hands it back, and the other replica is handed it at once, well
    inside the consumer's ack wait."""
    lane = replicas.lane
    event = _lane_event(leg="cut")
    lane.gates[event.id] = asyncio.Event()
    await publish_event(publisher, event, topic=LINEAGE_TOPIC, timeout_seconds=10)
    if not await _until(lambda: bool(lane.of(event.id)), 15):
        return ["ingest lane, cut: the event never reached a replica"]
    (first,) = lane.of(event.id)
    await replicas.stop(first.replica)
    stopped = asyncio.get_running_loop().time()
    again = await _until(lambda: len(lane.of(event.id)) == 2, _ACK_WAIT_SECONDS)
    lane.gates[event.id].set()
    if not again:
        return [f"ingest lane, cut: the event was not handed again within the ack wait: {lane.of(event.id)}"]
    second = lane.of(event.id)[1]
    findings = [] if (second.attempt, second.replica != first.replica) == (2, True) else [f"ingest lane, cut: the event came back as {second}"]
    return findings + ([] if second.at - stopped < 3 else [f"ingest lane, cut: the event came back {second.at - stopped:.1f} s after its handling was cut"])


async def _two_replicas_share_the_consumer_within_its_cap(publisher: Bus, replicas: _Replicas, rig: Client) -> list[str]:
    """Two replicas share the ingest consumer: each event is handed to one of them, both are handed some, and the two
    never hold more than the consumer's `max_ack_pending` between them, though each would handle more at once."""
    lane = replicas.lane
    gate = asyncio.Event()
    events = [_lane_event(leg="shared", n=str(n)) for n in range(3 * _DELIVERIES_IN_FLIGHT)]
    for event in events:
        lane.gates[event.id] = gate
    lane.most_held = 0
    for event in events:
        await publish_event(publisher, event, topic=LINEAGE_TOPIC, timeout_seconds=10)
    capped = False
    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        info = await _consumer(rig, INGEST_STREAM, INGEST_CONSUMER)
        if (lane.holding, info.get("num_ack_pending"), info.get("num_pending")) == (
            _DELIVERIES_IN_FLIGHT,
            _DELIVERIES_IN_FLIGHT,
            len(events) - _DELIVERIES_IN_FLIGHT,
        ):
            capped = int(info.get("num_waiting", 0)) >= 1
            if capped:
                break
        await asyncio.sleep(0.2)
    gate.set()
    findings = [] if capped else [f"ingest lane, shared: the replicas were not seen held at the consumer's cap with a pull waiting ({lane.holding} held)"]
    if not await _until(lambda: all(lane.of(event.id) for event in events), 30):
        return [*findings, f"ingest lane, shared: {sum(1 for e in events if not lane.of(e.id))} events were never handed on"]
    handed = {event.id: [h.replica for h in lane.of(event.id)] for event in events}
    if any(len(replica) != 1 for replica in handed.values()):
        findings.append(f"ingest lane, shared: an event was handed more than once: {handed}")
    if {replica for replicas_of in handed.values() for replica in replicas_of} != set(replicas.pulling):
        findings.append(f"ingest lane, shared: the events were handed to {sorted({r for rs in handed.values() for r in rs})}, not both replicas")
    return findings + (
        []
        if lane.most_held == _DELIVERIES_IN_FLIGHT
        else [f"ingest lane, shared: the replicas held {lane.most_held} at once, not the cap {_DELIVERIES_IN_FLIGHT}"]
    )


async def _a_replica_handles_no_more_at_once_than_its_lane_allows(url: str, publisher: Bus, rig: Client) -> list[str]:
    """One replica whose lane handles one delivery at a time, on a consumer that hands out more: it holds the first and
    asks for no other until that one is settled."""
    lane = _IngestLane()
    gate = asyncio.Event()
    events = [_lane_event(leg="one-at-a-time", n=str(n)) for n in range(_DELIVERIES_IN_FLIGHT)]
    for event in events:
        lane.gates[event.id] = gate
    findings: list[str] = []
    async with AsyncExitStack() as stack:
        replicas = _Replicas(stack, url, lane)
        await replicas.start("alone", _LANE.model_copy(update={"in_flight": 1}))
        for event in events:
            await publish_event(publisher, event, topic=LINEAGE_TOPIC, timeout_seconds=10)
        if not await _until(lambda: lane.holding == 1, 15):
            findings.append("ingest lane, one at a time: the replica never held a delivery")
        # No event marks a pull that is not made, so the replica is watched for as long as a second pull would take many
        # times over: the consumer answers one at once while it has a delivery to hand out.
        await asyncio.sleep(_ONE_AT_A_TIME_SECONDS)
        info = await _consumer(rig, INGEST_STREAM, INGEST_CONSUMER)
        handed = [len(lane.of(event.id)) for event in events]
        if (lane.most_held, info.get("num_ack_pending"), handed) != (1, 1, [1, *[0] * (len(events) - 1)]):
            findings.append(
                f"ingest lane, one at a time: the replica held {lane.most_held} at once ({handed} handed, {info.get('num_ack_pending')} unsettled on the consumer)"
            )
        gate.set()
        if not await _until(lambda: all(lane.of(event.id) for event in events), 30):
            findings.append(f"ingest lane, one at a time: {sum(1 for e in events if not lane.of(e.id))} events were never handed on")
        await replicas.stop_all()
    return findings


async def _a_body_that_is_no_envelope_does_not_end_the_lane(
    url: str, publisher: Bus, lane: _IngestLane, exporter: InMemorySpanExporter, catalog: Credential
) -> list[str]:
    """Each body of `_HOSTILE_BODIES` and each message of `_STATUS_HEADED`, published by a user the bus admits to the
    topic, is handed to a replica once, as a delivery that names no event and under a span that carries no message id,
    and the event behind them is handled: no body or header ends a lane, or is passed over as a word of the bus's."""
    before = len(exporter.get_finished_spans())
    raw = await Client.connect(url, catalog, "nested-catalog", "_INBOX_catalog", answer_within=10.0)
    try:
        stored = [(await raw.js.publish(LINEAGE_TOPIC, body, timeout=10)).seq for body in _HOSTILE_BODIES]
        stored += [(await raw.js.publish(LINEAGE_TOPIC, body, timeout=10, headers=headers)).seq for body, headers in _STATUS_HEADED]
    finally:
        await raw.close()
    behind = await _published(publisher, leg="behind-what-is-no-envelope")
    if not await _until(lambda: bool(lane.of(behind.id)), 15):
        return ["ingest lane, no envelope: the event behind them was never handled"]
    handed = {sequence: [(h.event_id, h.attempt) for h in lane.handled if h.sequence == sequence] for sequence in stored}
    astray = {sequence: seen for sequence, seen in handed.items() if seen != [("", 1)]}
    findings = [f"ingest lane, no envelope: {len(astray)} of {len(stored)} were not handed on once, naming no event: {astray}"] if astray else []

    def unnamed() -> int:
        handled = [span for span in exporter.get_finished_spans()[before:] if span.kind == SpanKind.CONSUMER]
        return sum(1 for span in handled if "messaging.message.id" not in (span.attributes or {}))

    if not await _until(lambda: unnamed() == len(stored), 5):
        findings.append(f"ingest lane, no envelope: {unnamed()} of the {len(stored)} deliveries were handled under a span carrying no message id")
    return findings


async def _lineages_ingest_lane(url: str, publisher: Bus, exporter: InMemorySpanExporter, rig: Client, catalog: Credential) -> list[str]:
    """Lineage's replicas on the ingest consumer the stream Job declared, each pulling it on its own connection."""
    lane = _IngestLane()
    findings: list[str] = []
    async with AsyncExitStack() as stack:
        replicas = _Replicas(stack, url, lane)
        await replicas.start("one")
        findings += await _a_delivery_is_handled_once_under_its_publishs_trace(publisher, lane, exporter, rig)
        findings += await _a_body_that_is_no_envelope_does_not_end_the_lane(url, publisher, lane, exporter, catalog)
        findings += await _a_delivery_asked_for_again_comes_back_after_the_delay_and_is_parked_on_its_last(publisher, lane, rig)
        await replicas.start("two")
        findings += await _a_handling_cut_hands_its_delivery_back_at_once(publisher, replicas, rig)
        await replicas.start("three")
        findings += await _two_replicas_share_the_consumer_within_its_cap(publisher, replicas, rig)
        await replicas.stop_all()
    return findings + await _a_replica_handles_no_more_at_once_than_its_lane_allows(url, publisher, rig)


# --- the catalog's control lane ----------------------------------------------------------------------------------------


class _Followed:
    """What each catalog replica following the control topic was handed, in order."""

    def __init__(self) -> None:
        self.handed: dict[str, list[tuple[int, str]]] = {}
        #: The events, by their `leg` and `n`, whose handling fails once it has recorded them, each with what it raises.
        self.failing: dict[tuple[str, str], type[BaseException]] = {}

    def handle_of(self, replica: str) -> Follow:
        handed = self.handed.setdefault(replica, [])

        async def handle(delivery: Delivery) -> None:
            handed.append((delivery.sequence, _id_of(delivery)))
            data = (delivery.envelope() or {}).get("data")
            failure = self.failing.get((data.get("leg"), data.get("n"))) if isinstance(data, dict) else None
            if failure is not None:
                raise failure("this replica's handling of the event failed")

        return handle

    def ids(self, replica: str) -> list[str]:
        return [event_id for _, event_id in self.handed.get(replica, [])]


async def _control_consumers(rig: Client) -> set[str]:
    return set((await rig.request(f"$JS.API.CONSUMER.NAMES.{CONTROL_STREAM}") or {}).get("consumers") or [])


async def _followers(stack: AsyncExitStack, url: str, followed: _Followed, replicas: tuple[str, ...], publisher: Bus) -> dict[str, asyncio.Task[None]]:
    """``replicas`` following the control topic, each on a connection of its own as `catalog`, once each has begun: a
    follower begins after the stream's last message as its connection is up, so an event is published until each
    follower has been handed the latest one."""
    following = {}
    for replica in replicas:
        session = await stack.enter_async_context(_connected_as("catalog", url))
        following[replica] = asyncio.create_task(session.follow(CONTROL_STREAM, CONTROL_TOPIC, followed.handle_of(replica)), name=f"follow-{replica}")
    for n in range(30):
        (latest,) = await _publish_control(publisher, 1, f"begun-{n}")
        if await _until(lambda latest=latest: all(followed.ids(replica)[-1:] == [latest] for replica in replicas), 2):
            break
    return following


async def _publish_control(publisher: Bus, count: int, leg: str) -> list[str]:
    ids = []
    for n in range(count):
        event = _lane_event(leg=leg, n=str(n))
        await publish_event(publisher, event, topic=CONTROL_TOPIC, timeout_seconds=10)
        ids.append(event.id)
    return ids


async def _the_catalogs_control_lane(url: str, publisher: Bus, catalog: Credential, rig: Client, caplog: pytest.LogCaptureFixture) -> list[str]:
    """Two catalog replicas following the control topic: each is handed every event published after it began, once and
    in the stream's order, and none it began after; an event whose handling fails, or is cancelled from within while
    nobody asked the follower to stop, is said and the events behind it are handed on; a message whose publisher gave
    it the headers of a bus status is handed on like any other, and the events behind it too; a burst is handed on
    whole; the followers leave no consumer on the stream; and each takes its place again on a stream made again."""
    findings: list[str] = []
    followed = _Followed()
    held = await _publish_control(publisher, 2, "held-before")
    async with AsyncExitStack() as stack:
        following = await _followers(stack, url, followed, ("one", "two"), publisher)
        began = {replica: len(followed.ids(replica)) for replica in following}
        if any(set(held) & set(followed.ids(replica)) for replica in following) or not all(began.values()):
            return [f"control lane: the followers began with {followed.handed}, having begun after {held}"]

        def since(replica: str) -> list[str]:
            return followed.ids(replica)[began[replica] :]

        # One handling raises, and one meets the cancellation of something it awaited: neither is a stop asked of the
        # follower.
        followed.failing.update({("followed", "1"): RuntimeError, ("followed", "3"): asyncio.CancelledError})
        first = await _publish_control(publisher, 5, "followed")
        for replica in following:
            if not await _until(lambda r=replica: len(since(r)) >= len(first), 30) or since(replica) != first:
                findings.append(f"control lane, two followers: {replica} was handed {since(replica)}, not {first}")
        unhandled = [r for r in caplog.records if r.name == seam.__name__ and r.getMessage() == "bus_followed_message_not_handled"]
        if len(unhandled) != len(followed.failing) * len(following):
            findings.append(
                f"control lane, a handling that fails: it was said {len(unhandled)} times, not {len(followed.failing)} times by each of the {len(following)} followers"
            )
        raw = await Client.connect(url, catalog, "burst-catalog", "_INBOX_catalog", answer_within=10.0)
        try:
            for body, headers in _STATUS_HEADED:
                await raw.js.publish(CONTROL_TOPIC, body, timeout=10, headers=headers)
            second = await _publish_control(publisher, 5, "behind-a-status-header")
            wanted = first + [""] * len(_STATUS_HEADED) + second
            for replica in following:
                if not await _until(lambda r=replica: len(since(r)) >= len(wanted), 30) or since(replica) != wanted:
                    findings.append(
                        f"control lane, a status header: {replica} was handed {since(replica)[len(first) :]} after it, not each headed message and {second}"
                    )
            # The burst goes out as plain publishes the stream captures, as fast as one connection sends them.
            padding = "x" * _BURST_BYTES
            for n in range(_BURST):
                await raw.nc.publish(CONTROL_TOPIC, json.dumps({"id": f"burst-{n}", "padding": padding}).encode())
            await raw.settle()
        finally:
            await raw.close()
        burst = [f"burst-{n}" for n in range(_BURST)]
        for replica in following:
            if not await _until(lambda r=replica: len(since(r)) >= len(wanted) + _BURST, 120):
                findings.append(f"control lane, burst: {replica} was handed {len(since(replica)) - len(wanted)} of {_BURST}")
            elif since(replica)[len(wanted) :] != burst:
                findings.append(f"control lane, burst: {replica} was not handed the burst whole and in order")
        if consumers := await _control_consumers(rig):
            findings.append(f"control lane: the followers left {sorted(consumers)} on {CONTROL_STREAM}, where a follower holds no consumer")
        # The stream is removed and made again as it was, as an operator does for a setting no edit changes: it numbers
        # its messages from 1 again, behind every follower's place.
        config = (await rig.request(f"$JS.API.STREAM.INFO.{CONTROL_STREAM}") or {}).get("config")
        removed = await rig.request(f"$JS.API.STREAM.DELETE.{CONTROL_STREAM}")
        made = await rig.request(f"$JS.API.STREAM.CREATE.{CONTROL_STREAM}", config)
        if not (removed or {}).get("success") or (made or {}).get("config") != config:
            findings.append(f"control lane, a stream made again: the rig could not remove and make {CONTROL_STREAM}: {removed}, {made}")
        handed = {replica: len(since(replica)) for replica in following}
        again = await _publish_control(publisher, 3, "on-a-stream-made-again")
        for replica in following:
            if not await _until(lambda r=replica: since(r)[handed[r] :] == again, 30):
                findings.append(f"control lane, a stream made again: {replica} was handed {since(replica)[handed[replica] :]} on it, not {again}")
        for task in following.values():
            task.cancel()
        await asyncio.gather(*following.values(), return_exceptions=True)
    return findings


# --- an outage past nats-py's own reconnects ---------------------------------------------------------------------------


async def _a_pulling_and_a_following_lane_deliver_again_after_an_outage(broker: Broker, held: dict[str, respx.Route], publisher: Bus, rig: Client) -> list[str]:
    """A lineage replica pulling and a catalog replica following, each through a `Link` the test cuts for longer than
    nats-py reconnects by itself: each connection closes for good and the seam's loop opens another with a fresh read of
    the credential. Events published while they are cut off are handed on once the bus is back, on the rebuilt
    connections and with no process started again: none lost, none handed twice, the followed ones in order."""
    lane, followed = _IngestLane(), _Followed()
    links = {"lineage": Link(broker.port), "catalog": Link(broker.port)}
    findings: list[str] = []
    for link in links.values():
        await link.start()
    try:
        async with AsyncExitStack() as stack:
            puller = await stack.enter_async_context(_connected_as("lineage", links["lineage"].url))
            follower = await stack.enter_async_context(_connected_as("catalog", links["catalog"].url))
            loops = [
                asyncio.create_task(puller.consume(_LANE, lane.handle_of("puller"), park=lane.park)),
                asyncio.create_task(follower.follow(CONTROL_STREAM, CONTROL_TOPIC, followed.handle_of("follower"))),
            ]
            ready = await _published(publisher, leg="before-the-outage")
            if not await _until(lambda: bool(lane.of(ready.id)), 15):
                findings.append("outage: the pulling lane handled nothing before the outage")
            reads = {user: held[user].call_count for user in links}
            for link in links.values():
                await link.stop()
            # A fresh read of the credential is the seam's loop opening a client after nats-py closed its own for good.
            if not await _until(lambda: all(held[user].call_count > reads[user] for user in links), _OUTAGE_WITHIN):
                findings.append(f"outage: the seam never opened a new client for {[u for u in links if held[u].call_count == reads[u]]}")
            pulled = [await _published(publisher, leg="in-the-outage", n=str(n)) for n in range(3)]
            controlled = await _publish_control(publisher, 3, "in-the-outage")
            for link in links.values():
                await link.start()
            if not await _settles(puller, up=True, within=30) or not await _settles(follower, up=True, within=30):
                findings.append("outage: a seam did not connect again once its bus was back")
            if not await _until(lambda: all(lane.of(event.id) for event in pulled) and len(followed.ids("follower")) >= len(controlled), 45):
                findings.append(
                    f"outage: after it the pulling lane was handed {[len(lane.of(e.id)) for e in pulled]} and the following one {followed.ids('follower')}"
                )
            if [[h.attempt for h in lane.of(event.id)] for event in pulled] != [[1]] * len(pulled):
                findings.append(f"outage: the pulling lane handled the outage's events on attempts {[[h.attempt for h in lane.of(e.id)] for e in pulled]}")
            if followed.ids("follower") != controlled:
                findings.append(f"outage: the following lane was handed {followed.ids('follower')}, not {controlled}")
            if dead := [task.get_name() for task in loops if task.done()]:
                findings.append(f"outage: the lanes {dead} ended")
            for task in loops:
                task.cancel()
            await asyncio.gather(*loops, return_exceptions=True)
    finally:
        for link in links.values():
            await link.stop()
    return findings


# --- what the run measured ---------------------------------------------------------------------------------------------


def _monitor_reads(tools: Tools, broker: Broker, mint_: Mint, home: Path) -> list[str]:
    """nats-box's read-only operator view, as `monitor`: the streams, their consumers, one consumer, and the account."""
    findings: list[str] = []
    reads = (
        ["stream", "ls"],
        ["stream", "ls", "--names"],
        ["stream", "info", "LINEAGE"],
        ["consumer", "ls", "LINEAGE"],
        ["consumer", "ls", "LINEAGE", "--names"],
        ["consumer", "info", INGEST_STREAM, INGEST_CONSUMER],
        ["account", "info"],
    )
    for args in reads:
        done = run_cli(tools.box_cli, list(args), mint_.credentials["monitor"], broker.url, home)
        if done.returncode:
            findings.append(f"nats {' '.join(args)} as monitor exited {done.returncode}: {(done.stdout + done.stderr)[-400:]}")
    return findings


def _refusals_against_the_expected(log_text: str, mint_: Mint, foreign: set[tuple[str, str]], overheard: set[tuple[str, str]]) -> list[str]:
    """The broker must have refused every foreign publish, every subscription to another user's inbox and every call
    outside a row, and nothing else: any other refusal is a missing allowance."""
    seen = [(mint_.user_of(v.user_key), v) for v in violations(log_text)]
    refused = {(user, v.subject) for user, v in seen if v.kind == "Publish"}
    unheard = {(user, v.subject) for user, v in seen if v.kind == "Subscription"}
    accepted = [f"{user} published {subject}, which only {sorted(PUBLISHERS.get(subject, ())) or 'nobody'} may" for user, subject in sorted(foreign - refused)]
    accepted += [f"{user} subscribed to {inbox}, the replies to another user's own connection" for user, inbox in sorted(overheard - unheard)]
    unexpected = {
        f"the broker refused {user} {v.kind.lower()} on {v.subject} (connection {v.connection})"
        for user, v in seen
        if (user, v.subject) not in foreign | overheard and not (user == BLIND_ADMIN and v.subject.startswith("$JS.API.CONSUMER.INFO."))
    }
    return accepted + sorted(unexpected)


def _grants_no_call_used(log_text: str, mint_: Mint, table: dict[str, Permissions], refused: set[tuple[str, str]]) -> list[str]:
    """Each grant of a user this module drives that no call of that user used: a row broader than what its client does.
    A call the broker refused, which the run sent to prove a refusal, uses no grant."""
    calls = client_calls(log_text)
    findings: list[str] = []
    for user in sorted(DRIVEN & set(table)):
        made = {call for call in calls.get(mint_.credentials[user].public_key, set()) if (user, call.subject) not in refused}
        log.info("%s called %s", user, sorted((call.kind, call.subject) for call in made))
        findings += [f"{user} holds {grant}, which no call of its own used" for grant in unused_grants(table[user], made)]
    return findings


@pytest.mark.asyncio
async def test_nats_admits_each_client_only_to_its_own_subjects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secret_store: SecretStoreDouble, caplog: pytest.LogCaptureFixture
) -> None:
    tools = Tools.from_env(os.environ)
    values = _values()
    docs = render(*DEFAULT_ARGS, "-f", str(values))
    changed = render(*DEFAULT_ARGS, "-f", str(values), *_CHANGED_LANE)
    retired = render(*DEFAULT_ARGS, "-f", str(values), *_CHANGED_LANE, *_ARRIVAL_RETIRED)
    table = {user: Permissions(publish=tuple(sorted(pub)), subscribe=tuple(sorted(sub))) for user, (pub, sub) in nats_users(docs).items()}
    assert table, f"the render with {values} issues no NATS user (the dev seed is absent)"
    services = _services(docs)
    findings = [*_lane_runs_the_charts_images(tools, docs), *_table_names_every_client(table, services), *_data_grants_beyond_the_publishers(table)]
    assert findings == [], "\n".join(findings)
    blind = Permissions(publish=tuple(s for s in table["admin"].publish if not s.startswith("$JS.API.CONSUMER.INFO.")), subscribe=table["admin"].subscribe)
    rig_grants = Permissions(publish=("$JS.API.>", "$JS.ACK.>"), subscribe=("_INBOX.>",))
    mint_ = mint(tools.nsc, {**table, RIG: rig_grants, BLIND_ADMIN: blind}, tmp_path / "nsc")
    # The store's stand-in holds each service's credential, as the dev seed writes it there; the rig's own HTTP calls, all
    # to loopback, pass it by.
    secret_store.router.route(host="127.0.0.1").pass_through()
    held = {user: secret_store.hold(f"nats-user-{user}", {"jwt": mint_.credentials[user].jwt, "seed": mint_.credentials[user].seed()}) for user in services}
    monkeypatch.setattr(seam, "_RECONNECT_ATTEMPTS", _RECONNECT_ATTEMPTS)
    monkeypatch.setattr(seam, "_RECONNECT_WAIT_SECONDS", _RECONNECT_WAIT_SECONDS)
    monkeypatch.setattr(seam, "_PAUSE_SECONDS", _PAUSE_SECONDS)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(seam, "tracer", provider.get_tracer(seam.__name__))
    caplog.set_level(logging.WARNING, logger=seam.__name__)
    broker = Broker(tools.nats_server, mint_, tmp_path / "broker")
    broker.start()
    try:
        rig = await Client.connect(broker.url, mint_.credentials[RIG], RIG, answer_within=10.0)
        admin = await Client.connect(broker.url, mint_.credentials["admin"], "admin", answer_within=10.0)
        findings += await _a_bus_the_previous_revision_left(tools, broker, mint_, tmp_path, rig)
        assert findings == [], "\n".join(findings)
        undeclared, printed = await _a_consumer_not_declared_yet_is_pulled_once_declared(tools, docs, broker, mint_, tmp_path, rig, caplog)
        findings += undeclared
        findings += _the_first_run_made_what_the_previous_revision_lacked(printed, _declared(docs))
        findings += await _a_second_declare_changes_nothing(tools, docs, broker, mint_, tmp_path, rig)
        findings += await _a_changed_lane_is_edited_in_place(tools, changed, broker, mint_, tmp_path, rig)
        findings += await _the_retire_phase_removes_nothing_it_could_not_read(tools, retired, broker, mint_, tmp_path, rig)
        findings += await _the_retire_phase_removes_what_git_declares_no_longer(tools, retired, broker, mint_, tmp_path, rig)
        # The next reconcile's declare, with the subscription the lane's start was taken from gone: the lane keeps its place.
        findings += [f"after the retire: {finding}" for finding in await _a_second_declare_changes_nothing(tools, retired, broker, mint_, tmp_path, rig)]
        findings += await _without_credential(broker.url)
        matrix, foreign, overheard, probes = await _publish_matrix(broker.url, mint_, table, rig)
        findings += matrix
        # Started here and judged last: its first credential takes a minute to run out.
        reissued = asyncio.create_task(_a_credential_the_bus_ends_is_replaced_by_the_stores_current_one(tools, tmp_path / "nsc", broker, secret_store, rig))
        try:
            outside, refused = await _calls_outside_a_row_are_refused(probes)
            findings += outside
            foreign |= refused
            async with _connected_as("catalog", broker.url) as publisher:
                findings += await _lineages_ingest_lane(broker.url, publisher, exporter, rig, mint_.credentials["catalog"])
                findings += await _the_catalogs_control_lane(broker.url, publisher, mint_.credentials["catalog"], rig, caplog)
                findings += await _a_pulling_and_a_following_lane_deliver_again_after_an_outage(broker, held, publisher, rig)
            findings += await asyncio.to_thread(_monitor_reads, tools, broker, mint_, tmp_path)
            findings += await _services_publish_as_themselves(broker.url, services, rig, probes)
            findings += await _a_service_outlasts_a_bus_it_cannot_reach(broker, services[0], held[services[0]], rig)
            unheard, refused_inbox = await _a_credential_with_no_inbox_of_its_own_is_not_connected_with(tools, tmp_path / "nsc", broker, secret_store, rig)
            findings += unheard
            overheard |= refused_inbox
            findings += await reissued
        finally:
            reissued.cancel()
            await asyncio.gather(reissued, return_exceptions=True)
        for client in [rig, admin, *probes.values()]:
            await client.close()
    finally:
        broker.stop()
    log_text = broker.log_text()
    findings += _refusals_against_the_expected(log_text, mint_, foreign, overheard)
    findings += _grants_no_call_used(log_text, mint_, table, foreign | overheard)
    assert findings == [], "\n".join(findings)
