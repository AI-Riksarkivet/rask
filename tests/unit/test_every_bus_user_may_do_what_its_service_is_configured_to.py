"""Every bus user may do what its service is configured to, on its own connection, and no more ([[XC-078]], [[XC-199]]).

The bus admits a client by the user it connects as (values.yaml `nats.auth`), and a missing allowance fails closed and
quietly: a refused pull is asked again for as long as the pod runs, a publish with no inbox times out and stays staged,
and the pod stays Ready throughout. So the table the dev seed issues is held here to what each render configures.

A SERVICE'S OWN CONNECTION ([[XC-199]], `lakehouse.bus`). Each service connects as the bus user its pod is named
(`RASK_BUS_USER`), with the credential its own store login reads, so that login's policy must read `nats-user-<user>`;
its acknowledgements arrive under `_INBOX_<user>`, which the table must grant to it and to no other user; and it
believes a duplicate acknowledgement only after reading the stored message back, so it holds the read of exactly the
streams that take what it publishes.

ITS LANE. A service takes its deliveries from a durable consumer the stream Job declares. The user that pulls one holds
its pull and its settling (`$JS.API.CONSUMER.MSG.NEXT.<stream>.<consumer>`, `$JS.ACK.<stream>.<consumer>.>`), no other
user holds either, and no user holds them for a consumer the render does not declare. The catalog follows its control
topic by reading its stream by sequence, so where it does its user holds that stream's read, and no user but the stream
Job's may create a consumer anywhere or answer one's flow control.

Besides the deployed overlays this renders the ones whose publishers and lanes differ from theirs. A pod that reads no
store (the stream Job; nats-box and the servers of the NATS fixture, a prerequisite rask never installs) mounts its
credential as a file, so that file must be one an ExternalSecret writes from the store. What the bus itself admits and
refuses under the table is the `nats-auth` lane's.
"""

from __future__ import annotations

import json
import pathlib
import posixpath
import re
import subprocess
from collections import defaultdict

import pytest

from catalog.api import control_lane
from lakehouse.bus import BusSettings
from lakehouse.control_events import CONTROL_TOPIC
from lineage.api import ingest_lane
from maintenance.services import queue
from tests.unit import chart_render
from tests.unit.chart_render import DEFAULT_ARGS, OIDC_ARGS, render
from tests.unit.test_no_chart_owned_manifest_renders_a_null import _overlays


REPO = pathlib.Path(__file__).resolve().parents[2]
_POLICY_PATH = re.compile(r'path "[^"/]+/data/([^"]+)"')
#: The JetStream API call that reads one stored message of a stream, before the stream's name.
_READ_BACK = "$JS.API.STREAM.MSG.GET."
#: The call a user pulls a durable consumer with, and the subject it settles each delivery on, before `<stream>.<consumer>`.
_PULL = "$JS.API.CONSUMER.MSG.NEXT."
_SETTLE = "$JS.ACK."
#: The call that creates a consumer, before the stream's name, and the subject a consumer's flow control is answered on.
_CREATE = "$JS.API.CONSUMER.CREATE."
_FLOW = "$JS.FC."
#: The stream Job's own user, the one that declares every durable consumer.
_ADMIN = "admin"
#: The consumers maintenance's worker pulls on the work queue's connection.
_WORKERS = frozenset(
    {
        (queue.TASKS_STREAM, queue.TASK_CONSUMER),
        (queue.SCHEDULES_STREAM, queue.DUE_CONSUMER),
        (queue.STORE_EVENTS_STREAM, queue.STORE_EVENTS_CONSUMER),
        (queue.ARRIVALS_STREAM, queue.ARRIVALS_CONSUMER),
    }
)
#: The renders whose publishers and lanes differ from the deployed overlays': the control topic's optional publisher,
#: maintenance with no write-event lane (its worker pulls no announcement), and each service with one of its publishes
#: switched off.
_OTHER_PUBLISHERS = [
    ("every-optional-publisher", (*DEFAULT_ARGS, "--set", "maintenance.controlEmit=true")),
    ("no-write-event-lane", (*DEFAULT_ARGS, "--set", "maintenance.arrivalEnabled=false")),
    ("no-control-lane", (*DEFAULT_ARGS, "--set", "catalog.controlEmit=false")),
    ("no-lineage-outbox", (*DEFAULT_ARGS, "--set", "services.lineage.outbox.enabled=false")),
    ("maintenance-emits-nothing", (*DEFAULT_ARGS, "--set", "maintenance.lineageEmit=false")),
]

type Users = dict[str, tuple[frozenset[str], frozenset[str]]]


def _configured_to_publish(env: dict[str, str]) -> set[str]:
    """The topics a pod's environment has its service publish on a connection of its own: each service's own switch,
    and the topic variable or the control lane's one topic beside it."""
    topics: set[str] = set()
    if env.get("LANCE_LINEAGE_EMIT_ENABLED") == "true" and env.get("LANCE_LINEAGE_TRANSPORT") == "bus":
        topics.add(env["LANCE_LINEAGE_TOPIC"])
    if env.get("LINEAGE_OUTBOX_URI"):
        topics.add(env["LINEAGE_EVENTS_TOPIC"])
    if env.get("MAINTENANCE_LINEAGE_EMIT_ENABLED") == "true":
        topics.add(env["MAINTENANCE_LINEAGE_TOPIC"])
    if "true" in (env.get("LANCE_CONTROL_EMIT_ENABLED"), env.get("MAINTENANCE_CONTROL_EMIT_ENABLED")):
        topics.add(CONTROL_TOPIC)
    return topics


def _own_connection_problems(docs: tuple[dict, ...], users: Users, streams: dict[str, str]) -> list[str]:
    """Every service the render names a bus user connects with settings its process accepts, as a user the seed issues,
    with a credential its own store login reads, under an inbox no other user reads; and it may publish exactly the
    topics its pods are configured to, and read back exactly the streams that take them."""
    envs = [(workload, chart_render.env_of(container)) for workload, _, container in chart_render.containers(docs)]
    problems: list[str] = []
    configured: dict[str, set[str]] = defaultdict(set)
    for workload, env in envs:
        user = env.get("RASK_BUS_USER")
        if user is None:
            continue
        configured[user] |= _configured_to_publish(env)
        try:
            BusSettings.model_validate({"RASK_BUS_URL": env.get("RASK_BUS_URL", ""), "RASK_BUS_USER": user})
        except ValueError as refused:
            problems.append(f"{workload} is named a bus its process refuses to connect to: {refused}")
        if user not in users:
            problems.append(f"{workload} connects to the bus as {user}, a user the seed does not issue")
            continue
        if f"nats-user-{user}" not in _granted(docs, env.get("RASK_SECRET_STORE_ROLE", "")):
            problems.append(f"{workload} connects to the bus as {user}, whose credential its own store login may not read")
    for user, topics in sorted(configured.items()):
        if user not in users:
            continue
        publish, subscribe = users[user]
        if f"_INBOX_{user}.>" not in subscribe:
            problems.append(f"{user} may not receive the acknowledgement of a publish it makes on its own connection")
        overheard = sorted(
            other for other, (_, theirs) in users.items() if other != user and any(chart_render.captures(f"_INBOX_{user}.reply", s) for s in theirs)
        )
        if overheard:
            problems.append(f"{overheard} may read the replies to {user}'s own connection")
        stored = {topic for topic in publish if any(chart_render.captures(topic, subject) for subject in streams.values())}
        if stored != topics:
            problems.append(f"{user} may publish {sorted(stored)} on its own connection, and its pods are configured to publish {sorted(topics)}")
        taking = {stream for topic in topics for stream, subject in streams.items() if chart_render.captures(topic, subject)}
        read_back = {grant.removeprefix(_READ_BACK) for grant in publish if grant.startswith(_READ_BACK)}
        if read_back != taking:
            problems.append(f"{user} publishes into {sorted(taking)} on its own connection and may read a stored message back from {sorted(read_back)}")
    for user, (publish, _) in sorted(users.items()):
        if user not in configured and any(grant.startswith(_READ_BACK) for grant in publish):
            problems.append(f"{user} may read stored messages, and no service connects to the bus as it")
    return problems


def _named(docs: tuple[dict, ...], container_name: str) -> dict[str, str]:
    """The environment of the one container called ``container_name``; empty when the render holds none."""
    found = [chart_render.env_of(container) for _, name, container in chart_render.containers(docs) if name == container_name]
    return found[0] if len(found) == 1 else {}


def _lane_problems(docs: tuple[dict, ...], users: Users) -> list[str]:
    """Each consumer the stream Job declares is pulled and settled by its service's user and by no other, no user holds
    a pull on a consumer the render does not declare, and only the catalog, where it follows its control topic, may
    create a consumer beside the stream Job."""
    problems: list[str] = []
    declared = chart_render.bus_consumers(docs)
    lineage = _named(docs, "lineage").get("RASK_BUS_USER")
    for stream, name in sorted(declared):
        puller = lineage if (stream, name) == (ingest_lane.STREAM, ingest_lane.CONSUMER) else queue.BUS_USER if (stream, name) in _WORKERS else None
        if puller is None:
            problems.append(f"the stream Job declares {stream}/{name}, which no service is configured to pull")
            continue
        needed = {f"{_PULL}{stream}.{name}", f"{_SETTLE}{stream}.{name}.>"}
        holding = {user: sorted(needed & publish) for user, (publish, _) in users.items() if needed & publish}
        if holding != {puller: sorted(needed)}:
            problems.append(f"{stream}/{name} is pulled by {puller}, and its pull and settling are held by {holding}")
    for user, (publish, _) in sorted(users.items()):
        for grant in sorted(publish):
            for call in (_PULL, _SETTLE):
                if grant.startswith(call) and (stream_and_name := tuple(grant.removeprefix(call).removesuffix(".>").split(".", 1))) not in declared:
                    problems.append(f"{user} holds {grant}, for a consumer {stream_and_name} the render does not declare")
    catalog = _named(docs, "catalog")
    follower = catalog.get("RASK_BUS_USER") if catalog.get("LANCE_CONTROL_EMIT_ENABLED") == "true" else None
    if follower is not None and f"{_READ_BACK}{control_lane.STREAM}" not in users.get(follower, (frozenset(), frozenset()))[0]:
        problems.append(f"{follower} follows {control_lane.TOPIC} and may not read {control_lane.STREAM} by sequence")
    creating = {user: sorted(g for g in publish if g.startswith((_CREATE, _FLOW))) for user, (publish, _) in users.items() if user != _ADMIN}
    if creating := {user: grants for user, grants in creating.items() if grants}:
        problems.append(f"beside the stream Job's user, these may create a consumer or answer its flow control: {creating}")
    return problems


@pytest.mark.parametrize(("label", "overlay"), [*_overlays(), *_OTHER_PUBLISHERS], ids=lambda v: v if isinstance(v, str) else "")
def test_every_bus_user_may_do_what_its_service_is_configured_to_and_no_more(label: str, overlay: tuple[str, ...]) -> None:
    try:
        docs = render(*overlay, *OIDC_ARGS)
    except subprocess.CalledProcessError as exc:
        # SURFACE HELM'S OWN WORDS. The chart REFUSES this combination at render, so an overlay that
        # reintroduces it fails here — and a bare CalledProcessError shows a 400-character argv and
        # none of the message written to explain the fix. Measured while mutation-checking this gate.
        pytest.fail(f"the {label} overlay no longer renders:\n{(exc.stderr or exc.stdout or '').strip()[:800]}")
    users = chart_render.nats_users(docs)
    assert users, f"under the {label} overlay the dev seed issues no bus user, so no service's credential can resolve"

    problems = _own_connection_problems(docs, users, chart_render.jetstream_streams(docs)) + _lane_problems(docs, users)

    assert not problems, f"under the {label} overlay a service would connect as nobody, or as a user that may not do its job:\n  " + "\n  ".join(problems)


def _fields(external_secret: dict) -> set[str]:
    """The fields an ExternalSecret fetches, each of which its template reads as `{{ .<secretKey> }}`."""
    return {item["secretKey"] for item in external_secret["spec"].get("data") or []}


def _granted(docs: tuple[dict, ...], policy: str) -> set[str]:
    """The store keys a policy rask renders lets its holder read: its `policy-<name>.json` in `<release>-secret-store-identities`."""
    document = json.loads(chart_render.store_identities(docs).get(f"policy-{policy}.json", '{"path": {}}'))
    return {match.group(1) for path in document["path"] if (match := _POLICY_PATH.match(f'path "{path}"'))}


def _eso_reads(docs: tuple[dict, ...]) -> set[str]:
    """The store keys the dev OpenBao's policy lets the External Secrets Operator read."""
    [role] = [doc for doc in docs if doc.get("kind") == "SecretStore"]
    return _granted(docs, role["spec"]["provider"]["vault"]["auth"]["kubernetes"]["role"])


def _credential_mounts(docs: tuple[dict, ...]) -> dict[str, dict[str, str]]:
    """Each container mounting a NATS credential Secret the render does not create -> {mount path: Secret}."""
    rendered = {doc["metadata"]["name"] for doc in docs if doc.get("kind") == "Secret"}
    mounts: dict[str, dict[str, str]] = defaultdict(dict)
    for doc in docs:
        if doc.get("kind") not in {"Job", "Deployment", "StatefulSet"}:
            continue
        pod = doc["spec"]["template"]["spec"]
        secrets = {v["name"]: v["secret"]["secretName"] for v in pod.get("volumes") or [] if "secret" in v and v["secret"]["secretName"] not in rendered}
        for container in pod["containers"]:
            for mount in container.get("volumeMounts") or []:
                if mount["name"] in secrets and "nats" in secrets[mount["name"]]:
                    mounts[f"{doc['kind']}/{doc['metadata']['name']}/{container['name']}"][mount["mountPath"]] = secrets[mount["name"]]
    return mounts


@pytest.mark.parametrize(
    ("release", "expected"),
    [
        pytest.param("rask", {"Job/rask-nats-stream-r1/nats", "Job/rask-nats-stream-retire-r1/nats"}, id="rask-the-stream-job"),
        pytest.param(
            "bus",
            {"Deployment/rask-nats-box/nats-box", "StatefulSet/rask-nats/nats", "StatefulSet/rask-nats/reloader"},
            id="the-fixture-bus-in-operator-mode",
        ),
    ],
)
def test_every_nats_credential_a_pod_mounts_is_a_file_an_external_secret_writes_from_the_store(release: str, expected: set[str]) -> None:
    """Both phases of rask's stream Job mount the admin user's creds file; the NATS fixture (deploy/fixtures/nats) in operator
    mode mounts its server config and nats-box's `monitor` creds, written by its own ExternalSecrets from rask's dev store
    ([[XC-129]]). Each file is written from keys the dev seed provides and the store's policy lets ESO read, so a pod that
    mounts it starts."""
    rask = render(*DEFAULT_ARGS)
    users = chart_render.nats_users(rask)
    seeded = {f"nats-user-{user}" for user in users} | {"nats-server", "nats-route"}
    readable = _eso_reads(rask)
    docs = rask if release == "rask" else chart_render.render_fixture("nats", "--set", "nats.auth.server=true")
    written = {doc["spec"]["target"]["name"]: doc for doc in docs if doc.get("kind") == "ExternalSecret"}
    mounts = _credential_mounts(docs)

    assert set(mounts) == expected, f"the pods mounting a NATS credential are {sorted(mounts)}, not {sorted(expected)}"
    for pod, paths in mounts.items():
        for secret in paths.values():
            assert secret in written, f"{pod} mounts {secret}, which no ExternalSecret writes, so the pod never starts"
            fetched = {(item["remoteRef"]["key"], item["remoteRef"]["property"]) for item in written[secret]["spec"]["data"]}
            assert {key for key, _ in fetched} <= seeded, (
                f"{secret} is written from {sorted(fetched)}, a secret neither the seed nor an attesting operator provides"
            )
            assert {key for key, _ in fetched} <= readable, f"{secret} is written from {sorted({k for k, _ in fetched} - readable)}, which ESO may not read"
            template = json.dumps(written[secret]["spec"]["target"]["template"]["data"])
            assert all(f"{{{{ .{field} }}}}" in template for field in _fields(written[secret])), f"{secret}'s template drops a field it fetches"

    if release == "rask":
        for phase in sorted(expected):
            workload = phase.rsplit("/", 1)[0]
            [job] = [c for found, _, c in chart_render.containers(docs) if found == workload]
            [(path, secret)] = mounts[phase].items()
            assert {(item["remoteRef"]["key"], item["remoteRef"]["property"]) for item in written[secret]["spec"]["data"]} == {
                ("nats-user-admin", "jwt"),
                ("nats-user-admin", "seed"),
            }, f"{workload}'s credential is not the admin user's"
            assert chart_render.env_of(job).get("NATS_CREDS") in {f"{path}/{key}" for key in written[secret]["spec"]["target"]["template"]["data"]}, (
                f"{workload}'s nats CLI is not pointed at the credential file it mounts"
            )
        return

    [conf] = [doc["data"]["nats.conf"] for doc in docs if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "rask-nats-config"]
    includes = [line.strip().removeprefix("include ").removesuffix(";") for line in conf.splitlines() if line.strip().startswith("include ")]
    assert len(includes) == 2 and '"routes"' not in conf, f"operator mode is not two included files, with the routes moved into one:\n{conf}"
    [box] = [
        json.loads(doc["stringData"]["default.json"]) for doc in docs if doc.get("kind") == "Secret" and doc["metadata"]["name"] == "rask-nats-box-contexts"
    ]
    assert box.get("creds", "").startswith(next(iter(mounts["Deployment/rask-nats-box/nats-box"]))), (
        f"nats-box's context does not use the credential it mounts: {box}"
    )
    [reloader] = [c for workload, name, c in chart_render.containers(docs) if workload == "StatefulSet/rask-nats" and name == "reloader"]
    for include in includes:
        target = posixpath.normpath(posixpath.join("/etc/nats-config", include))
        directory, file = posixpath.split(target)
        secret = mounts["StatefulSet/rask-nats/nats"].get(directory)
        assert secret and file in written[secret]["spec"]["target"]["template"]["data"], f"nats.conf includes {target}, a file no mounted ExternalSecret writes"
        assert target in reloader["args"], f"the config reloader does not watch {target}, so a rotated credential never reaches the server"


@pytest.mark.parametrize(
    ("row", "refusal"),
    [
        pytest.param(
            None, "the service maint connects to the bus as the user maint, which nats.auth.users does not declare", id="a-user-the-table-does-not-declare"
        ),
        pytest.param(
            '{"publish":["lineage.events.v1"],"subscribe":["_INBOX.>"]}',
            "nats.auth.users.maint grants the service maint no inbox of its own (_INBOX_maint.>)",
            id="a-user-with-only-the-shared-inbox",
        ),
    ],
)
def test_a_service_whose_bus_user_cannot_take_its_own_connection_fails_the_render(row: str | None, refusal: str) -> None:
    """A service connects to the bus as the user its app id names. A user the table does not declare has no credential in
    the store, and one granted only the shared inbox is handed no acknowledgement, so each publish would time out and stay
    staged with the pod Ready: the render refuses both."""
    declared = ("--set-json", f"nats.auth.users.maint={row}") if row else ()
    with pytest.raises(subprocess.CalledProcessError) as refused:
        render(*DEFAULT_ARGS, "--set", "maintenance.appId=maint", *declared)
    assert refusal in refused.value.stderr
