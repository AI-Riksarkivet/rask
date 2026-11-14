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
its namespace: a pod dials a store when its spec, or a ConfigMap or Secret it mounts, names the store's Service, and the
bus too when it runs a Dapr sidecar, whose pub/sub Component names the bus. A `helm test` pod runs only when someone runs
`helm test`. Read with the sidecars and without them ([[XC-199]]): a service dials its secret store itself, so that
store's door does not follow the sidecars, and the bus's does.
"""

from __future__ import annotations

import json
import re

import pytest
from pydantic import BaseModel, ConfigDict

from tests.unit import chart_render


#: The deployed overlay names the fixture's bus; `nats.url` is set again because a render's prerequisites `--set` another
#: address, which outranks a values file.
_DEPLOYED: tuple[str, ...] = (
    "--set", "image.localImages=true", "-f", str(chart_render.REPO / "deploy/values-local.yaml"),
    "--set", "networkPolicy.enabled=true", "--set-string", "nats.url=nats://rask-nats:4222",
)  # fmt: skip

#: The same estate with no sidecar: nothing publishes control events, which only a sidecar carries.
_NO_SIDECARS: tuple[str, ...] = ("--set", "dapr.enabled=false", "--set", "catalog.controlEmit=false")

#: The fixtures a development estate installs into the release's namespace: their pods are governed by the chart's
#: policies as the release's own are.
_NAMESPACE_FIXTURES = ("dex", "marquez", "nats", "openbao", "rustfs")

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


def _namespace(*overlay: str) -> tuple[dict, ...]:
    """Every document the deployed release, under ``overlay``, and the fixtures sharing its namespace render."""
    release = chart_render.render(*_DEPLOYED, *overlay)
    return release + tuple(doc for fixture in _NAMESPACE_FIXTURES for doc in chart_render.render_fixture(fixture))


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


def _admitted(policies: list[dict], server: dict[str, str], port: int, pods: list[_Pod]) -> set[str]:
    """The pods an ingress rule of a policy selecting the server admits on `port`, from its own namespace."""
    admitted: set[str] = set()
    for policy in policies:
        if not chart_render.selects(policy["spec"]["podSelector"], server):
            continue
        for rule in policy["spec"].get("ingress") or []:
            if "ports" in rule and port not in {entry.get("port") for entry in rule["ports"]}:
                continue
            peers = [peer["podSelector"] for peer in rule.get("from", [{"podSelector": {}}]) if "podSelector" in peer and "namespaceSelector" not in peer]
            admitted |= {pod.workload for pod in pods if any(chart_render.selects(peer, pod.labels) for peer in peers)}
    return admitted


def _server(pods: list[_Pod], component: str) -> _Pod:
    [server] = [pod for pod in pods if pod.labels.get("app.kubernetes.io/component") == component]
    return server


@pytest.mark.parametrize(
    ("store", "overlay"),
    [
        *(pytest.param(store, (), id=f"{store}-with-sidecars") for store in sorted(_STORES)),
        # The two doors a sidecar bears on: the bus's follows it, and the secret store's does not.
        pytest.param("bus", _NO_SIDECARS, id="bus-without-sidecars"),
        pytest.param("secret store", _NO_SIDECARS, id="secret store-without-sidecars"),
    ],
)
def test_a_development_store_admits_exactly_the_pods_that_dial_it(store: str, overlay: tuple[str, ...]) -> None:
    """The ingress the chart renders for a development store's servers admits, on its client port, exactly the pods of
    its namespace that dial it."""
    wanted = _STORES[store]
    docs = _namespace(*overlay)
    pods = _pods(docs)
    server = _server(pods, wanted.component)
    policies = [doc for doc in docs if doc.get("kind") == "NetworkPolicy"]

    dialers = {
        pod.workload
        for pod in pods
        if pod.workload != server.workload and (_names(pod, wanted.service) or (store == "bus" and pod.labels.get("dapr.io/enabled") == "true"))
    }
    admitted = _admitted(policies, server.labels, wanted.port, pods) - {server.workload}

    # The catalog dials every store but a bus no sidecar carries it to; without it the walk read no pod at all.
    assert ("Deployment/rask-catalog" in dialers) == (store != "bus" or not overlay), f"{store}: dialers read as {sorted(dialers)}"
    assert (sorted(admitted - dialers), sorted(dialers - admitted)) == ([], []), f"{store}: admitted and not dialling it, then dialling it and not admitted"


def test_the_bus_fixtures_servers_reach_each_other_on_the_cluster_port() -> None:
    """Each of the bus fixture's servers is admitted by its peers on the cluster port, and nothing else is."""
    docs = _namespace()
    pods = _pods(docs)
    server = _server(pods, "nats")

    admitted = _admitted([doc for doc in docs if doc.get("kind") == "NetworkPolicy"], server.labels, _CLUSTER_PORT, pods)

    assert admitted == {server.workload}
