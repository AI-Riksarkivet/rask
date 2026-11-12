"""No pod the release runs may send to an arbitrary destination, and each platform endpoint is reached by exactly the pods
that dial it.

[[XC-143]]. Under `networkPolicy.enabled` every pod in the namespace is default-deny for egress, and each allow names
where it goes. An egress rule with no `to` peers matches EVERY destination on its ports, in the cluster and outside it,
so one such rule on `podSelector: {}` hands every pod a path to any host on 443. A peer `ipBlock` of a `/0` CIDR is the
same rule spelled out.

[[XC-202]]. The endpoints the platform serves outside the namespace (the API server, and on a real deployment its S3,
its NATS bus, its IdP and the graph backend lineage forwards to) cannot be derived from a URL, so the platform states
each as rules. A pod that dials one the layer names no rule for reaches nothing, so the render refuses a real deployment
that leaves one unstated, in one message naming every missing value, and admits the stated peer to exactly the pods that
dial it.

READ FROM THE RENDER, in the overlay that ships the isolation layer (production) and the default values with it
switched on, each with every pod that calls the API server rendered. The render refuses a stated rule that names no
peers: that refusal is what makes the first claim hold for any values, so the first test drives it.

The console's pod (the lakehouse's UI, a static server) is the one first-party pod that dials nothing: every egress allow
leaves it out, so the default-deny holds for it, and its one port is admitted from anywhere, as the Gateway's data plane
is the platform's ([[XC-195]]).
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable

import pytest
import yaml
from pydantic import BaseModel, ConfigDict

from tests.unit import chart_render
from tests.unit.chart_yaml import FAST_LOADER


_OVERLAYS: dict[str, tuple[str, ...]] = {
    "default": (
        *chart_render.DEFAULT_ARGS, *chart_render.OBSERVABILITY_ARGS,
        "--set", "networkPolicy.enabled=true", "--set", "backups.volumeSnapshot.enabled=true",
    ),
    "prod": chart_render.PROD_ARGS,
}  # fmt: skip

_FIRST_PARTY_SOURCE = "rask/templates/"

#: Every list of egress rules the render takes from values, by the overlay that renders the policy reading it.
_STATED_RULES: dict[str, tuple[str, ...]] = {
    "default": ("networkPolicy.apiServerEgress", "networkPolicy.extraEgress", "observability.export.egress"),
    "prod": ("auth.fgaEgress", "openbao.egress", "objectStore.egress", "nats.egress", "auth.oidc.egress", "services.lineage.forward.egress"),
}

#: The egress a real deployment states for the platform's store, bus and IdP, by what each reaches ([[XC-202]]).
_UNSTATED_ON_A_REAL_DEPLOYMENT = ("objectStore.egress", "nats.egress", "auth.oidc.egress")


class _Pod(BaseModel):
    """One pod template the release runs, reduced to what says which endpoints it dials."""

    model_config = ConfigDict(frozen=True)

    workload: str
    labels: dict[str, str]
    first_party: bool
    holds_an_api_credential: bool
    env: frozenset[str]
    commands: str


#: Whether a pod dials each platform endpoint, read off its own spec: a token the API server accepts, the store's or the
#: IdP's address in its env, and for the bus a Dapr sidecar (every pub/sub Component dials `nats.url`) or a container that
#: names the bus itself (the stream Job).
_DIALS: dict[str, Callable[[_Pod], bool]] = {
    "api server": lambda pod: pod.holds_an_api_credential,
    "object store": lambda pod: chart_render.OBJECT_STORE_URL in pod.env,
    "issuer": lambda pod: chart_render.ISSUER_URL in pod.env,
    "bus": lambda pod: pod.labels.get("dapr.io/enabled") == "true" or chart_render.NATS_URL in pod.env or chart_render.NATS_URL in pod.commands,
    "graph backend": lambda pod: chart_render.GRAPH_URL in pod.env,
}


class _Endpoint(BaseModel):
    """A platform endpoint: the peer the render's overlays state for it, the overlays whose pods dial it from outside the
    namespace (the default values run the store, the bus and the IdP in the namespace, as development fixtures), and a
    workload known to dial it, without which the walk read nothing."""

    model_config = ConfigDict(frozen=True)

    cidr: str
    overlays: tuple[str, ...]
    known_caller: str


_ENDPOINTS: dict[str, _Endpoint] = {
    "api server": _Endpoint(cidr=chart_render.API_SERVER_CIDR, overlays=("default", "prod"), known_caller="Deployment/rask-otel-collector"),
    "object store": _Endpoint(cidr=chart_render.OBJECT_STORE_CIDR, overlays=("prod",), known_caller="Deployment/rask-catalog"),
    "bus": _Endpoint(cidr=chart_render.BUS_CIDR, overlays=("prod",), known_caller="Job/rask-nats-stream-r1"),
    "issuer": _Endpoint(cidr=chart_render.ISSUER_CIDR, overlays=("prod",), known_caller="Deployment/rask-catalog"),
    "graph backend": _Endpoint(cidr=chart_render.GRAPH_CIDR, overlays=("prod",), known_caller="Deployment/rask-lineage"),
}


def _documents(*args: str) -> list[tuple[str, dict]]:
    """`(source, document)` for every mapping the chart renders, the source read from Helm's header."""
    found: list[tuple[str, dict]] = []
    for chunk in chart_render.render_text(*args).split("\n---\n"):
        source = re.search(r"^# Source: (\S+)", chunk, re.MULTILINE)
        if source:
            found.extend((source.group(1), doc) for doc in yaml.load_all(chunk, Loader=FAST_LOADER) if isinstance(doc, dict))
    return found


def _holds_an_api_credential(template: dict, accounts: dict[str, dict]) -> bool:
    """A mounted token, or a projected one with no audience: either is presented to the API server."""
    spec = template["spec"]
    account = accounts.get(spec.get("serviceAccountName") or "default") or {}
    if spec.get("automountServiceAccountToken", account.get("automountServiceAccountToken", True)) is not False:
        return True
    return any(
        "serviceAccountToken" in source and "audience" not in source["serviceAccountToken"]
        for volume in spec.get("volumes") or []
        for source in (volume.get("projected") or {}).get("sources") or []
    )


def _pods(*args: str) -> list[_Pod]:
    documents = _documents(*args)
    accounts = {doc["metadata"]["name"]: doc for _, doc in documents if doc.get("kind") == "ServiceAccount"}
    pods: list[_Pod] = []
    for source, doc in documents:
        template = chart_render.pod_template(doc)
        if template is None:
            continue
        containers = [*(template["spec"].get("initContainers") or []), *template["spec"]["containers"]]
        pods.append(
            _Pod(
                workload=f"{doc['kind']}/{doc['metadata']['name']}",
                labels=template["metadata"].get("labels") or {},
                first_party=source.startswith(_FIRST_PARTY_SOURCE),
                holds_an_api_credential=_holds_an_api_credential(template, accounts),
                env=frozenset(value for container in containers for value in chart_render.env_of(container).values()),
                commands=json.dumps([[*(container.get("command") or []), *(container.get("args") or [])] for container in containers]),
            )
        )
    return pods


def _admits_everything(peer: dict) -> bool:
    return str((peer.get("ipBlock") or {}).get("cidr", "")).endswith("/0")


@pytest.mark.parametrize("overlay", sorted(_OVERLAYS))
def test_every_egress_rule_names_the_peers_it_admits(overlay: str) -> None:
    """Each egress rule of each NetworkPolicy names its peers, none of them every address, and a rule stated in values
    that names no peer, or every address, is refused. An egress policy with no rules allows nothing."""
    policies = [doc for doc in chart_render.render(*_OVERLAYS[overlay]) if doc.get("kind") == "NetworkPolicy"]
    rules = [(policy["metadata"]["name"], rule) for policy in policies for rule in policy["spec"].get("egress") or []]

    # The walk reads the isolation layer: default-deny with DNS, the namespace, the Dapr plane, the API server, the Gateway.
    assert len(rules) >= 5, f"{overlay}: only {len(rules)} egress rules rendered"
    unbounded = [f"{name}: {rule}" for name, rule in rules if not rule.get("to") or any(_admits_everything(peer) for peer in rule["to"])]
    assert unbounded == [], f"{overlay}: egress rules that admit every destination on their ports: {unbounded}"

    admitted = []
    for stated in _STATED_RULES[overlay]:
        for rule in ('{"ports":[{"protocol":"TCP","port":443}]}', '{"to":[{"ipBlock":{"cidr":"0.0.0.0/0"}}]}'):
            try:
                chart_render.render_text(*_OVERLAYS[overlay], "--set-json", f"{stated}=[{rule}]")
                admitted.append(f"{stated}={rule}")
            except subprocess.CalledProcessError as refused:
                assert "admits every destination" in refused.stderr, f"{stated}={rule} refused for another reason: {refused.stderr[-400:]}"
    assert admitted == [], f"{overlay}: the render admitted stated rules that reach every destination: {admitted}"


def test_a_real_deployment_that_states_no_egress_to_its_store_bus_and_issuer_is_refused_naming_each() -> None:
    """The production render with no egress stated for the platform's S3, bus and IdP fails with ONE message naming every
    missing value: under default-deny the pods that dial them would reach none of them."""
    unstated = [arg for value in _UNSTATED_ON_A_REAL_DEPLOYMENT for arg in ("--set-json", f"{value}=[]")]

    with pytest.raises(subprocess.CalledProcessError) as refused:
        chart_render.render_text(*chart_render.PROD_ARGS, *unstated)

    stderr = refused.value.stderr
    assert stderr.count("execution error") == 1, stderr[-1200:]
    assert [value for value in _UNSTATED_ON_A_REAL_DEPLOYMENT if value not in stderr] == [], stderr[-1200:]


@pytest.mark.parametrize(
    ("endpoint", "overlay"),
    [pytest.param(endpoint, overlay, id=f"{endpoint}-{overlay}") for endpoint, wanted in _ENDPOINTS.items() for overlay in wanted.overlays],
)
def test_only_a_pod_that_dials_a_platform_endpoint_may_reach_it(endpoint: str, overlay: str) -> None:
    """The policies that admit an endpoint's stated peer select exactly the first-party pods that dial it.

    A pod with nothing to say to an endpoint has, in a path there, only a path out; a first-party pod that dials it and is
    not admitted fails at its first call (a verifier's key set, the Collector's pod metadata, a store session, a bus
    connection, an IdP's discovery, a forwarded event). A subchart's pod with an unused token is not ours to admit.
    """
    wanted = _ENDPOINTS[endpoint]
    peer = {"ipBlock": {"cidr": wanted.cidr}}
    pods = _pods(*_OVERLAYS[overlay])
    selectors = [
        doc["spec"]["podSelector"]
        for doc in chart_render.render(*_OVERLAYS[overlay])
        if doc.get("kind") == "NetworkPolicy" and any(peer in (rule.get("to") or []) for rule in doc["spec"].get("egress") or [])
    ]

    admitted = {pod.workload for pod in pods if any(chart_render.selects(selector, pod.labels) for selector in selectors)}
    callers = {pod.workload for pod in pods if pod.first_party and _DIALS[endpoint](pod)}

    assert wanted.known_caller in callers, f"{endpoint} ({overlay}): callers read as {sorted(callers)}"
    assert (sorted(admitted - callers), sorted(callers - admitted)) == ([], []), (
        f"{endpoint} ({overlay}): admitted and not dialling it, then dialling it and not admitted"
    )


@pytest.mark.parametrize("overlay", sorted(_OVERLAYS))
def test_the_console_pod_dials_nothing_and_is_admitted_on_its_port_from_anywhere(overlay: str) -> None:
    """Every NetworkPolicy selecting the console's pod admits it no egress, and one admits its port from any peer."""
    docs = chart_render.render(*_OVERLAYS[overlay])
    [labels] = [doc["spec"]["template"]["metadata"]["labels"] for doc in docs if doc.get("kind") == "Deployment" and doc["metadata"]["name"] == "rask-console"]
    selecting = [doc for doc in docs if doc.get("kind") == "NetworkPolicy" and chart_render.selects(doc["spec"]["podSelector"], labels)]

    egress = {doc["metadata"]["name"]: doc["spec"]["egress"] for doc in selecting if doc["spec"].get("egress")}
    open_ports = [rule["ports"] for doc in selecting for rule in doc["spec"].get("ingress") or [] if "from" not in rule]
    assert (egress, open_ports) == ({}, [[{"protocol": "TCP", "port": 3000}]])
