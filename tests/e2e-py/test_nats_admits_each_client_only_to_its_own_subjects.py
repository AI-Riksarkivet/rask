"""Each NATS client reaches exactly its own subjects, a client with no credential reaches nothing, each service's pod
presents only its ServiceAccount token and is granted a short session it renews, and each service takes its deliveries
on its own connection ([[XC-078]], [[XC-199]]).

Closes-when clause 1 ("a publish from a pod without its app's credential is refused on every cascade, control and
lineage subject"), proved offline: the nats-server the NATS fixture deploys (deploy/fixtures/nats, a prerequisite rask
never installs) in operator mode with a MEMORY resolver, JetStream and TLS on the client port, test-only keys minted
with nsc, and one bus user per row of the permission table the chart renders (read off the render, so a values.yaml
`nats.auth.flagged` grant counts only while its flag is on): a scoped signing key per session user, whose scope the
bus enforces on every session the stand-in signer (`deploy/fixtures/nats/callout`, two processes) grants, and a static
user per other row (`nats_auth_rig.bus_users`).

The table is held to a declaration written independently of it and to what the run measures, because a table checked
only against itself can never be too broad. `PUBLISHERS` is who may publish each data subject, read off every publish
call site; a table that lets anyone else publish a subject fails, and so does one that withholds one. Every call a
client makes is read off the broker's trace: a call the table refuses fails the run, and so does a grant of a user
this module drives that no call of that user used.

Each service's own connection is the seam itself: the real `lakehouse.bus.Bus`, connected as the bus user the render
names that service, handed what the chart hands its pod and nothing else: a token file, the bus's CA and the session's
length (`nats_auth_rig.Pod`).

* Its pod presents only its token, over TLS verified against the bus's CA (a CA the bus's certificate does not chain to
  is not connected with), and its session carries exactly its row's grants. A token asking for another user's name, a
  token no TokenReview authenticates, and a connection with no token at all are each refused.
* It publishes each of its topics once under its event's id, under an inbox no other user may read; an id another
  publisher stored a message under is not taken as delivered; a bus it cannot reach fails its publishes at once and is
  connected to again, by nats-py through a short cut and by the seam's own loop past it; a session whose key grants
  its user no inbox of its own is not held, and is once the key's scope is changed in place to grant it.
* Its session is asked for again before the bus ends it, on the same client, for three session lengths while it
  publishes and its lane pulls: nothing acknowledged is lost or doubled. A session it cannot renew (the signers away
  across the renewal), and one the bus ends unrenewed (the signer grants shorter sessions than the pod was told), each
  leave it without a connection until the signers answer, and it opens a new one in-process and delivers what it
  staged meanwhile. A user whose scoped key is withdrawn from the account loses its sessions and is refused, and the
  other users keep theirs.
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

The chart's stream Job runs as `admin`, presenting its pod's token to the nats CLI with no creds file, both phases with
their rendered scripts, on a bus that already holds events on three of the declared streams and consumers of other
subscribers: the declare phase creates and converges every stream and consumer, begins lineage's ingest consumer at the
stream's first message, and edits a changed lane in place; the retire phase removes what this release declares no
longer, leaves what another subscriber holds, removes no stream, and removes nothing it could not read (`admin`'s key
scoped in place without its consumer reads for that run).

After the run, no file a service's pod was given and no environment a service ran in holds an NKEY seed, a NATS user
JWT or (in an environment) a bus token, and no signer logged a token.

Run by `dagger call nats-auth` (`make nats-auth`), which provides the binaries and selects this file by path.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import shutil
import subprocess
from collections import Counter
from collections.abc import AsyncGenerator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from itertools import pairwise
from pathlib import Path
from typing import Any

import nats
import pytest
import yaml
from nats.js.errors import APIError
from nats_auth_rig import (
    RENDER_ARGS,
    VALUES_ENV,
    Client,
    Link,
    Permissions,
    Pki,
    Pod,
    Rig,
    Tools,
    client_calls,
    matches,
    secrets_in,
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
from tests.unit.chart_render import DEFAULT_ARGS, containers, env_of, render, render_fixture


pytestmark = pytest.mark.nats_auth

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[2]
#: The rig's own observer, a static user every JetStream call and every ack granted: it reads stream and consumer state
#: and lays out the bus the stream Job first runs on, so no table user's call set carries the test's bookkeeping. It is
#: minted beside the table and is not a row of it.
RIG = "rask-test-rig"
#: How many times the stream Job runs before the lane gives up on it, inside the chart's `backoffLimit` of 20.
_JOB_RUNS = 3
#: The topics a service publishes on its own connection: its lineage events (`lakehouse.outbox`) and the control lane's.
OWN_CONNECTION_TOPICS = ("lineage.events.v1", "catalog.control.v1")
LINEAGE_TOPIC, CONTROL_TOPIC = OWN_CONNECTION_TOPICS
#: The stream the catalog's control events are kept on, which each catalog replica follows (`catalog.api.control_lane`).
CONTROL_STREAM = "CATALOG_CONTROL"
#: A session user of the rig's own, of a ServiceAccount of its own, whose scoped key first grants it only the shared
#: inbox, as a key scoped before its row gave it an inbox of its own would: the bus grants the session and would hand it
#: no acknowledgement. Minted beside the table and not a row of it: it may publish lineage and read a stored message back.
UNHEARD = "rask-test-unheard"
#: How long a session lasts in every leg but the renewal legs: longer than the module runs, so a probe connected at its
#: start keeps its session to the end.
_LONG_SESSION = 3600
#: The renewal legs' session, as the table and the pod are both told it: `BusSettings.session_seconds`'s least, renewed
#: 15 s in (the seam's least margin is 5 s), and how many lengths the leg outlives.
_SHORT_SESSION = 20
_SESSION_LENGTHS = 3
#: How often the renewal leg publishes.
_PUBLISH_EVERY = 0.25
#: How long the signers stay away in the legs that hold them stopped: past a short session's renewal and its end.
_SIGNERS_AWAY = 14.0
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
#: How many events the lineage stream holds before the stream Job first declares lineage's ingest consumer.
_HELD_BEFORE = 5
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
#: How each client the render names connects: a session user's pod presents its token, a static user its creds. The
#: lakehouse's own pods hold no bus credential; the static users are the development fixtures' and nats-box's.
SESSION_USERS = frozenset({"admin", "catalog", "lineage", "maintenance", "maintenance-worker"})
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
    """Every binary the lane runs comes from what the estate deploys: an image rask's render or the NATS fixture runs, or,
    for one the lane builds (named by its dockerfile), the dockerfile of an image the NATS fixture runs in operator mode,
    which is where its stand-in signer runs."""
    deployed = {c["image"] for _, _, c in containers((*docs, *render_fixture("nats")))}
    signing = {c["image"].rpartition("/")[2].partition(":")[0] for _, _, c in containers(render_fixture("nats", "--set", "nats.auth.server=true"))}
    findings = []
    for tool, source in sorted(tools.images.items()):
        if source.endswith(".dockerfile"):
            if not (REPO / source).is_file() or Path(source).stem not in signing:
                findings.append(f"the lane builds {tool} from {source}, of which the NATS fixture runs no image")
        elif source not in deployed:
            findings.append(f"the lane runs {tool} from {source}, which neither the chart nor a fixture deploys")
    return findings + [
        f"the lane names no source for {tool}" for tool in sorted({"nsc", "nats-server", "bus-signer", "job/nats", "box/nats"} - tools.images.keys())
    ]


def _table_names_every_client(table: dict[str, Permissions], services: list[str], sessions: set[str]) -> list[str]:
    """The table issues one user per client and none beyond: each service the render names a bus user, and the others;
    and each of the lakehouse's own is a session user, every other a static one."""
    clients = OTHER_CLIENTS | set(services)
    findings = [f"no table user for {user}, which the render needs a credential for" for user in sorted(clients - set(table))]
    findings += [f"the table issues {user}, which no client connects as" for user in sorted(set(table) - clients)]
    return findings + ([f"the session users are {sorted(sessions)}, not {sorted(SESSION_USERS)}"] if sessions != SESSION_USERS else [])


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
async def _session_of(settings: BusSettings) -> AsyncGenerator[Bus]:
    """A service's own connection to the bus, as the chart names it to a pod (`lakehouse.bus.bus_session`), up or not."""
    async with Supervisor() as loops, bus_session(loops, settings) as session:
        assert session is not None, "the seam opens no connection for a process named a bus"
        yield session


@asynccontextmanager
async def _connected_as(rig: Rig, user: str, url: str | None = None, *, session_seconds: int = _LONG_SESSION, pod: Pod | None = None) -> AsyncGenerator[Bus]:
    """A service's own connection to the bus, opened as the chart names it to a pod and up: the seam is handed a pod of
    ``user``'s (its token file and the bus's CA, nothing else) and presents its token."""
    settings = (pod or rig.pod(user)).settings(url or rig.url, session_seconds)
    async with _session_of(settings) as session:
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


async def _services_publish_as_themselves(bus: Rig, services: list[str], rig: Client, probes: dict[str, Client]) -> list[str]:
    """Each service on the seam its events are published with, as the bus user the render names it: every topic it
    publishes, kept once under its event's id, and an id another publisher of the topic took first."""
    findings: list[str] = []
    for user in services:
        async with _connected_as(bus, user) as session:
            for topic in (topic for topic in OWN_CONNECTION_TOPICS if user in PUBLISHERS[topic]):
                findings += await _published_once_under_its_id(session, user, topic, rig)
                squatter = probes[min(PUBLISHERS[topic] - {user}, default=user)]
                findings += await _an_id_another_message_holds_is_not_delivered(session, user, topic, squatter)
    return findings


def _ended(caplog: pytest.LogCaptureFixture, user: str) -> int:
    """How many connections of ``user``'s the seam has said closed for good, its loop then opening another."""
    return sum(1 for r in caplog.records if r.name == seam.__name__ and r.getMessage() == "bus_connection_ended" and r.__dict__.get("user") == user)


async def _a_service_outlasts_a_bus_it_cannot_reach(bus: Rig, user: str, rig: Client, caplog: pytest.LogCaptureFixture) -> list[str]:
    """``user``'s own connection through a `Link` the test cuts. While the bus cannot be reached a publish fails at once, so its
    event stays staged and no caller waits out its own timeout. After a short cut nats-py reconnects by itself, on the same client.
    Past its own attempts it closes for good, and the seam's loop opens another client, presenting the token the pod's
    file holds then, so the process publishes again with no restart."""
    findings: list[str] = []
    topic, clock = LINEAGE_TOPIC, asyncio.get_running_loop().time
    link = Link(bus.broker.port, bus.pki)
    await link.start()
    try:
        async with _connected_as(bus, user, link.url) as session:
            ended_once, first = _ended(caplog, user), session._client
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
            if not await _settles(session, up=True, within=15) or session._client is not first or _ended(caplog, user) != ended_once:
                findings.append(f"nats-py did not reconnect {user} by itself after a short cut ({_ended(caplog, user) - ended_once} connections ended)")
            findings += await _published_once_under_its_id(session, user, topic, rig)

            await link.stop()
            if not await _until(lambda: _ended(caplog, user) > ended_once, 30):
                findings.append(f"the seam never opened another client for {user} once nats-py had given its connection up")
            await link.start()
            if not await _settles(session, up=True, within=20):
                findings.append(f"the seam did not connect {user} again once its bus was back")
            else:
                findings += await _published_once_under_its_id(session, user, topic, rig)
    finally:
        await link.stop()
    return findings


async def _a_session_with_no_inbox_of_its_own_is_not_held(bus: Rig, rig: Client) -> tuple[list[str], set[tuple[str, str]]]:
    """A session whose key grants its user no inbox of its own would connect, and every publish on it would wait out its
    timeout unacknowledged with the process answering as connected. The seam asks the bus for its inbox when it connects,
    so it does not hold such a session and fails its publishes at once; once the key's scope is changed in place to grant
    the inbox (the same key), the seam's next session is held. Returns its findings and the subscription the broker must
    have refused.
    """
    findings: list[str] = []
    topic, clock = LINEAGE_TOPIC, asyncio.get_running_loop().time
    granted = len([d for d in bus.signers.decisions() if (d.event, d.user) == ("bus_session_granted", UNHEARD)])
    async with _session_of(bus.pod(UNHEARD).settings(bus.url, _LONG_SESSION)) as session:
        if not await _until(lambda: len([d for d in bus.signers.decisions() if (d.event, d.user) == ("bus_session_granted", UNHEARD)]) >= granted + 2, 20):
            findings.append("the seam did not ask for a session again after one whose key grants it no inbox of its own")
        if await _up(session):
            findings.append("the seam answers as connected on a session that would bring it no acknowledgement")
        asked = clock()
        try:
            await publish_event(session, _lane_event(probe=UNHEARD), topic=topic, timeout_seconds=30)
        except BusUnavailableError:
            if (waited := clock() - asked) > 1:
                findings.append(f"a publish on a session with no inbox of its own took {waited:.1f} s to fail")
        except TimeoutError:
            findings.append("a publish on a session with no inbox of its own waited out its timeout")
        else:
            findings.append("a publish on a session with no inbox of its own was taken as delivered")
        own = bus.permissions(UNHEARD).model_copy(update={"subscribe": (f"_INBOX_{UNHEARD}.>",)})
        findings += [f"the servers did not take {UNHEARD}'s key scoped to its own inbox: {unheard}" for unheard in await bus.scope(UNHEARD, own)]
        if not await _settles(session, up=True, within=30):
            findings.append("the seam did not connect once the key's scope granted its inbox")
        else:
            findings += await _published_once_under_its_id(session, UNHEARD, topic, rig)
    return findings, {(UNHEARD, f"_INBOX_{UNHEARD}.>")}


# --- the stream Job ----------------------------------------------------------------------------------------------------


def _stream_job(bus: Rig, script: str, home: Path) -> tuple[list[str], str]:
    """One phase of the chart's stream Job, run as `admin` against this broker; it must exit 0 within the runs Kubernetes
    would give it: a failed run is run again (the chart's `backoffLimit`), since creating a stream copied to three
    servers can outlast the CLI's request deadline on a loaded host (measured 2026-11-09), and each phase is idempotent.
    Returns the findings and what the runs printed."""
    printed = ""
    for _ in range(_JOB_RUNS):
        try:
            done = bus.run_stream_job(script, home, 300.0)
        except subprocess.TimeoutExpired as exc:
            return [f"the stream Job did not finish as admin (its readiness loop retries a refused connection): {str(exc.stdout or '')[-600:]}"], printed
        printed += done.stdout + done.stderr
        if done.returncode == 0:
            return [], printed
    return [f"the stream Job exited {done.returncode} as admin on each of {_JOB_RUNS} runs: {(done.stdout + done.stderr)[-800:]}"], printed


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
    path = home / "laid-out" / f"{name.replace('/', '-')}.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(config))
    return str(path)


async def _a_bus_holding_events_and_other_subscribers(bus: Rig, home: Path, rig: Client) -> list[str]:
    """The bus the stream Job first runs on, laid out with the nats CLI as the rig's superuser: three of the streams it
    declares (LINEAGE holding `_HELD_BEFORE` events), and on LINEAGE a consumer of another subscriber's with no mark
    (`lane-stranger`) and one carrying another release's (`lane-other-release`)."""
    limits = {"retention": "limits", "max_consumers": -1, "max_msgs": -1, "max_bytes": -1, "max_msgs_per_subject": -1, "max_msg_size": -1}
    stored = {**limits, "storage": "file", "discard": "old", "num_replicas": 3, "duplicate_window": 120_000_000_000}
    streams = {
        "LINEAGE": {**stored, "name": "LINEAGE", "subjects": ["lineage.events.>"]},
        "DLQ": {**stored, "name": "DLQ", "subjects": ["dlq.>"]},
        CONTROL_STREAM: {**stored, "name": CONTROL_STREAM, "subjects": ["catalog.control.>"]},
    }
    pulled = {"ack_policy": "explicit", "replay_policy": "instant", "deliver_policy": "all", "filter_subject": LINEAGE_TOPIC}
    consumers = {
        "LINEAGE/lane-stranger": {**pulled, "durable_name": "lane-stranger"},
        "LINEAGE/lane-other-release": {**pulled, "durable_name": "lane-other-release", "metadata": {_MARK: "another"}},
    }
    findings: list[str] = []

    async def laid_out(name: str, args: list[str], info: str) -> list[str]:
        """Add one stream or consumer with the nats CLI, and again when the bus did not answer in time: one copied to
        three servers can outlast the CLI's request deadline (as a stream the Job creates can, `_JOB_RUNS`), and an add
        the bus finished after the CLI gave up shows as the object being there."""
        printed = ""
        for _ in range(_JOB_RUNS):
            done = await asyncio.to_thread(bus.run_cli, args, RIG, home)
            if done.returncode == 0 or "config" in (await rig.request(info) or {}):
                return []
            printed = done.stdout + done.stderr
        return [f"{name} could not be laid out: {printed[-400:]}"]

    for name, config in streams.items():
        findings += await laid_out(name, ["stream", "add", name, "--config", _prepared(home, name, config)], f"$JS.API.STREAM.INFO.{name}")
    for name, config in consumers.items():
        stream, consumer = name.split("/")
        args = ["consumer", "add", stream, consumer, "--config", _prepared(home, name, config)]
        findings += await laid_out(name, args, f"$JS.API.CONSUMER.INFO.{stream}.{consumer}")
    if findings:
        return findings
    catalog = await bus.client("catalog", answer_within=10.0)
    try:
        for n in range(_HELD_BEFORE):
            await catalog.js.publish(LINEAGE_TOPIC, json.dumps(_lane_event(held=str(n)).as_wire()).encode(), timeout=10)
    finally:
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

    def __init__(self, stack: AsyncExitStack, bus: Rig, lane: _IngestLane) -> None:
        self._stack, self._bus, self.lane = stack, bus, lane
        self.pulling: dict[str, asyncio.Task[None]] = {}

    async def start(self, replica: str, lane: Lane = _LANE) -> Bus:
        session = await self._stack.enter_async_context(_connected_as(self._bus, "lineage"))
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
    bus: Rig, docs: tuple[dict, ...], home: Path, rig: Client, caplog: pytest.LogCaptureFixture
) -> tuple[list[str], str]:
    """A lineage replica that starts before the stream Job has declared its consumer asks for it again, said once, and
    pulls it once the Job declares it there. The Job's first run begins the consumer at the stream's first message, so
    the replica is handed every event the stream held. Returns the findings and what the Job printed."""
    findings: list[str] = []
    lane = _IngestLane()
    async with AsyncExitStack() as stack:
        replicas = _Replicas(stack, bus, lane)
        await replicas.start("early")
        if not await _until(lambda: any(r.name == seam.__name__ and r.getMessage() == "bus_pull_failed" for r in caplog.records), 20):
            findings.append("a consumer the Job had not declared yet: the replica never said its pull failed")
        # Nothing marks a line that is not logged again, so the replica is left one more pull at the absent consumer
        # before the Job declares it: the seam pauses that long between a pull the bus refused and the next.
        await asyncio.sleep(seam._PULL_SECONDS + 2)
        job, printed = await asyncio.to_thread(_stream_job, bus, stream_job_script(docs, bus.url), home)
        findings += [f"the declare phase, first run: {finding}" for finding in job]
        wanted = list(range(1, _HELD_BEFORE + 1))
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
    if start != 1:
        findings.append(f"the declare phase began {INGEST_CONSUMER} at stream sequence {start}, not at the stream's first message")
    return findings, printed


def _the_first_run_made_what_the_bus_lacked(printed: str, declared: set[str]) -> list[str]:
    """The declare phase's first run creates every declared object the bus lacked, and converges the ones it held
    (LINEAGE, DLQ and CATALOG_CONTROL)."""
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


async def _a_second_declare_changes_nothing(bus: Rig, docs: tuple[dict, ...], home: Path, rig: Client) -> list[str]:
    """The declare phase run again on a bus it declared converges every object to the config it already holds: no
    stream or consumer is made again or changed."""
    before = await _bus_objects(rig)
    findings, _ = await asyncio.to_thread(_stream_job, bus, stream_job_script(docs, bus.url), home)
    after = await _bus_objects(rig)
    changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    return [f"the declare phase, second run: {finding}" for finding in findings] + (
        [f"the declare phase's second run changed {changed}: {[(before.get(n), after.get(n)) for n in changed][:2]}"] if changed else []
    )


async def _a_changed_lane_is_edited_in_place(bus: Rig, changed: tuple[dict, ...], home: Path, rig: Client) -> list[str]:
    """The declare phase on a render whose lane changed (`services.lineage.delivery.ackWaitSeconds`,
    `services.lineage.deliveriesInFlight`) edits the ingest consumer in place: the same consumer, its new ack wait and
    cap, its start kept, and nothing else changed."""
    before = await _bus_objects(rig)
    findings, _ = await asyncio.to_thread(_stream_job, bus, stream_job_script(changed, bus.url), home)
    after = await _bus_objects(rig)
    name = f"{INGEST_STREAM}/{INGEST_CONSUMER}"
    was, now = before.get(name), after.get(name)
    if was is None or now is None:
        return [*findings, f"the declare phase on a changed lane: {name} is not on the bus ({was}, {now})"]
    held = (now.created, now.config.get("ack_wait"), now.config.get("max_ack_pending"), now.config.get("opt_start_seq"))
    if held != (was.created, _ACK_WAIT_SECONDS * 1_000_000_000, _DELIVERIES_IN_FLIGHT, 1):
        findings.append(f"the declare phase on a changed lane left {name} at (created, ack_wait, max_ack_pending, opt_start_seq) {held}")
    others = sorted(n for n in before.keys() | after.keys() if n != name and before.get(n) != after.get(n))
    return findings + ([f"the declare phase on a changed lane changed {others} besides {name}"] if others else [])


async def _the_retire_phase_removes_nothing_it_could_not_read(bus: Rig, retired: tuple[dict, ...], home: Path, rig: Client) -> tuple[list[str], set[str]]:
    """A retire run that can read no consumer fails and removes nothing: a consumer it could not read is not one it knows
    to be this release's. For the run, `admin`'s scoped key is scoped in place without `$JS.API.CONSUMER.INFO`, so every
    consumer read the Job makes fails as a read during a JetStream leader move does, while it may still list and delete
    ([[LH-343]]); its scope is restored after. Returns the findings and the broker's log lines of that run, whose
    consumer-read refusals are the ones it asked for."""
    granted = bus.permissions("admin")
    blind = granted.model_copy(update={"publish": tuple(s for s in granted.publish if not s.startswith("$JS.API.CONSUMER.INFO."))})
    before, logged = await _bus_objects(rig), set(bus.broker.log_text().splitlines())
    findings = [f"the servers did not take admin's key scoped without its consumer reads: {unheard}" for unheard in await bus.scope("admin", blind)]
    try:
        done = await asyncio.to_thread(bus.run_stream_job, stream_job_script(retired, bus.url, retire=True), home, 300.0)
    except subprocess.TimeoutExpired as exc:
        findings.append(f"the retire phase that can read no consumer did not finish: {str(exc.stdout or '')[-600:]}")
    else:
        if done.returncode == 0:
            findings.append(f"the retire phase exited 0 though it could read no consumer: {(done.stdout + done.stderr)[-600:]}")
    finally:
        findings += [f"the servers did not take admin's key scoped back to its row: {unheard}" for unheard in await bus.scope("admin", granted)]
    gone = sorted(before.keys() - (await _bus_objects(rig)).keys())
    return findings + ([f"the retire phase removed {gone}, though it could read no consumer"] if gone else []), set(bus.broker.log_text().splitlines()) - logged


async def _the_retire_phase_removes_what_git_declares_no_longer(bus: Rig, retired: tuple[dict, ...], home: Path, rig: Client) -> list[str]:
    """The retire phase on a render that no longer declares the commit lane (`maintenance.arrivalEnabled=false`) removes
    that consumer, which carries the release's mark, under admin's own grants. It leaves the consumer that carries no
    mark and the one that carries another release's, and removes no stream."""
    before = await _bus_objects(rig)
    findings, _ = await asyncio.to_thread(_stream_job, bus, stream_job_script(retired, bus.url, retire=True), home)
    after = await _bus_objects(rig)
    removed = {"LINEAGE/maintenance-arrival"}
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


async def _without_credential(bus: Rig) -> list[str]:
    """A client presenting nothing over TLS is the servers' sentinel, which grants nothing: the signer refuses it a
    session, and it is turned away at CONNECT, for the reason auth gives and no other."""
    try:
        nc = await nats.connect(bus.url, name="no-credential", tls=bus.pki.trust(), allow_reconnect=False, max_reconnect_attempts=0, connect_timeout=5)
    except Exception as exc:  # noqa: BLE001 — which exception carries the refusal is the client's choice; the text is the server's
        if "authorization violation" not in repr(exc).lower():
            return [f"a client with no credential failed to connect, but not on authorization: {exc!r}"]
        said = [d.reason for d in bus.signers.decisions() if d.event == "bus_session_refused" and d.user == "no-credential"]
        return [] if said == ["no token presented"] else [f"the signer refused the client with no credential with {said}, not once for presenting no token"]
    landed = []
    for subject in PUBLISHERS:
        try:
            ack = await nc.jetstream().publish(subject, b"{}", timeout=2)
        except Exception:  # noqa: BLE001, S112 — a refusal of this one publish is the outcome being looked for
            continue
        landed.append(f"a client with no credential published {subject} (stream {ack.stream} seq {ack.seq})")
    await nc.close()
    return landed or ["a client with no credential connected"]


async def _refused_a_session(bus: Rig, settings: BusSettings, reason: str) -> list[str]:
    """The seam, handed ``settings``, is refused a session for ``reason`` (the signer's word) and holds no connection."""
    refused = len([d for d in bus.signers.decisions() if d.reason == reason])
    async with _session_of(settings) as session:
        if not await _until(lambda: len([d for d in bus.signers.decisions() if d.reason == reason]) > refused, 20):
            return [f"the signer never refused {settings.user} a session for {reason!r}: {bus.signers.decisions()[-3:]}"]
        return [f"the seam connected as {settings.user} though the signer refused it for {reason!r}"] if await _up(session) else []


async def _a_pod_presents_only_its_token(bus: Rig) -> list[str]:
    """A pod's process is handed its token file and the bus's CA and nothing else, and connects over TLS verified against
    that CA: with a CA the bus's certificate does not chain to it is never connected and the signer is never asked. A
    token of one ServiceAccount asking for another user's name, and a token no TokenReview authenticates, are each
    refused a session by the signer, the seam holding no connection; a client presenting nothing is refused too."""
    findings: list[str] = []
    stranger = Pki.make(bus.home / "stranger-pki")
    pod = bus.pod("catalog")
    shutil.copyfile(stranger.ca, pod.ca_file)
    reviewed = bus.reviewer.reviewed(bus.table.users["catalog"].service_account)
    async with _session_of(pod.settings(bus.url, _LONG_SESSION)) as session:
        if await _settles(session, up=True, within=8):
            findings.append("the seam connected to a bus whose certificate does not chain to the CA its pod was given")
    if bus.reviewer.reviewed(bus.table.users["catalog"].service_account) != reviewed:
        findings.append("a pod that could not verify the bus's certificate had its token reviewed: it was sent before the bus was verified")
    lineage = bus.table.users["lineage"]
    findings += await _refused_a_session(
        bus, bus.pod("lineage", account=bus.table.users["catalog"].service_account).settings(bus.url, _LONG_SESSION), "service account is not that bus user's"
    )
    forged = bus.pod("lineage", account=lineage.service_account)
    forged.token_file.write_text(f"forged-{secrets.token_urlsafe(16)}")
    findings += await _refused_a_session(bus, forged.settings(bus.url, _LONG_SESSION), "token review: not authenticated")
    return findings + await _without_credential(bus)


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
    bus: Rig, table: dict[str, Permissions], rig: Client
) -> tuple[list[str], set[tuple[str, str]], set[tuple[str, str]], dict[str, Client]]:
    """Every table user against every data subject, and against every inbox another user holds alone: an owner's publish
    is acked, anyone else's must not land, and nobody else may listen for the replies to a user's own connection. A
    session user's probe presents a token of its ServiceAccount, so what it may do is its scoped key's scope.

    Returns the publishes and the subscriptions that must have been refused; the broker's log, read once it stops, says
    whether they were.
    """
    clients = {user: await bus.client(user, name=f"probe-{user}") for user in table}
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


async def _a_replica_handles_no_more_at_once_than_its_lane_allows(bus: Rig, publisher: Bus, rig: Client) -> list[str]:
    """One replica whose lane handles one delivery at a time, on a consumer that hands out more: it holds the first and
    asks for no other until that one is settled."""
    lane = _IngestLane()
    gate = asyncio.Event()
    events = [_lane_event(leg="one-at-a-time", n=str(n)) for n in range(_DELIVERIES_IN_FLIGHT)]
    for event in events:
        lane.gates[event.id] = gate
    findings: list[str] = []
    async with AsyncExitStack() as stack:
        replicas = _Replicas(stack, bus, lane)
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


async def _a_body_that_is_no_envelope_does_not_end_the_lane(bus: Rig, publisher: Bus, lane: _IngestLane, exporter: InMemorySpanExporter) -> list[str]:
    """Each body of `_HOSTILE_BODIES` and each message of `_STATUS_HEADED`, published by a user the bus admits to the
    topic, is handed to a replica once, as a delivery that names no event and under a span that carries no message id,
    and the event behind them is handled: no body or header ends a lane, or is passed over as a word of the bus's."""
    before = len(exporter.get_finished_spans())
    raw = await bus.client("catalog", answer_within=10.0)
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


async def _lineages_ingest_lane(bus: Rig, publisher: Bus, exporter: InMemorySpanExporter, rig: Client) -> list[str]:
    """Lineage's replicas on the ingest consumer the stream Job declared, each pulling it on its own connection."""
    lane = _IngestLane()
    findings: list[str] = []
    async with AsyncExitStack() as stack:
        replicas = _Replicas(stack, bus, lane)
        await replicas.start("one")
        findings += await _a_delivery_is_handled_once_under_its_publishs_trace(publisher, lane, exporter, rig)
        findings += await _a_body_that_is_no_envelope_does_not_end_the_lane(bus, publisher, lane, exporter)
        findings += await _a_delivery_asked_for_again_comes_back_after_the_delay_and_is_parked_on_its_last(publisher, lane, rig)
        await replicas.start("two")
        findings += await _a_handling_cut_hands_its_delivery_back_at_once(publisher, replicas, rig)
        await replicas.start("three")
        findings += await _two_replicas_share_the_consumer_within_its_cap(publisher, replicas, rig)
        await replicas.stop_all()
    return findings + await _a_replica_handles_no_more_at_once_than_its_lane_allows(bus, publisher, rig)


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


async def _followers(stack: AsyncExitStack, bus: Rig, followed: _Followed, replicas: tuple[str, ...], publisher: Bus) -> dict[str, asyncio.Task[None]]:
    """``replicas`` following the control topic, each on a connection of its own as `catalog`, once each has begun: a
    follower begins after the stream's last message as its connection is up, so an event is published until each
    follower has been handed the latest one."""
    following = {}
    for replica in replicas:
        session = await stack.enter_async_context(_connected_as(bus, "catalog"))
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


async def _the_catalogs_control_lane(bus: Rig, publisher: Bus, rig: Client, caplog: pytest.LogCaptureFixture) -> list[str]:
    """Two catalog replicas following the control topic: each is handed every event published after it began, once and
    in the stream's order, and none it began after; an event whose handling fails, or is cancelled from within while
    nobody asked the follower to stop, is said and the events behind it are handed on; a message whose publisher gave
    it the headers of a bus status is handed on like any other, and the events behind it too; a burst is handed on
    whole; the followers leave no consumer on the stream; and each takes its place again on a stream made again."""
    findings: list[str] = []
    followed = _Followed()
    held = await _publish_control(publisher, 2, "held-before")
    async with AsyncExitStack() as stack:
        following = await _followers(stack, bus, followed, ("one", "two"), publisher)
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
        raw = await bus.client("catalog", answer_within=10.0)
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


async def _a_pulling_and_a_following_lane_deliver_again_after_an_outage(bus: Rig, publisher: Bus, rig: Client, caplog: pytest.LogCaptureFixture) -> list[str]:
    """A lineage replica pulling and a catalog replica following, each through a `Link` the test cuts for longer than
    nats-py reconnects by itself: each connection closes for good and the seam's loop opens another, presenting its pod's
    token again. Events published while they are cut off are handed on once the bus is back, on the rebuilt connections
    and with no process started again: none lost, none handed twice, the followed ones in order."""
    lane, followed = _IngestLane(), _Followed()
    links = {"lineage": Link(bus.broker.port, bus.pki), "catalog": Link(bus.broker.port, bus.pki)}
    findings: list[str] = []
    for link in links.values():
        await link.start()
    try:
        async with AsyncExitStack() as stack:
            puller = await stack.enter_async_context(_connected_as(bus, "lineage", links["lineage"].url))
            follower = await stack.enter_async_context(_connected_as(bus, "catalog", links["catalog"].url))
            loops = [
                asyncio.create_task(puller.consume(_LANE, lane.handle_of("puller"), park=lane.park)),
                asyncio.create_task(follower.follow(CONTROL_STREAM, CONTROL_TOPIC, followed.handle_of("follower"))),
            ]
            ready = await _published(publisher, leg="before-the-outage")
            if not await _until(lambda: bool(lane.of(ready.id)), 15):
                findings.append("outage: the pulling lane handled nothing before the outage")
            ended = {user: _ended(caplog, user) for user in links}
            for link in links.values():
                await link.stop()
            if not await _until(lambda: all(_ended(caplog, user) > ended[user] for user in links), _OUTAGE_WITHIN):
                findings.append(f"outage: the seam never gave up a client for {[u for u in links if _ended(caplog, u) == ended[u]]}")
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


# --- a session's length ------------------------------------------------------------------------------------------------


def _granted(bus: Rig, user: str) -> int:
    """How many sessions the signers granted ``user``."""
    return sum(1 for d in bus.signers.decisions() if (d.event, d.user) == ("bus_session_granted", user))


def _expired(log_text: str, user: str) -> int:
    """How many of ``user``'s sessions the bus ended at their length: the error it sends the client as it ends one, as its
    trace shows it."""
    return sum(1 for line in log_text.splitlines() if f'/APP/user:{user}" - ->> [-ERR' in line and "User Authentication Expired" in line)


async def _ids_kept(rig: Client, stream: str, since: int) -> Counter[str]:
    """How many messages ``stream`` holds after sequence ``since`` under each message id."""
    kept: Counter[str] = Counter()
    for seq in range(since + 1, (await _last_seqs(rig)).get(stream, since) + 1):
        message = ((await rig.request(f"$JS.API.STREAM.MSG.GET.{stream}", {"seq": seq})) or {}).get("message") or {}
        kept[_headers(message).get("Nats-Msg-Id", "")] += 1
    return kept


class _Delivered(BaseModel):
    """One event a leg published as its relay would: asked for, acknowledged (or not), and how many publishes it took."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    asked: float
    acknowledged: float | None
    publishes: int


async def _published_until_taken(session: Bus, event: CloudEvent, within: float) -> _Delivered:
    """Publish ``event`` again under its id until the bus takes it, as an outbox relay does, for up to ``within`` s."""
    clock = asyncio.get_running_loop().time
    asked, publishes = clock(), 0
    while clock() - asked < within:
        publishes += 1
        try:
            await publish_event(session, event, topic=LINEAGE_TOPIC, timeout_seconds=5)
        except (BusUnavailableError, TimeoutError):
            await asyncio.sleep(0.2)
            continue
        return _Delivered(event_id=event.id, asked=asked, acknowledged=clock(), publishes=publishes)
    return _Delivered(event_id=event.id, asked=asked, acknowledged=None, publishes=publishes)


async def _a_session_is_renewed_on_the_same_client(bus: Rig, rig: Client, caplog: pytest.LogCaptureFixture) -> tuple[list[str], dict[str, float]]:
    """A lineage process on sessions of `_SHORT_SESSION` s, as its table and its pod are both told, publishing every
    `_PUBLISH_EVERY` s and pulling its ingest consumer on the same connection for `_SESSION_LENGTHS` lengths and more: its
    session is asked for again before each ends, the connection never closes for good, and the client is the one it
    opened. Every event the bus acknowledged is stored once and handled once. Returns the findings and what it measured:
    the longest a publish waited for its acknowledgement and the longest from an acknowledgement to the handling."""
    findings: list[str] = []
    lane, clock, user = _IngestLane(), asyncio.get_running_loop().time, "lineage"
    since = (await _last_seqs(rig))[INGEST_STREAM]
    delivered: list[_Delivered] = []
    with bus.sessions_of(_SHORT_SESSION):
        async with _connected_as(bus, user, session_seconds=_SHORT_SESSION) as session:
            first, ended, granted, expired = session._client, _ended(caplog, user), _granted(bus, user), _expired(bus.broker.log_text(), user)
            pulling = asyncio.create_task(session.consume(_LANE, lane.handle_of("renewing"), park=lane.park), name="lane-renewing")
            began = clock()
            while clock() - began < _SESSION_LENGTHS * _SHORT_SESSION + 5:
                delivered.append(await _published_until_taken(session, _lane_event(leg="renewal", n=str(len(delivered))), 30))
                await asyncio.sleep(_PUBLISH_EVERY)
            taken = [d for d in delivered if d.acknowledged is not None]
            if not await _until(lambda: all(lane.of(d.event_id) for d in taken), 45):
                findings.append(f"renewal: {sum(1 for d in taken if not lane.of(d.event_id))} acknowledged events were never handled")
            pulling.cancel()
            await asyncio.gather(pulling, return_exceptions=True)
            renewed = _granted(bus, user) - granted
            if session._client is not first or _ended(caplog, user) != ended:
                findings.append(f"renewal: the connection closed for good {_ended(caplog, user) - ended} times across {_SESSION_LENGTHS} session lengths")
            if renewed < _SESSION_LENGTHS or _expired(bus.broker.log_text(), user) != expired:
                findings.append(
                    f"renewal: {renewed} sessions granted and {_expired(bus.broker.log_text(), user) - expired} ended by the bus, not one per renewal and none"
                )
    if lost := [d.event_id for d in delivered if d.acknowledged is None]:
        findings.append(f"renewal: {len(lost)} events were never acknowledged in 30 s")
    kept = await _ids_kept(rig, INGEST_STREAM, since)
    if doubled := {d.event_id: kept[d.event_id] for d in taken if kept[d.event_id] != 1}:
        findings.append(f"renewal: events stored other than once: {doubled}")
    if handled := {d.event_id: len(lane.of(d.event_id)) for d in taken if len(lane.of(d.event_id)) != 1}:
        findings.append(f"renewal: events handled other than once: {handled}")
    measured = {
        "renewals": float(renewed),
        "events": float(len(delivered)),
        "publishes_retried": float(sum(1 for d in taken if d.publishes > 1)),
        "longest_publish_seconds": max(d.acknowledged - d.asked for d in taken if d.acknowledged is not None),
        "longest_acknowledged_to_handled_seconds": max(lane.of(d.event_id)[0].at - (d.acknowledged or 0) for d in taken if lane.of(d.event_id)),
        "handled_on_a_later_delivery": float(sum(1 for d in taken if any(h.attempt > 1 for h in lane.of(d.event_id)))),
    }
    return findings, measured


async def _a_session_it_cannot_renew_is_opened_again(bus: Rig, rig: Client, caplog: pytest.LogCaptureFixture) -> tuple[list[str], dict[str, float]]:
    """Twice, a catalog process on sessions of `_SHORT_SESSION` s loses its session while the signers are away for
    `_SIGNERS_AWAY` s: once at its renewal (its pod told the session's true length, so its own renewal drops the session
    the signers cannot replace), and once at the session's end (its pod told a longer one, as a pod not yet rolled after
    the table shortened its sessions, so it never asks and the bus ends the session). Its publishes fail at once while it
    has no session and stay staged; the kubelet rotates its token meanwhile. Once the signers answer again it opens a new
    connection in-process, presenting the rotated token, and delivers what it staged, each event stored once. Returns the
    findings and how long each stretch was without a connection after the signers were back."""
    findings: list[str] = []
    measured: dict[str, float] = {}
    clock, user = asyncio.get_running_loop().time, "catalog"
    account = bus.table.users[user].service_account
    for stretch, told, lost_at in (("renewal", _SHORT_SESSION, _SHORT_SESSION - 5), ("end", _LONG_SESSION, _SHORT_SESSION)):
        since = (await _last_seqs(rig))[INGEST_STREAM]
        with bus.sessions_of(_SHORT_SESSION):
            pod = bus.pod(user)
            async with _connected_as(bus, user, session_seconds=told, pod=pod) as session:
                began, ended, expired = clock(), _ended(caplog, user), _expired(bus.broker.log_text(), user)
                await asyncio.sleep(max(0.0, began + lost_at - 3 - clock()))
                await asyncio.to_thread(bus.signers.stop)
                rotated = bus.reviewer.issue(account)
                pod.token_file.write_text(rotated)
                staged, away_until = [], clock() + _SIGNERS_AWAY
                while clock() < away_until:
                    event = _lane_event(leg=f"staged-at-the-{stretch}", n=str(len(staged)))
                    try:
                        await publish_event(session, event, topic=LINEAGE_TOPIC, timeout_seconds=5)
                    except (BusUnavailableError, TimeoutError):
                        staged.append(event)
                    await asyncio.sleep(0.5)
                if await _up(session):
                    findings.append(f"the {stretch}: the process still had a connection after its session was lost with the signers away")
                await asyncio.to_thread(bus.signers.start)
                returned = clock()
                if not await _settles(session, up=True, within=30):
                    findings.append(f"the {stretch}: the process never connected again once the signers were back")
                    continue
                measured[f"{stretch}_without_a_connection_after_the_signers_returned_seconds"] = clock() - returned
                if bus.reviewer.reviews_of(rotated) == 0:
                    findings.append(f"the {stretch}: the new connection did not present the token the kubelet rotated in")
                if stretch == "end" and (_expired(bus.broker.log_text(), user) == expired or _ended(caplog, user) == ended):
                    findings.append("the end: the bus did not end the session at its length, or the seam did not open another client")
                if not staged:
                    findings.append(f"the {stretch}: no publish failed while the process had no connection")
                late = [await _published_until_taken(session, event, 30) for event in staged]
                if unsent := [d.event_id for d in late if d.acknowledged is None]:
                    findings.append(f"the {stretch}: {len(unsent)} staged events were not delivered once the process connected again")
        kept = await _ids_kept(rig, INGEST_STREAM, since)
        if doubled := {event.id: kept[event.id] for event in staged if kept[event.id] != 1}:
            findings.append(f"the {stretch}: staged events stored other than once: {doubled}")
    return findings, measured


async def _a_withdrawn_key_ends_its_users_sessions(bus: Rig, caplog: pytest.LogCaptureFixture) -> list[str]:
    """Withdrawing lineage's scoped key from the account (its new JWT pushed to every server) ends lineage's session, and
    every session the signer goes on granting it is refused by the bus, whose account no longer holds the key; the
    catalog's connection, signed by its own key, stays up on the same client and publishes."""
    findings: list[str] = []
    async with _connected_as(bus, "lineage") as withdrawn, _connected_as(bus, "catalog") as kept:
        first, ended, granted = kept._client, _ended(caplog, "catalog"), _granted(bus, "lineage")
        findings += [f"the servers did not take APP without lineage's key: {unheard}" for unheard in await bus.withdraw("lineage")]
        if not await _settles(withdrawn, up=False, within=10):
            findings.append("a withdrawn key: lineage's connection stayed up")
        deadline = asyncio.get_running_loop().time() + 15
        while asyncio.get_running_loop().time() < deadline:
            if await _up(withdrawn):
                findings.append("a withdrawn key: lineage connected again")
                break
            await asyncio.sleep(0.2)
        if _granted(bus, "lineage") <= granted:
            findings.append("a withdrawn key: the signer was never asked for lineage again, so its refusal was not the bus's")
        if not await _up(kept) or kept._client is not first or _ended(caplog, "catalog") != ended:
            findings.append("a withdrawn key: the catalog's connection, signed by its own key, did not stay up on its client")
        try:
            await publish_event(kept, _lane_event(probe="kept"), topic=LINEAGE_TOPIC, timeout_seconds=10)
        except (BusUnavailableError, TimeoutError) as exc:
            findings.append(f"a withdrawn key: the catalog could not publish: {type(exc).__name__}: {exc}")
    return findings


# --- what the run measured ---------------------------------------------------------------------------------------------


def _monitor_reads(bus: Rig, home: Path) -> list[str]:
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
        done = bus.run_cli(list(args), "monitor", home)
        if done.returncode:
            findings.append(f"nats {' '.join(args)} as monitor exited {done.returncode}: {(done.stdout + done.stderr)[-400:]}")
    return findings


def _refusals_against_the_expected(log_text: str, bus: Rig, foreign: set[tuple[str, str]], overheard: set[tuple[str, str]], blind: set[str]) -> list[str]:
    """The broker must have refused every foreign publish, every subscription to another user's inbox and every call
    outside a row, and nothing else: any other refusal is a missing allowance. The consumer reads of the stream Job's run
    under a key scoped without them (``blind``, its log lines) are the refusals that run asked for."""
    asked = [v for line in blind for v in violations(line) if v.subject.startswith("$JS.API.CONSUMER.INFO.")]
    seen = [(bus.mint.user_of(v.user_key), v) for v in violations("\n".join(line for line in log_text.splitlines() if line not in blind))]
    refused = {(user, v.subject) for user, v in seen if v.kind == "Publish"}
    unheard = {(user, v.subject) for user, v in seen if v.kind == "Subscription"}
    accepted = [f"{user} published {subject}, which only {sorted(PUBLISHERS.get(subject, ())) or 'nobody'} may" for user, subject in sorted(foreign - refused)]
    accepted += [f"{user} subscribed to {inbox}, the replies to another user's own connection" for user, inbox in sorted(overheard - unheard)]
    unexpected = {
        f"the broker refused {user} {v.kind.lower()} on {v.subject} (connection {v.connection})"
        for user, v in seen
        if (user, v.subject) not in foreign | overheard
    }
    asked_of = {bus.mint.user_of(v.user_key) for v in asked}
    return (
        accepted
        + sorted(unexpected)
        + ([] if asked and asked_of == {"admin"} else [f"the stream Job's blind run was refused its consumer reads as {asked_of}"])
    )


def _grants_no_call_used(log_text: str, bus: Rig, table: dict[str, Permissions], refused: set[tuple[str, str]]) -> list[str]:
    """Each grant of a user this module drives that no call of that user used: a row broader than what its client does.
    A call the broker refused, which the run sent to prove a refusal, uses no grant."""
    calls = client_calls(log_text)
    findings: list[str] = []
    for user in sorted(DRIVEN & set(table)):
        made = {call for call in calls.get(bus.mint.who(user), set()) if (user, call.subject) not in refused}
        log.info("%s called %s", user, sorted((call.kind, call.subject) for call in made))
        findings += [f"{user} holds {grant}, which no call of its own used" for grant in unused_grants(table[user], made)]
    return findings


def _the_runs_secrets_stayed_with_the_rig(bus: Rig) -> list[str]:
    """No file a pod was given and no environment its process ran in holds a seed, a NATS user JWT or (in the environment)
    a token, and no signer logged a token. The in-process services ran in this process, so its environment is theirs."""
    findings = secrets_in([bus.home / "pods"], {"the test process, where the services ran": dict(os.environ)}, bus.reviewer.tokens())
    logged = bus.signers.log_text()
    return findings + [f"a signer logged a token it was asked about ({token[:4]}…)" for token in bus.reviewer.tokens() if token in logged]


@pytest.mark.asyncio
async def test_nats_admits_each_client_only_to_its_own_subjects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    tools = Tools.from_env(os.environ)
    values = _values()
    docs = render(*DEFAULT_ARGS, "-f", str(values), *RENDER_ARGS)
    changed = render(*DEFAULT_ARGS, "-f", str(values), *RENDER_ARGS, *_CHANGED_LANE)
    retired = render(*DEFAULT_ARGS, "-f", str(values), *RENDER_ARGS, *_CHANGED_LANE, *_ARRIVAL_RETIRED)
    unheard = Permissions(publish=(LINEAGE_TOPIC, f"$JS.API.STREAM.MSG.GET.{INGEST_STREAM}"), subscribe=("_INBOX.>",))
    rig_grants = Permissions(publish=("$JS.API.>", "$JS.ACK.>"), subscribe=("_INBOX.>",))
    bus = Rig(tools, docs, tmp_path / "rig", sessions={UNHEARD: unheard}, static={RIG: rig_grants}, session_seconds=_LONG_SESSION)
    table = {**{user: row.permissions() for user, row in bus.rendered.users.items()}, **bus.rendered_static}
    assert table, f"the render with {values} issues no NATS user (the dev seed is absent)"
    services = _services(docs)
    findings = [
        *_lane_runs_the_charts_images(tools, docs),
        *_table_names_every_client(table, services, set(bus.rendered.users)),
        *_data_grants_beyond_the_publishers(table),
    ]
    assert findings == [], "\n".join(findings)
    monkeypatch.setattr(seam, "_RECONNECT_ATTEMPTS", _RECONNECT_ATTEMPTS)
    monkeypatch.setattr(seam, "_RECONNECT_WAIT_SECONDS", _RECONNECT_WAIT_SECONDS)
    monkeypatch.setattr(seam, "_PAUSE_SECONDS", _PAUSE_SECONDS)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(seam, "tracer", provider.get_tracer(seam.__name__))
    caplog.set_level(logging.WARNING, logger=seam.__name__)
    measured: dict[str, float] = {}
    bus.start()
    try:
        rig = await bus.client(RIG, answer_within=10.0)
        findings += await _a_bus_holding_events_and_other_subscribers(bus, tmp_path, rig)
        assert findings == [], "\n".join(findings)
        undeclared, printed = await _a_consumer_not_declared_yet_is_pulled_once_declared(bus, docs, tmp_path, rig, caplog)
        findings += undeclared
        findings += _the_first_run_made_what_the_bus_lacked(printed, _declared(docs))
        findings += await _a_second_declare_changes_nothing(bus, docs, tmp_path, rig)
        findings += await _a_changed_lane_is_edited_in_place(bus, changed, tmp_path, rig)
        unreadable, blind = await _the_retire_phase_removes_nothing_it_could_not_read(bus, retired, tmp_path, rig)
        findings += unreadable
        findings += await _the_retire_phase_removes_what_git_declares_no_longer(bus, retired, tmp_path, rig)
        # The next reconcile's declare, after the retire: the lane keeps its place.
        findings += [f"after the retire: {finding}" for finding in await _a_second_declare_changes_nothing(bus, retired, tmp_path, rig)]
        findings += await _a_pod_presents_only_its_token(bus)
        matrix, foreign, overheard, probes = await _publish_matrix(bus, table, rig)
        findings += matrix
        outside, refused = await _calls_outside_a_row_are_refused(probes)
        findings += outside
        foreign |= refused
        async with _connected_as(bus, "catalog") as publisher:
            findings += await _lineages_ingest_lane(bus, publisher, exporter, rig)
            findings += await _the_catalogs_control_lane(bus, publisher, rig, caplog)
            findings += await _a_pulling_and_a_following_lane_deliver_again_after_an_outage(bus, publisher, rig, caplog)
        findings += await asyncio.to_thread(_monitor_reads, bus, tmp_path)
        findings += await _services_publish_as_themselves(bus, services, rig, probes)
        findings += await _a_service_outlasts_a_bus_it_cannot_reach(bus, services[0], rig, caplog)
        unheard_findings, refused_inbox = await _a_session_with_no_inbox_of_its_own_is_not_held(bus, rig)
        findings += unheard_findings
        overheard |= refused_inbox
        renewed, renewal = await _a_session_is_renewed_on_the_same_client(bus, rig, caplog)
        findings += renewed
        lost, regained = await _a_session_it_cannot_renew_is_opened_again(bus, rig, caplog)
        findings += lost
        measured |= renewal | regained
        findings += await _a_withdrawn_key_ends_its_users_sessions(bus, caplog)
        for client in [rig, *probes.values()]:
            await client.close()
    finally:
        bus.stop()
        log.warning("bus sessions measured: %s", json.dumps(measured, sort_keys=True))
    log_text = bus.broker.log_text()
    findings += _refusals_against_the_expected(log_text, bus, foreign, overheard, blind)
    findings += _grants_no_call_used(log_text, bus, table, foreign | overheard)
    findings += _the_runs_secrets_stayed_with_the_rig(bus)
    assert findings == [], "\n".join(findings)
