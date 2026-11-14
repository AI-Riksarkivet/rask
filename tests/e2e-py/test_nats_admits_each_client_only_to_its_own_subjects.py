"""Each NATS client reaches exactly its own subjects, and a client with no credential reaches nothing ([[XC-078]]).

Closes-when clause 1 ("a publish from a pod without its app's credential is refused on every cascade, control and
lineage subject"), proved offline: the nats-server the NATS fixture deploys (deploy/fixtures/nats,
a prerequisite rask never installs) in operator mode with a MEMORY resolver and JetStream, test-only keys minted with nsc, and one user per row of the permission table the chart's dev
seed issues (read off the render, so a values.yaml `nats.auth.flagged` grant counts only while its flag is on).

The table is held to two declarations below, written independently of it, because a table checked only against
itself can never be too broad. `PUBLISHERS` is who may publish each data subject, read off every publish call site
(each service's own connection, `lakehouse.bus`) and every `deadLetterTopic` (published by that app's own sidecar).
`DAPR_SUBSCRIPTIONS` is what each sidecar subscribes, read off every `dapr_app.subscribe`. A table that lets anyone
else publish a subject fails, and so does one that withholds a subject or a JetStream API call a client makes.

Each service's own connection is the seam itself ([[XC-199]]): the real `lakehouse.bus.Bus`, connected as the bus user
the render names that service, with the credential a stand-in of the secret store holds for it. Each publishes its
topics once under its event's id and reads a duplicate acknowledgement back, under an inbox no other user may read;
an id another publisher stored a message under is not taken as delivered; a bus it cannot reach fails its publishes at
once and is connected to again, by nats-py through a short cut and by the seam's own loop past it; and a credential
the bus ends is replaced by the one the store holds then, with no restart.

The Dapr half is measured rather than derived: the real daprd the Dapr fixture injects runs each Component shape (a
durable subscriber with a dead-letter topic, the catalog's per-replica broadcast subscriber) from the chart's own
render, delivering what the catalog's own connection published, and every subject it used must be one the replay of
that app's subscriptions also exercises, so the replay that covers every other app cannot drift from what Dapr does.

Run by `dagger call nats-auth` (`make nats-auth`), which provides the binaries and selects this file by path.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import secrets
import subprocess
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
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
    Link,
    Mint,
    Permissions,
    Sidecar,
    SubscriberApp,
    Tools,
    call_template,
    client_subjects,
    component_metadata,
    dapr_consumer_config,
    issue,
    mint,
    run_cli,
    run_stream_job,
    sidecar_component,
    violations,
)
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

from lakehouse import bus as seam
from lakehouse.bus import Bus, BusSettings, BusUnavailableError, bus_session, publish_event
from lakehouse.cloud_events import STRUCTURED_CONTENT_TYPE, CloudEvent
from lakehouse.supervision import Supervisor
from tests.secret_store_double import SecretStoreDouble
from tests.unit.chart_render import DEFAULT_ARGS, POD_NAME, containers, env_of, nats_users, render, render_fixture


pytestmark = pytest.mark.nats_auth

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[2]
#: The rig's own observer: it reads stream state and plants consumers for the Job's second pass, so no table user's
#: call set carries the test's bookkeeping. It is minted beside the table and is not a row of it.
RIG = "rask-test-rig"
#: `admin`'s grants without `$JS.API.CONSUMER.INFO`, so every consumer read the stream Job makes fails as a read during a
#: JetStream leader move does, while it may still list and delete ([[LH-343]]). Minted beside the table, not a row of it.
BLIND_ADMIN = "rask-test-blind-admin"
#: How long the broadcast sidecar's connection is cut: past an ephemeral consumer's 5 s inactivity threshold
#: (nats-server v2.14.2 `consumer.go:1462-1467`), well inside nats.go's 120 s reconnect budget (60 tries, 2 s apart).
LINK_DOWN_SECONDS = 10.0
#: The durable the replay binds for the catalog's broadcast, named as a replica named `replay-catalog` would name it.
BROADCAST_REPLAY = "catalog-control-replay-catalog"
#: How many times the stream Job runs before the lane gives up on it, inside the chart's `backoffLimit` of 20.
_JOB_RUNS = 3
#: The topics a service publishes on its own connection: its lineage events (`lakehouse.outbox`) and the control lane's.
OWN_CONNECTION_TOPICS = ("lineage.events.v1", "catalog.control.v1")
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
_RECONNECT_ATTEMPTS = 3
_RECONNECT_WAIT_SECONDS = 1
_PAUSE_SECONDS = (0.5, 1.0)


#: Who may publish each data subject; every other table user must be refused on it.
#:
#: lineage: catalog/core/lineage_emit.py, lineage/services/staged.py (recovered signed bytes), maintenance/core/
#: lineage_emit.py. control: catalog's control_emit and control_relay.py, plus the flagged announcer below. maintenance's
#: tasks, schedules and claims on tables: maintenance/services/queue.py, as the worker's own user. store.events: the development store's
#: change notifications (deploy/fixtures/rustfs), as its own user. dlq.<app>: the
#: `deadLetterTopic` of that app's subscriptions. probe.<app>: lakehouse/bus_watchdog.py, which each service runs over its
#: own sidecar's connections.


def _values() -> Path:
    return Path(os.environ.get(VALUES_ENV) or REPO / "chart" / "values.yaml")


def _control_emitters(values: Path) -> frozenset[str]:
    """Maintenance announces on catalog.control.v1 only while the flag that renders its control component is on
    (maintenance.controlEmit, false in the chart's values)."""
    chart = yaml.safe_load((REPO / "chart" / "values.yaml").read_text()) or {}
    given = yaml.safe_load(values.read_text()) or {}
    on = (given.get("maintenance") or {}).get("controlEmit", (chart.get("maintenance") or {}).get("controlEmit"))
    return frozenset({"maintenance"}) if on else frozenset()


PUBLISHERS: dict[str, frozenset[str]] = {
    "lineage.events.v1": frozenset({"catalog", "lineage", "maintenance"}),
    "catalog.control.v1": frozenset({"catalog", *_control_emitters(_values())}),
    "dlq.lineage.events": frozenset({"lineage"}),
    "dlq.maintenance.arrival": frozenset({"maintenance"}),
    "dlq.catalog.control": frozenset({"catalog"}),
    **{f"probe.{app}": frozenset({app}) for app in ("catalog", "lineage", "maintenance")},
    "maintenance.task.probe": frozenset({"maintenance-worker"}),
    "maintenance.schedule.probe": frozenset({"maintenance-worker"}),
    "$KV.MAINTENANCE_CLAIMS.probe": frozenset({"maintenance-worker"}),
    "store.events": frozenset({"object-store"}),
}
#: The subjects no stream captures, so an owner's publish is acknowledged by nobody and only the broker's log says
#: whether it was refused.
UNSTORED = frozenset(subject for subject in PUBLISHERS if subject.startswith("probe."))

#: What each sidecar subscribes, as (durableName, topic); a `{podName}` durable is one each replica names for its pod.
#: catalog/api/dapr.py; lineage/api/dapr.py; maintenance/api/arrival.py.
DAPR_SUBSCRIPTIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "catalog": ((f"catalog-control-{POD_NAME}", "catalog.control.v1"),),
    "lineage": (("lineage-durable", "lineage.events.v1"), ("lineage-dlq-durable", "dlq.lineage.events")),
    "maintenance": (("maintenance-durable", "lineage.events.v1"),),
}

#: The non-Dapr clients the table must also name: the stream Job, nats-box, maintenance's worker and the dev store.
NON_DAPR_USERS = frozenset({"admin", "monitor", "maintenance-worker", "object-store"})
#: JetStream's answer to a publish on a subject already holding the one message its stream keeps.
_ONE_PER_SUBJECT = 10077


def _probe_of(subject: str) -> str:
    return ".".join("probe" if token in {"*", ">"} else token for token in subject.split("."))


def _is_data_subject(subject: str) -> bool:
    """A subject that carries an application's data: not the JetStream API, not a reply inbox, and not $SYS.REQ.USER.INFO,
    the request in which a client asks the server who it is (monitor's `nats account info`)."""
    return not subject.startswith(("$JS.", "_INBOX.")) and subject != "$SYS.REQ.USER.INFO"


def _pubsub_components(docs: tuple[dict, ...]) -> list[dict[str, Any]]:
    return [d for d in docs if d.get("kind") == "Component" and d["spec"]["type"] == "pubsub.jetstream"]


def _named(components: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return next(c for c in components if c["metadata"]["name"] == name)


def _subscriber_component(components: list[dict[str, Any]], app: str, durable: str) -> list[dict[str, Any]]:
    """The Components scoped to `app` that carry `durable`."""
    return [c for c in components if app in c.get("scopes", []) and component_metadata(c).get("durableName") == durable]


def _lane_runs_the_charts_images(tools: Tools, docs: tuple[dict, ...]) -> list[str]:
    """Every binary the lane runs comes from an image the estate deploys: rask's render, or the prerequisites' fixtures (the
    NATS server, and the daprd image Dapr's injector stamps as SIDECAR_IMAGE)."""
    docs = (*docs, *render_fixture("nats"), *render_fixture("dapr"))
    deployed = {c["image"] for _, _, c in containers(docs)}
    deployed |= {e["value"] for _, _, c in containers(docs) for e in c.get("env") or [] if e.get("name") == "SIDECAR_IMAGE" and "value" in e}
    return [
        f"the lane runs {tool} from {image}, which neither the chart nor a fixture deploys"
        for tool, image in sorted(tools.images.items())
        if image not in deployed
    ]


def _table_names_every_client(table: dict[str, Permissions], components: list[dict[str, Any]], services: list[str]) -> list[str]:
    findings = [
        f"no table user for {user}, which the render needs a credential for"
        for user in sorted({s for c in components for s in c.get("scopes", [])} | NON_DAPR_USERS | set(services))
        if user not in table
    ]
    declared = {(app, durable) for app, subscriptions in DAPR_SUBSCRIPTIONS.items() for durable, _ in subscriptions}
    for component in components:
        durable = component_metadata(component).get("durableName")
        if durable and not any((app, durable) in declared for app in component.get("scopes", [])):
            findings.append(f"{component['metadata']['name']} carries durable {durable}, which DAPR_SUBSCRIPTIONS does not replay")
    for app, subscriptions in DAPR_SUBSCRIPTIONS.items():
        for durable, topic in subscriptions:
            if len(_subscriber_component(components, app, durable)) != 1:
                findings.append(f"{app}'s subscription to {topic} ({durable}) does not resolve to exactly one rendered Component")
    return findings


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
    carried = (_headers(kept[0]).get("Content-Type"), json.loads(base64.b64decode(kept[0]["data"])))
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
    topic, clock = OWN_CONNECTION_TOPICS[0], asyncio.get_running_loop().time
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
    topic = OWN_CONNECTION_TOPICS[0]
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
    topic, clock = OWN_CONNECTION_TOPICS[0], asyncio.get_running_loop().time
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


def _run_stream_job(tools: Tools, docs: tuple[dict, ...], broker: Broker, mint_: Mint, home: Path, user: str) -> subprocess.CompletedProcess[str] | str:
    """The chart's stream Job, run as `user` against this broker: its run, or why it did not finish."""
    job = next(d for d in docs if d.get("kind") == "Job" and "-nats-stream-" in d["metadata"]["name"])
    script = job["spec"]["template"]["spec"]["containers"][0]["command"][2]
    rendered_url = component_metadata(_pubsub_components(docs)[0])["natsURL"]
    if rendered_url not in script:
        return f"the stream Job's script does not name the broker as {rendered_url}"
    try:
        return run_stream_job(tools.job_cli, script.replace(rendered_url, broker.url), mint_.credentials[user], home, 300.0)
    except subprocess.TimeoutExpired as exc:
        return f"the stream Job did not finish as {user} (its readiness loop retries a refused connection): {str(exc.stdout or '')[-600:]}"


def _stream_job(tools: Tools, docs: tuple[dict, ...], broker: Broker, mint_: Mint, home: Path) -> list[str]:
    """The chart's stream Job, run as `admin` against this broker; it must finish and exit 0, within the runs Kubernetes would
    give it: a failed run is run again (the chart's `backoffLimit`), since creating a stream copied to three servers can
    outlast the CLI's request deadline on a loaded host (measured 2026-11-09), and the Job is idempotent."""
    for _ in range(_JOB_RUNS):
        done = _run_stream_job(tools, docs, broker, mint_, home, "admin")
        if isinstance(done, str):
            return [done]
        if done.returncode == 0:
            return []
    return [f"the stream Job exited {done.returncode} as admin on each of {_JOB_RUNS} runs: {(done.stdout + done.stderr)[-800:]}"]


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
    """An owner's publish of one of its subjects: acknowledged when a stream captures it, the broker's log judging the
    rest. Nothing when it lands; the finding when it is refused."""
    if subject in UNSTORED:
        await client.nc.publish(subject, json.dumps({"probe": user}).encode())
        return []
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
    """One acknowledged publish by an owner of every stored subject, sent inside the refused-publish window.

    A deploy's own producers keep publishing while the matrix runs, and the broadcast stream mirrors the control stream
    behind its writes, so a stream can take a permitted message at any moment of the window. Every run carries one on
    each stream, so the check is held to telling a permitted message from a foreign one rather than to an empty window.
    """
    findings: list[str] = []
    for subject, owners in sorted(PUBLISHERS.items()):
        owner = next((user for user in sorted(owners) if user in clients), None)
        if subject not in UNSTORED and owner is not None:
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
    universe = sorted(set(PUBLISHERS) | {_probe_of(s) for p in table.values() for s in p.publish if _is_data_subject(s)})
    findings: list[str] = []
    for user, client in clients.items():
        for subject in (s for s in universe if user in PUBLISHERS.get(s, ())):
            findings += await _owner_publish(client, user, subject)
    foreign = {(user, s) for user in clients for s in universe if user not in PUBLISHERS.get(s, ())}
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

    A permitted publisher's message (an owner's, or the broadcast stream's mirrored copy of one) may land in the window and
    proves nothing; one carrying this run's marker is a publish the broker had to refuse, and one the rig cannot read is
    not shown to be anything else.
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


async def _publish_until_delivered(publisher: Bus, topic: str, app: SubscriberApp, route: str, timeout: float = 30.0) -> str | None:
    """Publish on `publisher`, the catalog's own connection, until `app` receives one of these publishes on `route`: a
    `deliverPolicy: new` consumer drops what precedes it, and a `deliverPolicy: all` one first delivers what the stream
    already held."""
    deadline = asyncio.get_running_loop().time() + timeout
    for attempt in range(1_000):
        await publish_event(publisher, _lane_event(marker=f"{topic}#{attempt}"), topic=topic, timeout_seconds=5)
        await asyncio.sleep(0.5)
        if delivered := [marker for marker in app.markers(route) if marker.startswith(f"{topic}#")]:
            return delivered[0]
        if asyncio.get_running_loop().time() > deadline:
            break
    return None


async def _a_delivery_carries_the_trace_its_event_was_published_under(publisher: Bus, topic: str, app: SubscriberApp, route: str) -> list[str]:
    """An event published inside a span reaches a sidecar's app under that span's trace: the envelope carries the
    publish's ``traceparent``, and daprd makes its request to the app under the same trace id."""
    published = SpanContext(trace_id=int(secrets.token_hex(16), 16), span_id=int(secrets.token_hex(8), 16), is_remote=False, trace_flags=TraceFlags(1))
    marker = f"traced-{secrets.token_hex(4)}"
    with trace.use_span(NonRecordingSpan(published)):
        await publish_event(publisher, _lane_event(marker=marker), topic=topic, timeout_seconds=5)
    if not await _until(lambda: marker in app.markers(route), within=15):
        return [f"a publish on {topic} inside a span never reached {route}"]
    delivery = next(d for d in list(app.deliveries) if d.route == route and isinstance(d.body.get("data"), dict) and d.body["data"].get("marker") == marker)
    wanted = f"{published.trace_id:032x}"
    carried = (_trace_id(delivery.body.get("traceparent")), _trace_id(delivery.traceparent))
    if carried != (wanted, wanted):
        return [
            f"an event published under trace {wanted} reached {route} with {carried[0] or 'no trace'} in its envelope, in a request under {carried[1] or 'no trace'}"
        ]
    return []


def _trace_id(traceparent: object) -> str:
    """The trace id a W3C ``traceparent`` names, or nothing for a value that is none."""
    parts = str(traceparent or "").split("-")
    return parts[1] if len(parts) == 4 else ""


async def _unacked(rig: Client, streams: tuple[str, ...], metadata: dict[str, str]) -> list[str]:
    """A durable daprd has handled must hold no unacked delivery once it settles; an unacked one is redelivered."""
    durable, findings = metadata["durableName"], []
    for stream in streams:
        pending = -1
        for _ in range(20):
            pending = int(((await rig.request(f"$JS.API.CONSUMER.INFO.{stream}.{durable}")) or {}).get("num_ack_pending", -1))
            if pending == 0:
                break
            await asyncio.sleep(0.25)
        else:
            findings.append(
                f"{stream}/{durable} holds {pending} unacked deliveries after daprd handled them: the broker redelivers each "
                f"after ackWait {metadata.get('ackWait')}, up to maxDeliver {metadata.get('maxDeliver')}"
            )
    return findings


async def _daprd_shapes(tools: Tools, broker: Broker, mint_: Mint, components: list[dict[str, Any]], rig: Client, workdir: Path) -> list[str]:
    """One real daprd per Component shape, each delivering to its app, through the chart's own Component, what the catalog
    published on its own connection."""
    findings: list[str] = []
    creds = mint_.credentials
    # Lineage's durable ingest, whose dead letters come back to its parking route on a Component of their own
    # (`lineage.api.dapr.register_dapr`).
    ingest = _subscriber_component(components, "lineage", "lineage-durable")[0]
    parking = _subscriber_component(components, "lineage", "lineage-dlq-durable")[0]
    subscriber_app = SubscriberApp(
        [
            {"pubsubname": ingest["metadata"]["name"], "topic": "lineage.events.v1", "route": "/lineage-events", "deadLetterTopic": "dlq.lineage.events"},
            {"pubsubname": parking["metadata"]["name"], "topic": "dlq.lineage.events", "route": "/lineage-dlq"},
        ],
        failing=frozenset({"/lineage-events"}),
    )
    broadcast_app = SubscriberApp(
        [{"pubsubname": "catalog-control-pubsub", "topic": "catalog.control.v1", "route": "/control-events", "deadLetterTopic": "dlq.catalog.control"}]
    )
    link = Link(broker.port)
    await link.start()
    sidecars = [
        Sidecar(
            tools.daprd,
            "lineage",
            [sidecar_component(component, broker.url, creds["lineage"]) for component in (ingest, parking)],
            subscriber_app,
            workdir / "subscriber",
        ),
        Sidecar(
            tools.daprd,
            "catalog",
            [sidecar_component(_named(components, "catalog-control-pubsub"), link.url, creds["catalog"])],
            broadcast_app,
            workdir / "broadcast",
        ),
    ]
    try:
        for sidecar in sidecars:
            await sidecar.start()
    except (RuntimeError, TimeoutError) as exc:
        findings.append(f"daprd did not start: {exc}")
        for sidecar in sidecars:
            sidecar.stop()
            sidecar.app.stop()
        await link.stop()
        return findings
    try:
        async with _connected_as("catalog", broker.url) as publisher:
            marker = await _publish_until_delivered(publisher, "lineage.events.v1", subscriber_app, "/lineage-events")
            if marker is None:
                findings.append("the catalog's own connection -> daprd lineage (durable): nothing reached /lineage-events")
            else:
                for _ in range(60):
                    if marker in subscriber_app.markers("/lineage-dlq"):
                        break
                    await asyncio.sleep(0.25)
                else:
                    findings.append(f"daprd lineage never dead-lettered {marker} through dlq.lineage.events back to itself")
                findings += await _unacked(rig, ("LINEAGE",), component_metadata(ingest))
                findings += await _unacked(rig, ("DLQ",), component_metadata(parking))
            if await _publish_until_delivered(publisher, "catalog.control.v1", broadcast_app, "/control-events") is None:
                findings.append("the catalog's own connection -> daprd catalog (broadcast): nothing reached /control-events")
            else:
                findings += await _a_delivery_carries_the_trace_its_event_was_published_under(publisher, "catalog.control.v1", broadcast_app, "/control-events")
                # The broadcast's connection is cut for longer than an ephemeral consumer outlives lost interest, and shorter
                # than nats.go's reconnect budget, so the sidecar reconnects rather than closing ([[LH-305]]).
                await link.cut(LINK_DOWN_SECONDS)
                before = set(broadcast_app.markers("/control-events"))
                for attempt in range(120):
                    await publish_event(publisher, _lane_event(marker=f"after-cut#{attempt}"), topic="catalog.control.v1", timeout_seconds=5)
                    await asyncio.sleep(0.5)
                    if set(broadcast_app.markers("/control-events")) - before:
                        break
                else:
                    findings.append(f"daprd catalog (broadcast): nothing reached /control-events once its connection came back after {LINK_DOWN_SECONDS} s")
    finally:
        for sidecar in sidecars:
            sidecar.stop()
            sidecar.app.stop()
        await link.stop()
    for sidecar in sidecars:
        findings += [f"daprd {sidecar.app_id} logged: {line.strip()[:300]}" for line in sidecar.log_text().splitlines() if "violation" in line.lower()]
    return findings


async def _replay_subscriptions(url: str, mint_: Mint, components: list[dict[str, Any]], publishers: dict[str, Client]) -> list[str]:
    """Each app's subscriptions, created and bound exactly as Dapr 1.18.1's pubsub.jetstream does, then one delivery acked."""
    findings: list[str] = []
    for app, subscriptions in DAPR_SUBSCRIPTIONS.items():
        client = await Client.connect(url, mint_.credentials[app], f"replay-{app}")
        for durable, topic in subscriptions:
            metadata = component_metadata(_subscriber_component(components, app, durable)[0])
            label = f"{app} {topic} ({durable})"
            durable = durable.replace(POD_NAME, f"replay-{app}")
            metadata = {**metadata, "durableName": durable}
            if not (stream := metadata.get("streamName", "")):
                names = await client.request("$JS.API.STREAM.NAMES", {"subject": topic})
                if not names or len(names.get("streams") or []) != 1:
                    findings.append(f"{label}: STREAM.NAMES answered {names}")
                    continue
                stream = names["streams"][0]
            if await client.request(f"$JS.API.CONSUMER.INFO.{stream}.{durable}") is None:
                findings.append(f"{label}: CONSUMER.INFO.{stream}.{durable} was not answered")
            create = f"$JS.API.CONSUMER.CREATE.{stream}.{durable}.{topic}"
            created = await client.request(create, {"stream_name": stream, "config": dapr_consumer_config(metadata, topic, client.nc.new_inbox())})
            if created is None:
                findings.append(f"{label}: {create} was not answered")
                continue
            bound = await client.request(f"$JS.API.CONSUMER.INFO.{stream}.{durable}")
            if not bound or "config" not in bound:
                findings.append(f"{label}: CONSUMER.INFO.{stream}.{durable} answered {bound}")
                continue
            sub = await client.nc.subscribe(bound["config"]["deliver_subject"], queue=bound["config"].get("deliver_group") or "")
            try:
                await publishers[min(PUBLISHERS[topic])].js.publish(topic, json.dumps({"replay": label}).encode(), timeout=3)
                message = await sub.next_msg(timeout=5)
            except Exception as exc:  # noqa: BLE001 — a refused publish times out, an undelivered message too
                findings.append(f"{label}: nothing was delivered to the bound consumer ({type(exc).__name__})")
            else:
                await message.ack()
            await sub.unsubscribe()
        await client.settle()
        await client.close()
    return findings


async def _stream_job_repairs(tools: Tools, docs: tuple[dict, ...], broker: Broker, mint_: Mint, rig: Client, home: Path) -> list[str]:
    """The Job's second run, over an orphan durable and a durable whose redelivery config drifted, both of which it must
    delete as `admin` (the sidecar re-creates the drifted one with the component's config), and over a broadcast mirror whose
    consumer limit drifted, which it must set back to the chart's value."""
    arrival = component_metadata(_subscriber_component(_pubsub_components(docs), "maintenance", "maintenance-durable")[0])
    orphan = {"durable_name": "orphan-durable", "ack_policy": "explicit", "deliver_policy": "new", "filter_subject": "lineage.events.v1"}
    await rig.request("$JS.API.CONSUMER.CREATE.LINEAGE.orphan-durable.lineage.events.v1", {"stream_name": "LINEAGE", "config": orphan})
    drifted = ((await rig.request("$JS.API.CONSUMER.INFO.LINEAGE.maintenance-durable")) or {}).get("config", {})
    drifted["max_deliver"] = int(arrival["maxDeliver"]) + 1
    update = {"stream_name": "LINEAGE", "config": drifted, "action": "update"}
    await rig.request("$JS.API.CONSUMER.CREATE.LINEAGE.maintenance-durable.lineage.events.v1", update)
    mirror = ((await rig.request("$JS.API.STREAM.INFO.CATALOG_CONTROL_BROADCAST")) or {}).get("config", {})
    declared = mirror.get("consumer_limits", {}).get("inactive_threshold")
    # Drifted above the chart's value, a replica's durable with it, so the Job must lower the durable before the server
    # lets the mirror's limit come down (nats-server v2.14.2 `stream.go:2420-2446`).
    above = 2 * int(declared or 0)
    limits = {**mirror.get("consumer_limits", {}), "inactive_threshold": above}
    drifted_mirror = await rig.request("$JS.API.STREAM.UPDATE.CATALOG_CONTROL_BROADCAST", {**mirror, "consumer_limits": limits})
    replica = ((await rig.request(f"$JS.API.CONSUMER.INFO.CATALOG_CONTROL_BROADCAST.{BROADCAST_REPLAY}")) or {}).get("config", {})
    raised = {"stream_name": "CATALOG_CONTROL_BROADCAST", "config": {**replica, "inactive_threshold": above}, "action": "update"}
    drifted_replica = await rig.request(f"$JS.API.CONSUMER.CREATE.CATALOG_CONTROL_BROADCAST.{BROADCAST_REPLAY}.catalog.control.v1", raised)
    planted = (
        ((drifted_mirror or {}).get("config") or {}).get("consumer_limits", {}).get("inactive_threshold"),
        ((drifted_replica or {}).get("config") or {}).get("inactive_threshold"),
    )
    assert planted == (above, above), f"the rig could not drift the broadcast mirror and a replica's durable: {drifted_mirror}, {drifted_replica}"
    findings = await asyncio.to_thread(_stream_job, tools, docs, broker, mint_, home)
    mirror = ((await rig.request("$JS.API.STREAM.INFO.CATALOG_CONTROL_BROADCAST")) or {}).get("config", {})
    if not declared or mirror.get("consumer_limits", {}).get("inactive_threshold") != declared:
        findings.append(f"the stream Job left CATALOG_CONTROL_BROADCAST's consumer limit at {mirror.get('consumer_limits')}, not the chart's {declared}")
    for durable in ("orphan-durable", "maintenance-durable"):
        gone = (await rig.request(f"$JS.API.CONSUMER.INFO.LINEAGE.{durable}") or {}).get("error") or {}
        if gone.get("err_code") != 10014:
            findings.append(f"the stream Job did not delete {durable} as admin")
    return findings


async def _judged_durables(rig: Client, docs: tuple[dict, ...]) -> set[tuple[str, str]]:
    """The durables the Job's drift passes judge: every `-durable` consumer on the streams they walk."""
    script = next(d for d in docs if d.get("kind") == "Job" and "-nats-stream-" in d["metadata"]["name"])["spec"]["template"]["spec"]["containers"][0][
        "command"
    ][2]
    walked = re.search(r"for st in ([A-Z_ ]+); do", script)
    assert walked, "the drift loop's `for st in` line is not in the rendered Job"
    return {
        (stream, name)
        for stream in walked.group(1).split()
        for name in (await rig.request(f"$JS.API.CONSUMER.NAMES.{stream}") or {}).get("consumers") or []
        if name.endswith("-durable")
    }


async def _stream_job_converges_a_changed_ack_wait(tools: Tools, values: Path, broker: Broker, mint_: Mint, rig: Client, home: Path) -> list[str]:
    """The Job run again on a render whose `maintenance.queue.ackWaitSeconds` changed, as an upgrade runs it: as `admin`
    it converges the claims bucket's max age and the worker's consumer to the new ack wait, so neither is left on the
    old one beside pods that vouch for the new."""
    changed = render(*DEFAULT_ARGS, "-f", str(values), "--set", "maintenance.queue.ackWaitSeconds=120")
    findings = await asyncio.to_thread(_stream_job, tools, changed, broker, mint_, home)
    bucket = ((await rig.request("$JS.API.STREAM.INFO.KV_MAINTENANCE_CLAIMS")) or {}).get("config", {})
    consumer = ((await rig.request("$JS.API.CONSUMER.INFO.MAINTENANCE_TASKS.maintenance-worker")) or {}).get("config", {})
    if bucket.get("max_age") != 120_000_000_000:
        findings.append(f"the stream Job left the claims bucket's max age at {bucket.get('max_age')} for an ack wait of 120 s")
    if consumer.get("ack_wait") != 120_000_000_000:
        findings.append(f"the stream Job left the worker's consumer's ack wait at {consumer.get('ack_wait')} for 120 s")
    return findings


async def _stream_job_deletes_nothing_it_could_not_read(
    tools: Tools, docs: tuple[dict, ...], broker: Broker, mint_: Mint, rig: Client, home: Path
) -> list[str]:
    """A Job run whose every consumer read fails must fail and delete nothing: a durable it could not read is not one that
    drifted, and the sidecar would re-create a deleted one at `deliverPolicy: new`, skipping what was published between."""
    before = await _judged_durables(rig, docs)
    assert before, "no durable sits on a stream the drift passes walk, so this leg judges nothing"
    done = await asyncio.to_thread(_run_stream_job, tools, docs, broker, mint_, home, BLIND_ADMIN)
    if isinstance(done, str):
        return [done]
    findings = [f"the stream Job deleted {stream}/{name}, whose info it could not read" for stream, name in sorted(before - await _judged_durables(rig, docs))]
    if done.returncode == 0:
        findings.append(f"the stream Job exited 0 though it could read no consumer: {(done.stdout + done.stderr)[-600:]}")
    return findings


def _monitor_reads(tools: Tools, broker: Broker, mint_: Mint, home: Path) -> list[str]:
    """nats-box's read-only operator view, as `monitor`."""
    findings: list[str] = []
    for args in (["stream", "ls"], ["stream", "info", "LINEAGE"], ["consumer", "ls", "LINEAGE"], ["consumer", "info", "LINEAGE", "lineage-durable"]):
        done = run_cli(tools.box_cli, args, mint_.credentials["monitor"], broker.url, home)
        if done.returncode:
            findings.append(f"nats {' '.join(args)} as monitor exited {done.returncode}: {(done.stdout + done.stderr)[-400:]}")
    return findings


def _refusals_against_the_matrix(log_text: str, mint_: Mint, foreign: set[tuple[str, str]], overheard: set[tuple[str, str]]) -> list[str]:
    """The broker must have refused every foreign publish and every subscription to another user's inbox, and nothing
    else: any other refusal is a missing allowance."""
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


def _daprd_calls_the_replay_misses(log_text: str, mint_: Mint, components: list[dict[str, Any]]) -> list[str]:
    """Each daprd connection's subjects, against the subjects the test's own clients exercised as the same user."""
    durables = {component_metadata(c)["durableName"] for c in components if "durableName" in component_metadata(c)}
    connection_names = {component_metadata(c)["name"] for c in components}
    measured: dict[tuple[str, str], set[str]] = {}
    exercised: dict[str, set[str]] = {}
    for (user_key, connection), subjects in client_subjects(log_text).items():
        user, templates = mint_.user_of(user_key), {call_template(s, durables) for s in subjects}
        if connection.rsplit(":", 1)[-1] in connection_names and ":go:" in connection:
            measured.setdefault((user, connection), set()).update(templates)
        else:
            exercised.setdefault(user, set()).update(templates)
    for (user, connection), templates in sorted(measured.items()):
        log.info("daprd %s as %s used %s", connection, user, sorted(templates))
    return [
        f"daprd {connection} used {template} as {user}, which the replay never exercises"
        for (user, connection), templates in sorted(measured.items())
        for template in sorted(templates - exercised.get(user, set()))
    ]


@pytest.mark.asyncio
async def test_nats_admits_each_client_only_to_its_own_subjects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, secret_store: SecretStoreDouble) -> None:
    tools = Tools.from_env(os.environ)
    values = _values()
    docs = render(*DEFAULT_ARGS, "-f", str(values))
    table = {user: Permissions(publish=tuple(sorted(pub)), subscribe=tuple(sorted(sub))) for user, (pub, sub) in nats_users(docs).items()}
    assert table, f"the render with {values} issues no NATS user (the dev seed is absent)"
    components = _pubsub_components(docs)
    services = _services(docs)
    findings = [*_lane_runs_the_charts_images(tools, docs), *_table_names_every_client(table, components, services)]
    assert findings == [], "\n".join(findings)
    blind = Permissions(publish=tuple(s for s in table["admin"].publish if not s.startswith("$JS.API.CONSUMER.INFO.")), subscribe=table["admin"].subscribe)
    mint_ = mint(tools.nsc, {**table, RIG: Permissions(publish=("$JS.API.>",), subscribe=("_INBOX.>",)), BLIND_ADMIN: blind}, tmp_path / "nsc")
    # The store's stand-in holds each service's credential, as the dev seed writes it there; the rig's own HTTP calls, all
    # to loopback, pass it by.
    secret_store.router.route(host="127.0.0.1").pass_through()
    held = {user: secret_store.hold(f"nats-user-{user}", {"jwt": mint_.credentials[user].jwt, "seed": mint_.credentials[user].seed()}) for user in services}
    monkeypatch.setattr(seam, "_RECONNECT_ATTEMPTS", _RECONNECT_ATTEMPTS)
    monkeypatch.setattr(seam, "_RECONNECT_WAIT_SECONDS", _RECONNECT_WAIT_SECONDS)
    monkeypatch.setattr(seam, "_PAUSE_SECONDS", _PAUSE_SECONDS)
    broker = Broker(tools.nats_server, mint_, tmp_path / "broker")
    broker.start()
    try:
        findings += await asyncio.to_thread(_stream_job, tools, docs, broker, mint_, tmp_path)
        rig = await Client.connect(broker.url, mint_.credentials[RIG], RIG, answer_within=10.0)
        findings += await _without_credential(broker.url)
        matrix, foreign, overheard, probes = await _publish_matrix(broker.url, mint_, table, rig)
        findings += matrix
        # Started here and judged last: its first credential takes a minute to run out.
        reissued = asyncio.create_task(_a_credential_the_bus_ends_is_replaced_by_the_stores_current_one(tools, tmp_path / "nsc", broker, secret_store, rig))
        try:
            findings += await _daprd_shapes(tools, broker, mint_, components, rig, tmp_path / "daprd")
            findings += await _replay_subscriptions(broker.url, mint_, components, probes)
            findings += await _stream_job_repairs(tools, docs, broker, mint_, rig, tmp_path)
            findings += await _stream_job_deletes_nothing_it_could_not_read(tools, docs, broker, mint_, rig, tmp_path)
            findings += await _stream_job_converges_a_changed_ack_wait(tools, values, broker, mint_, rig, tmp_path)
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
        for client in [rig, *probes.values()]:
            await client.close()
    finally:
        broker.stop()
    findings += _refusals_against_the_matrix(broker.log_text(), mint_, foreign, overheard)
    findings += _daprd_calls_the_replay_misses(broker.log_text(), mint_, components)
    assert findings == [], "\n".join(findings)
