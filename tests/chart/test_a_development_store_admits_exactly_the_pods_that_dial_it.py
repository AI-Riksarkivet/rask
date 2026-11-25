"""A development store admits exactly the pods that dial it, and the bus's servers reach each other.

[[XC-202]]. On a development estate the object store (deploy/fixtures/rustfs), the bus (deploy/fixtures/nats) and the
secret store (deploy/fixtures/openbao) run in the release's namespace, beside the release and the other fixtures installed
there, and under `networkPolicy.enabled` the chart's ingress policies are their only door: the intra-namespace allow
leaves all three out. A pod the policy leaves out
cannot reach its store (the store fixture's bucket-events Job could not configure a bucket's change events, and the
store's server could not publish them on the bus), and a pod it admits that never dials the store is a path that serves
only an intruder. The bus fixture runs three clustered servers, which reach each other on the cluster port or the
JetStream cluster partitions.

READ FROM THE RENDER of the deployed overlay with the isolation layer on, beside the render of each fixture that shares
its namespace: a pod dials a store when its spec, or a ConfigMap or Secret it mounts, names the store's Service. A
`helm test` pod runs only when someone runs `helm test`.
"""

import json
import re
from typing import Final

import pytest
from pydantic import BaseModel, ConfigDict

from tests.chart import chart_render


#: The deployed overlay names the fixture's bus; `nats.url` is set again because a render's prerequisites `--set` another
#: address, which outranks a values file.
_DEPLOYED: tuple[str, ...] = (
    "--set", "image.localImages=true", "-f", str(chart_render.REPO / "deploy/values-local.yaml"),
    "--set", "networkPolicy.enabled=true", "--set-string", "nats.url=nats://rask-nats:4222",
)  # fmt: skip

#: The fixtures a development estate installs into the release's namespace, each with the values that run all its pods
#: (the bus in operator mode runs its stand-in signer): their pods are governed by the chart's policies as the release's
#: own are.
_NAMESPACE_FIXTURES: Final[frozendict[str, tuple[str, ...]]] = frozendict(
    {"dex": (), "marquez": (), "nats": ("--set", "nats.auth.server=true"), "openbao": (), "rustfs": ()}
)

#: The cluster-routes port of the bus fixture's servers (the upstream nats chart's default).
_CLUSTER_PORT = 6222


class _Store(BaseModel):
    """A development store: the component its servers carry, its client port and the Service its clients dial."""

    model_config = ConfigDict(frozen=True)

    component: str
    port: int
    service: str


_STORES: dict[str, _Store] = {
    "object store": _Store(component="rustfs", port=9000, service="rask-rustfs"),
    "bus": _Store(component="nats", port=4222, service="rask-nats"),
    "secret store": _Store(component="openbao", port=8200, service="rask-openbao"),
}


class _Pod(BaseModel):
    """One pod template the namespace runs: its workload, its labels, and its spec with what it mounts."""

    model_config = ConfigDict(frozen=True)

    workload: str
    labels: dict[str, str]
    spec: str


def _namespace() -> tuple[dict, ...]:
    """Every document the deployed release and the fixtures sharing its namespace render."""
    release = chart_render.render(*_DEPLOYED)
    return release + tuple(doc for fixture, values in _NAMESPACE_FIXTURES.items() for doc in chart_render.render_fixture(fixture, *values))


def _pods(docs: tuple[dict, ...]) -> list[_Pod]:
    """Every pod template the documents run, its spec read with the ConfigMaps and Secrets it mounts."""
    mountable = {(doc["kind"], doc["metadata"]["name"]): json.dumps(doc) for doc in docs if doc.get("kind") in {"ConfigMap", "Secret"}}
    pods: list[_Pod] = []
    for doc in docs:
        template = chart_render.pod_template(doc)
        if template is None or "test" in ((doc.get("metadata") or {}).get("annotations") or {}).get("helm.sh/hook", ""):
            continue
        volumes = template["spec"].get("volumes") or []
        mounted = [mountable.get(("ConfigMap", (v.get("configMap") or {}).get("name")), "") for v in volumes]
        mounted += [mountable.get(("Secret", (v.get("secret") or {}).get("secretName")), "") for v in volumes]
        pods.append(
            _Pod(
                workload=f"{doc['kind']}/{doc['metadata']['name']}",
                labels=template["metadata"].get("labels") or {},
                spec=json.dumps(template["spec"]) + "".join(mounted),
            )
        )
    return pods


def _names(pod: _Pod, service: str) -> bool:
    """Whether the pod names `service` as an address, alone or with a scheme or a port, and not as the start of another
    name (`rask-nats-box`)."""
    return re.search(rf"(?<![\w-]){re.escape(service)}(?![\w-])", pod.spec) is not None


def _peers(policies: list[dict], server: dict[str, str], port: int) -> list[dict]:
    """The pod selectors the ingress rules of the policies selecting the server admit on `port`, from its own namespace."""
    peers: list[dict] = []
    for policy in policies:
        if not chart_render.selects(policy["spec"]["podSelector"], server):
            continue
        for rule in policy["spec"].get("ingress") or []:
            if "ports" in rule and port not in {entry.get("port") for entry in rule["ports"]}:
                continue
            peers += [peer["podSelector"] for peer in rule.get("from", [{"podSelector": {}}]) if "podSelector" in peer and "namespaceSelector" not in peer]
    return peers


def _admitted(peers: list[dict], pods: list[_Pod]) -> set[str]:
    """The pods one of ``peers`` selects."""
    return {pod.workload for pod in pods if any(chart_render.selects(peer, pod.labels) for peer in peers)}


def _each_way_in(peer: dict) -> list[dict]:
    """The selectors ``peer`` admits by: one for each value an `In` expression lists, since a pod carrying any one of
    them is admitted, and each is a label somebody could take."""
    ways = [peer]
    for index, expression in enumerate(peer.get("matchExpressions") or []):
        if expression["operator"] == "In":
            ways = [
                {**way, "matchExpressions": [*way["matchExpressions"][:index], {**expression, "values": [value]}, *way["matchExpressions"][index + 1 :]]}
                for way in ways
                for value in expression["values"]
            ]
    return ways


def _server(pods: list[_Pod], component: str) -> _Pod:
    [server] = [pod for pod in pods if pod.labels.get("app.kubernetes.io/component") == component]
    return server


@pytest.mark.parametrize("store", sorted(_STORES))
def test_a_development_store_admits_exactly_the_pods_that_dial_it(store: str) -> None:
    """The ingress the chart renders for a development store's servers admits, on its client port, exactly the pods of
    its namespace that dial it, and names no peer that no pod is: a label nobody carries admits whoever takes it."""
    wanted = _STORES[store]
    docs = _namespace()
    pods = _pods(docs)
    server = _server(pods, wanted.component)
    peers = _peers([doc for doc in docs if doc.get("kind") == "NetworkPolicy"], server.labels, wanted.port)

    dialers = {pod.workload for pod in pods if pod.workload != server.workload and _names(pod, wanted.service)}
    admitted = _admitted(peers, pods) - {server.workload}
    carried_by_no_pod = [way for peer in peers for way in _each_way_in(peer) if not _admitted([way], pods)]

    # The catalog dials every store; without it the walk read no pod at all.
    assert "Deployment/rask-catalog" in dialers, f"{store}: dialers read as {sorted(dialers)}"
    assert (sorted(admitted - dialers), sorted(dialers - admitted)) == ([], []), f"{store}: admitted and not dialling it, then dialling it and not admitted"
    assert carried_by_no_pod == [], f"{store}: the door admits peers no pod of the namespace is"


def test_the_bus_fixtures_servers_reach_each_other_on_the_cluster_port() -> None:
    """Each of the bus fixture's servers is admitted by its peers on the cluster port, and nothing else is."""
    docs = _namespace()
    pods = _pods(docs)
    server = _server(pods, "nats")

    admitted = _admitted(_peers([doc for doc in docs if doc.get("kind") == "NetworkPolicy"], server.labels, _CLUSTER_PORT), pods)

    assert admitted == {server.workload}
