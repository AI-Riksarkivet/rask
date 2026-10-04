"""A rendered Dapr Component may not reference a secret no configured store will resolve.

A Component's `secretKeyRef` is resolved by the store its `auth.secretStore` names. With none named,
Dapr falls back to Kubernetes Secrets — and this chart declares no `lance` Secret, so the load fails:

    Fatal error from runtime: failed to load components:
    rpc error: code = Unknown desc = Secret "lance" not found

THE BLAST RADIUS IS THE WHOLE RELEASE, which is what makes this worth a gate rather than a fix. A
component that fails to load kills the SIDECAR, so every app sharing it fails its health check for a
reason naming neither the component nor the flag behind it. Measured 2026-09-24 on `e2e-ray`, whose
overlay set `openbao.enabled=false`: `medallion-producer` and `bronze-to-silver` both died that way,
seven minutes into a deploy, and the visible symptom was `TimeoutError: Dapr health check timed out`.

IT WAS HIDDEN BEHIND AN EARLIER BUG. `auth:` used to render as YAML null, so the CRD refused the
Component outright and it never loaded — nobody reached the point of discovering the secret was not
there either. Fixing the null exposed this one underneath, which is the ordinary shape of a lane that
nothing has run: the failures come out in sequence, not all at once.

SCOPED TO THE OVERLAYS WE DEPLOY, and that bound is deliberate. `openbao.enabled=false` is a
legitimate render — the estate's own suites use it to exercise the non-Dapr secret paths, and an
operator may supply a `lance` Secret out of band, which is a reference and not a carried secret. So
the chart does not refuse the shape; this gate asserts it of the estates whose values we own, read
out of the stack scripts themselves. A first cut of this failed the RENDER instead and broke nine
tests that disable OpenBao on purpose: the combination is unsupported in OUR stacks, not unsound.

THE BUS CREDENTIAL IS THE SAME KIND OF REFERENCE ([[XC-078]], values.yaml `nats.auth`). Dapr 1.18.1's pubsub.jetstream
authenticates only with a user JWT and the NKEY seed that signs the server's nonce, and with either one empty Init falls
through to an unauthenticated connect, silently. So wherever the store is on, every pub/sub Component names its one app's
`nats-user-<app>` from that store, the store is scoped to that app (a store a sidecar has not loaded leaves the value empty
too), and the user the dev seed issues may do what the render configures that app to do: consume through each of its
components' durables, and publish or consume every topic its pods are given. A pod that is not a sidecar (the stream Job,
nats-box, the NATS servers) mounts its credential as a file, so that file must be one an ExternalSecret writes from the store.
"""

from __future__ import annotations

import json
import pathlib
import posixpath
import subprocess
from collections import defaultdict

import pytest

from tests.unit import chart_render
from tests.unit.chart_render import DEFAULT_ARGS, OIDC_ARGS, render
from tests.unit.test_no_chart_owned_manifest_renders_a_null import _overlays


REPO = pathlib.Path(__file__).resolve().parents[2]
STORE = "lance-secrets"
#: Names ANOTHER stage's topic as DAG configuration: a stage runner reads it as a gate input and never publishes it, because
#: only the catalog's publication advances a tier. Granting it would let a stage runner wake the next tier past that door.
_NOT_A_TOPIC_THE_APP_USES = frozenset({"MEDALLION_PUB_TOPIC"})


def _secret_refs(node: object, found: list[dict]) -> list[dict]:
    if isinstance(node, dict):
        if isinstance(node.get("secretKeyRef"), dict):
            found.append(node["secretKeyRef"])
        for value in node.values():
            _secret_refs(value, found)
    elif isinstance(node, list):
        for value in node:
            _secret_refs(value, found)
    return found


def _consumes(publish: frozenset[str], stream: str, durables: set[str]) -> bool:
    """Whether a user may create, read and acknowledge a consumer on `stream`: one of its durables, or a server-named one."""
    durable = any({f"$JS.API.CONSUMER.INFO.{stream}.{d}", f"$JS.API.CONSUMER.CREATE.{stream}.{d}.>", f"$JS.ACK.{stream}.{d}.>"} <= publish for d in durables)
    ephemeral = {f"$JS.API.CONSUMER.CREATE.{stream}", f"$JS.API.CONSUMER.INFO.{stream}.*", f"$JS.ACK.{stream}.>"} <= publish
    return durable or ephemeral


def _bus_credential_problems(docs: tuple[dict, ...], components: list[dict]) -> list[str]:
    """Every pub/sub Component names its one app's user from the store, and that user may do what the render configures."""
    users = chart_render.nats_users(docs)
    if not users:
        return ["the store is on and the dev seed issues no NATS user, so no pub/sub credential can resolve"]
    streams = chart_render.jetstream_streams(docs)
    store_scopes = next(doc.get("scopes") or [] for doc in components if doc["metadata"]["name"] == STORE)
    problems: list[str] = []
    held: dict[str, set[str]] = defaultdict(set)
    durables: dict[str, set[str]] = defaultdict(set)
    for doc in components:
        if doc["spec"]["type"] != "pubsub.jetstream":
            continue
        name, scopes = doc["metadata"]["name"], doc.get("scopes") or []
        rows = {row["name"]: row for row in doc["spec"].get("metadata") or []}
        if "token" in rows:
            problems.append(f"{name} carries a shared token, which authenticates no app as itself")
        if len(scopes) != 1:
            problems.append(f"{name} is scoped to {scopes}: a component's credential is one app's user")
            continue
        app = scopes[0]
        held[app].add(name)
        for field, key in (("jwt", "jwt"), ("seedKey", "seed")):
            if (rows.get(field) or {}).get("secretKeyRef") != {"name": f"nats-user-{app}", "key": key}:
                problems.append(f"{name} does not take its {field} from nats-user-{app}/{key}: {rows.get(field)}")
        if (doc.get("auth") or {}).get("secretStore") != STORE or app not in store_scopes:
            problems.append(f"{name} names nats-user-{app} from a store {app}'s sidecar does not load")
        if app not in users:
            problems.append(f"{name} names nats-user-{app}, a user the seed does not issue")
            continue
        publish, subscribe = users[app]
        if "_INBOX.>" not in subscribe:
            problems.append(f"{app} may not receive a reply, a delivery or a publish acknowledgement")
        if durable := (rows.get("durableName") or {}).get("value"):
            durables[app].add(str(durable))
            if "$JS.API.STREAM.NAMES" not in publish or not any(_consumes(publish, stream, {str(durable)}) for stream in streams):
                problems.append(f"{app} may not create, bind and acknowledge its durable {durable} on any stream")
        elif "deliverPolicy" in rows and not any(_consumes(publish, stream, set()) for stream in streams):
            problems.append(f"{app} may not create, bind and acknowledge the ephemeral consumer {name} subscribes with")
    for workload, _, container in chart_render.containers(docs):
        app = next(
            (
                (doc["spec"]["template"].get("metadata") or {}).get("annotations", {}).get("dapr.io/app-id")
                for doc in docs
                if f"{doc.get('kind')}/{doc['metadata']['name']}" == workload
            ),
            None,
        )
        if app not in held:
            continue
        env = chart_render.env_of(container)
        for key, value in env.items():
            if key.endswith("_PUBSUB") and value not in held[app]:
                problems.append(f"{workload} {key}={value}, a component not scoped to {app}")
        topics = {value for key, value in env.items() if "TOPIC" in key and value and key not in _NOT_A_TOPIC_THE_APP_USES}
        topics |= set(json.loads(env.get("MEDALLION_TRANSFORM_ROUTES", "{}")).values())
        publish = users[app][0]
        for topic in sorted(topics):
            stream = next((s for s, subject in streams.items() if chart_render.captures(topic, subject)), None)
            if topic not in publish and not (stream and _consumes(publish, stream, durables[app])):
                problems.append(f"{workload} is configured with {topic}, which nats-user-{app} may neither publish nor consume")
    return problems


@pytest.mark.parametrize("label,overlay", _overlays(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_referenced_secret_has_a_store_to_resolve_it(label: str, overlay: tuple[str, ...]) -> None:
    try:
        docs = render(*overlay, *OIDC_ARGS)
    except subprocess.CalledProcessError as exc:
        # SURFACE HELM'S OWN WORDS. The chart REFUSES this combination at render, so an overlay that
        # reintroduces it fails here — and a bare CalledProcessError shows a 400-character argv and
        # none of the message written to explain the fix. Measured while mutation-checking this gate.
        pytest.fail(f"the {label} overlay no longer renders:\n{(exc.stderr or exc.stdout or '').strip()[:800]}")
    components = [doc for doc in docs if doc.get("kind") == "Component"]
    assert components, f"{label} rendered no Dapr Component — the gate lost its subject"
    offenders = []
    for doc in components:
        refs = _secret_refs(doc.get("spec") or {}, [])
        store = (doc.get("auth") or {}).get("secretStore")
        if refs and not store:
            names = sorted({str(ref.get("name")) for ref in refs})
            offenders.append(f"{doc['metadata']['name']} references {names} with no auth.secretStore")
    assert not offenders, (
        f"under the {label} overlay these Dapr Components name a secret nothing will resolve, so daprd "
        f"exits fatal on load and takes every app's sidecar with it:\n  " + "\n  ".join(offenders)
    )
    assert any(doc["metadata"]["name"] == STORE for doc in components), f"{label} deploys no secret store, so no bus credential can resolve"
    problems = _bus_credential_problems(docs, components)
    assert not problems, f"under the {label} overlay a pub/sub client would connect as nobody, or as a user that may not do its job:\n  " + "\n  ".join(
        problems
    )


def _fields(external_secret: dict) -> set[str]:
    """The fields an ExternalSecret fetches, each of which its template reads as `{{ .<secretKey> }}`."""
    return {item["secretKey"] for item in external_secret["spec"].get("data") or []}


@pytest.mark.parametrize(
    ("overlay", "server"),
    [
        pytest.param(("--set", "externalSecrets.enabled=true"), False, id="credential-files-while-the-server-takes-anyone"),
        pytest.param(("--set", "externalSecrets.enabled=true", "--set", "nats.auth.server=true"), True, id="the-server-in-operator-mode"),
        # Operator mode refuses every client without a user JWT, and its own operator and route credentials arrive through ESO.
        pytest.param(("--set", "nats.auth.server=true"), None, id="no-operator-mode-without-external-secrets"),
    ],
)
def test_every_nats_credential_a_pod_mounts_is_a_file_an_external_secret_writes_from_the_store(overlay: tuple[str, ...], server: bool | None) -> None:  # noqa: FBT001
    if server is None:
        with pytest.raises(subprocess.CalledProcessError) as refused:
            render(*DEFAULT_ARGS, *overlay)
        assert "externalSecrets.enabled" in refused.value.stderr, "the refusal does not name the switch that delivers the server's credentials"
        return
    docs = render(*DEFAULT_ARGS, *overlay)
    users = chart_render.nats_users(docs)
    written = {doc["spec"]["target"]["name"]: doc for doc in docs if doc.get("kind") == "ExternalSecret"}
    # A Secret the render creates holds only what values carry, and values never carry a credential; any other must be written.
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

    expected = {"Job/rask-nats-stream-r1/nats"} | (
        {"Deployment/rask-nats-box/nats-box", "StatefulSet/rask-nats/nats", "StatefulSet/rask-nats/reloader"} if server else set()
    )
    assert set(mounts) == expected, f"the pods mounting a NATS credential are {sorted(mounts)}, not {sorted(expected)}"
    for pod, paths in mounts.items():
        for secret in paths.values():
            assert secret in written, f"{pod} mounts {secret}, which no ExternalSecret writes, so the pod never starts"
            fetched = {(item["remoteRef"]["key"], item["remoteRef"]["property"]) for item in written[secret]["spec"]["data"]}
            assert {key for key, _ in fetched} <= {f"nats-user-{user}" for user in users} | {"nats-server", "nats-route"}, (
                f"{secret} is written from {sorted(fetched)}, a secret neither the seed nor an attesting operator provides"
            )
            template = json.dumps(written[secret]["spec"]["target"]["template"]["data"])
            assert all(f"{{{{ .{field} }}}}" in template for field in _fields(written[secret])), f"{secret}'s template drops a field it fetches"

    [job] = [c for workload, _, c in chart_render.containers(docs) if workload == "Job/rask-nats-stream-r1"]
    [(path, secret)] = mounts["Job/rask-nats-stream-r1/nats"].items()
    assert {(item["remoteRef"]["key"], item["remoteRef"]["property"]) for item in written[secret]["spec"]["data"]} == {
        ("nats-user-admin", "jwt"),
        ("nats-user-admin", "seed"),
    }, "the stream Job's credential is not the admin user's"
    assert chart_render.env_of(job).get("NATS_CREDS") in {f"{path}/{key}" for key in written[secret]["spec"]["target"]["template"]["data"]}, (
        "the stream Job's nats CLI is not pointed at the credential file it mounts"
    )

    [conf] = [doc["data"]["nats.conf"] for doc in docs if doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == "rask-nats-config"]
    includes = [line.strip().removeprefix("include ").removesuffix(";") for line in conf.splitlines() if line.strip().startswith("include ")]
    if not server:
        assert not includes and not any(word in conf for word in ('"operator"', '"system_account"', '"resolver', '"authorization"')), (
            f"the server authenticates before its switch is on:\n{conf}"
        )
        assert '"routes"' in conf, "the cluster lost its routes with the server switch off"
        return
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
