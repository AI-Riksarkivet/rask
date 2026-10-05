"""Each NATS client reaches exactly its own subjects, and a client with no credential reaches nothing ([[XC-078]]).

Closes-when clause 1 ("a publish from a pod without its app's credential is refused on every cascade, control and
lineage subject") and the ingest clause ("ingest drains a run through its own NATS user, and a pod without that
user cannot publish on `ingest.tasks.>`"), proved offline: the chart's nats-server in operator mode with a MEMORY
resolver and JetStream, test-only keys minted with nsc, and one user per row of the permission table the chart's dev
seed issues (read off the render, so a values.yaml `nats.auth.flagged` grant counts only while its flag is on).

The table is held to two declarations below, written independently of it, because a table checked only against
itself can never be too broad. `PUBLISHERS` is who may publish each data subject, read off every Dapr publish call
site, every `deadLetterTopic` (published by that app's own sidecar) and the raw ingest client. `DAPR_SUBSCRIPTIONS`
is what each sidecar subscribes, read off every `dapr_app.subscribe`. A table that lets anyone else publish a
subject fails, and so does one that withholds a subject or a JetStream API call a client makes.

The Dapr half is measured rather than derived: real daprd 1.18.1 runs each Component shape (publisher-only, durable
subscriber with a dead-letter topic, the catalog's ephemeral broadcast subscriber) from the chart's own render, and
every subject it used must be one the replay of that app's subscriptions also exercises, so the replay that covers
every other app cannot drift from what Dapr does.

Run by `dagger call nats-auth` (`make nats-auth`), which provides the binaries and selects this file by path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import nats
import pytest
import yaml
from nats_auth_rig import (
    VALUES_ENV,
    Broker,
    Client,
    Mint,
    Permissions,
    Sidecar,
    SubscriberApp,
    Tools,
    call_template,
    client_subjects,
    component_metadata,
    dapr_consumer_config,
    ignore_refusal,
    mint,
    run_cli,
    run_stream_job,
    sidecar_component,
    violations,
)

from ingest.lander import create_empty
from ingest.queue import UnitTask, WorkQueue
from ingest.runtime import BRONZE_SCHEMA
from ingest.worker import Worker
from tests.unit.chart_render import DEFAULT_ARGS, containers, nats_users, render


pytestmark = pytest.mark.nats_auth

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[2]
RUNNERS = ("bronze-to-silver", "silver-to-gold", "media-to-silver")
#: The rig's own observer: it reads stream state and plants consumers for the Job's second pass, so no table user's
#: call set carries the test's bookkeeping. It is minted beside the table and is not a row of it.
RIG = "rask-test-rig"


#: Who may publish each data subject; every other table user must be refused on it.
#:
#: lineage: catalog/core/lineage_emit.py, lineage/services/staged.py (recovered signed bytes), maintenance/core/
#: lineage_emit.py, medallion/core/lineage_publish.py (producer and stage runners). control: catalog's control_emit
#: and control_relay.py, medallion/workflow.py `request_approval`, which runs in the producer's promotion_review
#: workflow, and each stage runner's plan announcement (stage_plans.py) under medallion.ray. maintenance work:
#: catalog/endpoints/maintenance.py and maintenance/services/work_queue.py; index: catalog only. medallion.<tier>: the
#: producer (ingest_trigger, publication_trigger, rerun, media_produce) and each stage runner's hand-off to its own
#: topic (stage_plans.py); a stage runner never publishes downstream, promotion goes through
#: the catalog (transform.py). medallion.promotion: promotion_hold.py in the stage runners. refused.<app>:
#: transform.py. dlq.<app>: the `deadLetterTopic` of that app's subscriptions. ingest: queue.py.
def _values() -> Path:
    return Path(os.environ.get(VALUES_ENV) or REPO / "chart" / "values.yaml")


def _control_emitters(values: Path) -> frozenset[str]:
    """Maintenance and the annotator announce on catalog.control.v1 only while the flag that renders their control
    component is on (maintenance.controlEmit, explorer.controlEmit; both false in the chart's values), and the stage
    runners only while the Ray lane writes (medallion.ray and medallion.compute; both true in the chart's values)."""
    chart = yaml.safe_load((REPO / "chart" / "values.yaml").read_text()) or {}
    given = yaml.safe_load(values.read_text()) or {}

    def on(section: str) -> bool:
        return bool((given.get(section) or {}).get("controlEmit", (chart.get(section) or {}).get("controlEmit")))

    announcers = frozenset(app for app, section in (("maintenance", "maintenance"), ("annotator", "explorer")) if on(section))
    medallion = {**(chart.get("medallion") or {}), **(given.get("medallion") or {})}
    # Each stage runner announces its Ray runs' plans while the Ray lane writes (medallion.ray with medallion.compute).
    return announcers | (frozenset(RUNNERS) if medallion.get("ray") and medallion.get("compute") else frozenset())


PUBLISHERS: dict[str, frozenset[str]] = {
    "lineage.events.v1": frozenset({"catalog", "lineage", "maintenance", "medallion-producer", *RUNNERS}),
    "catalog.control.v1": frozenset({"catalog", "medallion-producer", *_control_emitters(_values())}),
    "maintenance.work.v1": frozenset({"catalog", "maintenance"}),
    "maintenance.index.v1": frozenset({"catalog"}),
    "medallion.bronze": frozenset({"medallion-producer", "bronze-to-silver"}),
    "medallion.silver": frozenset({"medallion-producer", "silver-to-gold"}),
    "medallion.media": frozenset({"medallion-producer", "media-to-silver"}),
    "medallion.promotion": frozenset(RUNNERS),
    "training.jobs": frozenset({"medallion-producer"}),
    "refused.medallion-producer": frozenset({"medallion-producer"}),
    **{f"refused.{runner}": frozenset({runner}) for runner in RUNNERS},
    "dlq.lineage.events": frozenset({"lineage"}),
    "dlq.notifications": frozenset({"notifications"}),
    "dlq.medallion-producer": frozenset({"medallion-producer"}),
    **{f"dlq.{runner}": frozenset({runner}) for runner in RUNNERS},
    "dlq.maintenance.work": frozenset({"maintenance"}),
    "dlq.maintenance.index": frozenset({"maintenance"}),
    "dlq.ingest.tasks": frozenset({"ingest"}),
    "ingest.tasks.probe": frozenset({"ingest"}),
}

#: What each sidecar subscribes, as (durableName, topic); None is the catalog's ephemeral broadcast consumer.
#: catalog/api/dapr.py; lineage/api/dapr.py; medallion/api/{bronze_arrival,train,promotions,events,dlq}.py;
#: notifications/api/{subscriptions,dlq}.py; maintenance/api/{arrival,work,index_work}.py.
DAPR_SUBSCRIPTIONS: dict[str, tuple[tuple[str | None, str], ...]] = {
    "catalog": ((None, "catalog.control.v1"),),
    "lineage": (("lineage-durable", "lineage.events.v1"), ("lineage-dlq-durable", "dlq.lineage.events")),
    "medallion-producer": (
        ("medallion-producer-durable", "lineage.events.v1"),
        ("medallion-producer-durable", "training.jobs"),
        ("medallion-producer-durable", "medallion.promotion"),
        ("medallion-producer-durable", "dlq.medallion-producer"),
        ("medallion-producer-control-durable", "catalog.control.v1"),
    ),
    **{
        runner: ((f"{runner}-durable", topic), (f"{runner}-durable", f"dlq.{runner}"))
        for runner, topic in zip(RUNNERS, ("medallion.bronze", "medallion.silver", "medallion.media"), strict=True)
    },
    "notifications": (
        ("notifications-durable", "lineage.events.v1"),
        ("notifications-durable", "dlq.notifications"),
        ("notifications-control-durable", "catalog.control.v1"),
    ),
    "maintenance": (
        ("maintenance-durable", "lineage.events.v1"),
        ("maintenance-work-durable", "maintenance.work.v1"),
        ("maintenance-index-durable", "maintenance.index.v1"),
    ),
}

#: The non-Dapr clients the table must also name: the stream Job, nats-box, and ingest's raw client.
NON_DAPR_USERS = frozenset({"admin", "monitor", "ingest"})


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


def _subscriber_component(components: list[dict[str, Any]], app: str, durable: str | None) -> list[dict[str, Any]]:
    """The Components scoped to `app` that carry `durable`; for None, its broadcast one (a deliver policy, no durable)."""
    return [
        c
        for c in components
        if app in c.get("scopes", [])
        and component_metadata(c).get("durableName") == durable
        and (durable is not None or "deliverPolicy" in component_metadata(c))
    ]


def _lane_runs_the_charts_images(tools: Tools, docs: tuple[dict, ...]) -> list[str]:
    deployed = {c["image"] for _, _, c in containers(docs)}
    deployed |= {e["value"] for _, _, c in containers(docs) for e in c.get("env") or [] if e.get("name") == "SIDECAR_IMAGE" and "value" in e}
    return [f"the lane runs {tool} from {image}, which the chart does not deploy" for tool, image in sorted(tools.images.items()) if image not in deployed]


def _table_names_every_client(table: dict[str, Permissions], components: list[dict[str, Any]]) -> list[str]:
    findings = [
        f"no table user for {user}, which the render needs a credential for"
        for user in sorted({s for c in components for s in c.get("scopes", [])} | NON_DAPR_USERS)
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
                findings.append(f"{app}'s subscription to {topic} ({durable or 'ephemeral'}) does not resolve to exactly one rendered Component")
    return findings


def _stream_job(tools: Tools, docs: tuple[dict, ...], broker: Broker, mint_: Mint, home: Path) -> list[str]:
    """The chart's stream Job, run as `admin` against this broker; it must finish and exit 0."""
    job = next(d for d in docs if d.get("kind") == "Job" and "-nats-stream-" in d["metadata"]["name"])
    script = job["spec"]["template"]["spec"]["containers"][0]["command"][2]
    rendered_url = component_metadata(_pubsub_components(docs)[0])["natsURL"]
    if rendered_url not in script:
        return [f"the stream Job's script does not name the broker as {rendered_url}"]
    try:
        done = run_stream_job(tools.job_cli, script.replace(rendered_url, broker.url), mint_.credentials["admin"], home)
    except subprocess.TimeoutExpired as exc:
        return [f"the stream Job did not finish as admin (its readiness loop retries a refused connection): {str(exc.stdout or '')[-600:]}"]
    return [] if done.returncode == 0 else [f"the stream Job exited {done.returncode} as admin: {(done.stdout + done.stderr)[-800:]}"]


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


async def _publish_matrix(url: str, mint_: Mint, table: dict[str, Permissions], rig: Client) -> tuple[list[str], set[tuple[str, str]], dict[str, Client]]:
    """Every table user against every data subject: an owner's publish is acked, anyone else's must not land.

    Returns the publishes that must have been refused; the broker's log, read once it stops, says whether they were.
    """
    clients = {user: await Client.connect(url, mint_.credentials[user], f"probe-{user}") for user in table}
    universe = sorted(set(PUBLISHERS) | {_probe_of(s) for p in table.values() for s in p.publish if _is_data_subject(s)})
    findings: list[str] = []
    for user, client in clients.items():
        for subject in (s for s in universe if user in PUBLISHERS.get(s, ())):
            try:
                await client.js.publish(subject, json.dumps({"probe": user}).encode(), timeout=3)
            except Exception as exc:  # noqa: BLE001 — any failure to land is the finding
                findings.append(f"{user} publishes {subject} and was refused ({type(exc).__name__})")
    foreign = {(user, s) for user in clients for s in universe if user not in PUBLISHERS.get(s, ())}
    before = await _last_seqs(rig)
    for user, subject in sorted(foreign):
        await clients[user].nc.publish(subject, json.dumps({"probe": user}).encode())
    for client in clients.values():
        await client.settle()
    after = await _last_seqs(rig)
    findings += [
        f"stream {name} moved {seq} -> {after.get(name)} under publishes that must be refused" for name, seq in before.items() if after.get(name) != seq
    ]
    return findings, foreign, clients


async def _publish_until_delivered(sidecar: Sidecar, pubsub: str, topic: str, app: SubscriberApp, route: str, timeout: float = 30.0) -> str | None:
    """Publish through `sidecar` until `app` receives one on `route`: a `deliverPolicy: new` consumer drops what precedes it."""
    deadline = asyncio.get_running_loop().time() + timeout
    for attempt in range(1_000):
        await sidecar.publish(pubsub, topic, {"marker": f"{topic}#{attempt}"})
        await asyncio.sleep(0.5)
        if delivered := app.markers(route):
            return delivered[0]
        if asyncio.get_running_loop().time() > deadline:
            break
    return None


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
    """One real daprd per Component shape, each delivering a message end to end with the chart's own Component."""
    findings: list[str] = []
    creds = mint_.credentials
    subscriber_app = SubscriberApp(
        [
            {"pubsubname": "lineage-pubsub-notifications", "topic": "lineage.events.v1", "route": "/lineage-events", "deadLetterTopic": "dlq.notifications"},
            {"pubsubname": "lineage-pubsub-notifications", "topic": "dlq.notifications", "route": "/dlq-event"},
        ],
        failing=frozenset({"/lineage-events"}),
    )
    publisher_app, broadcast_app = (
        SubscriberApp([]),
        SubscriberApp([{"pubsubname": "catalog-control-pubsub", "topic": "catalog.control.v1", "route": "/control-events"}]),
    )
    sidecars = [
        Sidecar(
            tools.daprd,
            "notifications",
            [sidecar_component(_named(components, "lineage-pubsub-notifications"), broker.url, creds["notifications"])],
            subscriber_app,
            workdir / "subscriber",
        ),
        Sidecar(
            tools.daprd,
            "catalog",
            [sidecar_component(_named(components, "lineage-pubsub"), broker.url, creds["catalog"])],
            publisher_app,
            workdir / "publisher",
        ),
        Sidecar(
            tools.daprd,
            "catalog",
            [sidecar_component(_named(components, "catalog-control-pubsub"), broker.url, creds["catalog"])],
            broadcast_app,
            workdir / "broadcast",
        ),
    ]
    _, publisher, broadcast = sidecars
    try:
        for sidecar in sidecars:
            await sidecar.start()
    except (RuntimeError, TimeoutError) as exc:
        findings.append(f"daprd did not start: {exc}")
        for sidecar in sidecars:
            sidecar.stop()
            sidecar.app.stop()
        return findings
    try:
        marker = await _publish_until_delivered(publisher, "lineage-pubsub", "lineage.events.v1", subscriber_app, "/lineage-events")
        if marker is None:
            findings.append("daprd catalog (publisher-only) -> daprd notifications (durable): nothing reached /lineage-events")
        else:
            for _ in range(60):
                if marker in subscriber_app.markers("/dlq-event"):
                    break
                await asyncio.sleep(0.25)
            else:
                findings.append(f"daprd notifications never dead-lettered {marker} through dlq.notifications back to itself")
            findings += await _unacked(rig, ("LINEAGE", "DLQ"), component_metadata(_named(components, "lineage-pubsub-notifications")))
        if await _publish_until_delivered(broadcast, "catalog-control-pubsub", "catalog.control.v1", broadcast_app, "/control-events") is None:
            findings.append("daprd catalog (ephemeral broadcast): nothing reached /control-events")
    finally:
        for sidecar in sidecars:
            sidecar.stop()
            sidecar.app.stop()
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
            label = f"{app} {topic} ({durable or 'ephemeral'})"
            names = await client.request("$JS.API.STREAM.NAMES", {"subject": topic})
            if not names or len(names.get("streams") or []) != 1:
                findings.append(f"{label}: STREAM.NAMES answered {names}")
                continue
            stream = names["streams"][0]
            if durable and await client.request(f"$JS.API.CONSUMER.INFO.{stream}.{durable}") is None:
                findings.append(f"{label}: CONSUMER.INFO.{stream}.{durable} was not answered")
            create = f"$JS.API.CONSUMER.CREATE.{stream}.{durable}.{topic}" if durable else f"$JS.API.CONSUMER.CREATE.{stream}"
            created = await client.request(create, {"stream_name": stream, "config": dapr_consumer_config(metadata, topic, client.nc.new_inbox())})
            if created is None:
                findings.append(f"{label}: {create} was not answered")
                continue
            name = durable or str(created.get("name"))
            bound = await client.request(f"$JS.API.CONSUMER.INFO.{stream}.{name}")
            if not bound or "config" not in bound:
                findings.append(f"{label}: CONSUMER.INFO.{stream}.{name} answered {bound}")
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


class _Source:
    """The unit source a worker fetches from (`ingest.worker.Fetcher`): bytes per key, nothing for `empty` keys."""

    def __init__(self, empty: frozenset[str]) -> None:
        self._empty = empty

    async def fetch(self, key: str, *, source_endpoint: str | None = None) -> bytes:
        return b"" if key in self._empty else f"bytes-for-{key}".encode()


async def _drain_ingest_run(url: str, mint_: Mint, rig: Client, workdir: Path) -> list[str]:
    """Ingest's own client provisions, publishes, drains (parking one invalid unit) and releases a run as `ingest`."""
    credential = mint_.credentials["ingest"]
    run, uri = "nats-auth-run", str(workdir / "bronze.lance")
    create_empty(uri, BRONZE_SCHEMA)
    tasks = [UnitTask(run_id=run, chunk_id="c0", key=f"probe://unit/{i}", dataset_uri=uri) for i in range(3)]
    findings: list[str] = []
    queue = await WorkQueue.connect(url, name="ingest", error_cb=ignore_refusal, user_jwt_cb=credential.jwt_bytes, signature_cb=credential.sign_nonce)
    try:
        await queue.ensure_stream()
        await queue.ensure_dlq_stream()
        if (published := await queue.publish_units(tasks)) != len(tasks):
            findings.append(f"ingest published {published} of {len(tasks)} units")
        outcome = await Worker(queue, _Source(frozenset({tasks[1].key})), name="w1").drain_chunk(run, "c0", expected=len(tasks), dataset_uri=uri)
        if (outcome.units_done, outcome.errors) != (2, {tasks[1].key: "empty payload"}):
            findings.append(f"ingest drained {outcome.units_done} units with errors {outcome.errors}")
        pending = await (await queue.subscribe(run)).consumer_info()
        if (pending.num_pending, pending.num_ack_pending) != (0, 0):
            findings.append(f"ingest left {pending.num_pending} pending and {pending.num_ack_pending} unacked")
        await queue.release_run(run)
    except Exception as exc:  # noqa: BLE001 — a refused provisioning call surfaces as whatever nats-py raises
        findings.append(f"ingest failed to drain its run: {exc!r}")
    finally:
        await queue.close()
    left = await rig.request(f"$JS.API.CONSUMER.INFO.INGEST.ingest-{run}") or {}
    if (left.get("error") or {}).get("err_code") != 10014:
        findings.append(f"release_run left the run's consumer behind: {left}")
    return findings


async def _stream_job_repairs(tools: Tools, docs: tuple[dict, ...], broker: Broker, mint_: Mint, rig: Client, home: Path) -> list[str]:
    """The Job's second run, over an orphan durable and a drifted bound it must delete and converge as `admin`."""
    work = component_metadata(_subscriber_component(_pubsub_components(docs), "maintenance", "maintenance-work-durable")[0])
    orphan = {"durable_name": "orphan-durable", "ack_policy": "explicit", "deliver_policy": "new", "filter_subject": "lineage.events.v1"}
    await rig.request("$JS.API.CONSUMER.CREATE.LINEAGE.orphan-durable.lineage.events.v1", {"stream_name": "LINEAGE", "config": orphan})
    drifted = ((await rig.request("$JS.API.CONSUMER.INFO.MAINTENANCE_WORK.maintenance-work-durable")) or {}).get("config", {})
    drifted["max_ack_pending"] = int(work["maxAckPending"]) + 1000
    update = {"stream_name": "MAINTENANCE_WORK", "config": drifted, "action": "update"}
    await rig.request("$JS.API.CONSUMER.CREATE.MAINTENANCE_WORK.maintenance-work-durable.maintenance.work.v1", update)
    findings = await asyncio.to_thread(_stream_job, tools, docs, broker, mint_, home)
    gone = (await rig.request("$JS.API.CONSUMER.INFO.LINEAGE.orphan-durable") or {}).get("error") or {}
    converged = (await rig.request("$JS.API.CONSUMER.INFO.MAINTENANCE_WORK.maintenance-work-durable") or {}).get("config", {})
    if gone.get("err_code") != 10014:
        findings.append("the stream Job did not delete the orphan durable as admin")
    if converged.get("max_ack_pending") != int(work["maxAckPending"]):
        findings.append(f"the stream Job did not converge maintenance-work-durable to {work['maxAckPending']} as admin: {converged.get('max_ack_pending')}")
    return findings


def _monitor_reads(tools: Tools, broker: Broker, mint_: Mint, home: Path) -> list[str]:
    """nats-box's read-only operator view, as `monitor`."""
    findings = []
    for args in (["stream", "ls"], ["stream", "info", "LINEAGE"], ["consumer", "ls", "LINEAGE"], ["consumer", "info", "LINEAGE", "lineage-durable"]):
        done = run_cli(tools.box_cli, args, mint_.credentials["monitor"], broker.url, home)
        if done.returncode:
            findings.append(f"nats {' '.join(args)} as monitor exited {done.returncode}: {(done.stdout + done.stderr)[-400:]}")
    return findings


def _refusals_against_the_matrix(log_text: str, mint_: Mint, foreign: set[tuple[str, str]]) -> list[str]:
    """The broker must have refused every foreign publish, and nothing else: any other refusal is a missing allowance."""
    seen = [(mint_.user_of(v.user_key), v) for v in violations(log_text)]
    refused = {(user, v.subject) for user, v in seen if v.kind == "Publish"}
    accepted = [f"{user} published {subject}, which only {sorted(PUBLISHERS.get(subject, ())) or 'nobody'} may" for user, subject in sorted(foreign - refused)]
    unexpected = {
        f"the broker refused {user} {v.kind.lower()} on {v.subject} (connection {v.connection})" for user, v in seen if (user, v.subject) not in foreign
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
async def test_nats_admits_each_client_only_to_its_own_subjects(tmp_path: Path) -> None:
    tools = Tools.from_env(os.environ)
    values = _values()
    docs = render(*DEFAULT_ARGS, "-f", str(values), "--set", "nats.streamReplicas=1")
    table = {user: Permissions(publish=tuple(sorted(pub)), subscribe=tuple(sorted(sub))) for user, (pub, sub) in nats_users(docs).items()}
    assert table, f"the render with {values} issues no NATS user (the dev seed is absent)"
    components = _pubsub_components(docs)
    findings = [*_lane_runs_the_charts_images(tools, docs), *_table_names_every_client(table, components)]
    assert findings == [], "\n".join(findings)
    mint_ = mint(tools.nsc, {**table, RIG: Permissions(publish=("$JS.API.>",), subscribe=("_INBOX.>",))}, tmp_path / "nsc")
    broker = Broker(tools.nats_server, mint_, tmp_path / "broker")
    broker.start()
    try:
        findings += await asyncio.to_thread(_stream_job, tools, docs, broker, mint_, tmp_path)
        rig = await Client.connect(broker.url, mint_.credentials[RIG], RIG)
        findings += await _without_credential(broker.url)
        matrix, foreign, probes = await _publish_matrix(broker.url, mint_, table, rig)
        findings += matrix
        findings += await _daprd_shapes(tools, broker, mint_, components, rig, tmp_path / "daprd")
        findings += await _replay_subscriptions(broker.url, mint_, components, probes)
        findings += await _drain_ingest_run(broker.url, mint_, rig, tmp_path)
        findings += await _stream_job_repairs(tools, docs, broker, mint_, rig, tmp_path)
        findings += await asyncio.to_thread(_monitor_reads, tools, broker, mint_, tmp_path)
        for client in [rig, *probes.values()]:
            await client.close()
    finally:
        broker.stop()
    findings += _refusals_against_the_matrix(broker.log_text(), mint_, foreign)
    findings += _daprd_calls_the_replay_misses(broker.log_text(), mint_, components)
    assert findings == [], "\n".join(findings)
