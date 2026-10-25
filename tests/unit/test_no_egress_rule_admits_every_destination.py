"""No pod the release runs may send to an arbitrary destination, and the API server is reached only by the pods that call it.

[[XC-143]]. Under `networkPolicy.enabled` every pod in the namespace is default-deny for egress, and each allow names
where it goes. An egress rule with no `to` peers matches EVERY destination on its ports, in the cluster and outside it,
so one such rule on `podSelector: {}` hands every pod a path to any host on 443. A peer `ipBlock` of a `/0` CIDR is the
same rule spelled out.

READ FROM THE RENDER, in the overlay that ships the isolation layer (production) and the default values with it
switched on, each with every pod that calls the API server rendered. The destinations the platform owns (the API
server, the Gateway, the telemetry backend, an external OpenFGA, an external store) cannot be derived from a URL, so the
platform states them as rules, and the render refuses a stated rule that names no peers: that refusal is what makes the
first claim hold for any values, so the first test drives it.

MUTATION-CHECKED 2026-10-16. With the API-server rule's `to` removed from the template, the first test fails in both
overlays on `rask-allow-apiserver-egress`, and with the OpenFGA rule's in prod on `rask-allow-openfga-egress`; with
the peer guard removed from `lance.egressRules`, it fails on each stated list. With the API-server rule's pod selector widened back to `{}`, the second fails on every tokenless pod, and with
the Collector dropped from its clients, on the Collector.
"""

from __future__ import annotations

import re
import subprocess

import pytest
import yaml

from tests.unit import chart_render
from tests.unit.chart_yaml import FAST_LOADER


#: The API server as `chart_render.PREREQ_ARGS` states it, so the policies that admit it can be found by their peer.
_API_SERVER = {"ipBlock": {"cidr": chart_render.API_SERVER_CIDR}}

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
    "prod": ("auth.fgaEgress", "openbao.egress"),
}


def _documents(*args: str) -> list[tuple[str, dict]]:
    """`(source, document)` for every mapping the chart renders, the source read from Helm's header."""
    found: list[tuple[str, dict]] = []
    for chunk in chart_render.render_text(*args).split("\n---\n"):
        source = re.search(r"^# Source: (\S+)", chunk, re.MULTILINE)
        if source:
            found.extend((source.group(1), doc) for doc in yaml.load_all(chunk, Loader=FAST_LOADER) if isinstance(doc, dict))
    return found


def _pod_template(doc: dict) -> dict | None:
    kind = doc.get("kind")
    if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job"}:
        return doc["spec"]["template"]
    if kind == "CronJob":
        return doc["spec"]["jobTemplate"]["spec"]["template"]
    return None


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


@pytest.mark.parametrize("overlay", sorted(_OVERLAYS))
def test_only_a_pod_that_calls_the_api_server_may_reach_it(overlay: str) -> None:
    """The policies that admit the API server select exactly the first-party pods holding a credential it accepts.

    A tokenless pod has nothing to say to the API server, so a path there is only a path out; a first-party pod that
    holds a token and is not admitted fails at its first call (the Collector's pod metadata, a verifier's key set,
    OpenBao's review of a login, the snapshot's `kubectl`). A subchart's pod with an unused token is not ours to admit.
    """
    documents = _documents(*_OVERLAYS[overlay])
    accounts = {doc["metadata"]["name"]: doc for _, doc in documents if doc.get("kind") == "ServiceAccount"}
    selectors = [
        doc["spec"]["podSelector"]
        for _, doc in documents
        if doc.get("kind") == "NetworkPolicy" and any(_API_SERVER in (rule.get("to") or []) for rule in doc["spec"].get("egress") or [])
    ]
    assert selectors, f"{overlay}: no NetworkPolicy admits the stated API server"

    admitted, callers = set(), set()
    for source, doc in documents:
        template = _pod_template(doc)
        if template is None:
            continue
        workload = f"{doc['kind']}/{doc['metadata']['name']}"
        if any(chart_render.selects(selector, template["metadata"].get("labels") or {}) for selector in selectors):
            admitted.add(workload)
        if source.startswith(_FIRST_PARTY_SOURCE) and _holds_an_api_credential(template, accounts):
            callers.add(workload)

    # The callers this overlay renders: the token verifiers, and the Collector's pod-metadata lookups.
    assert {"Deployment/rask-catalog", "Deployment/rask-otel-collector"} <= callers, f"{overlay}: callers read as {sorted(callers)}"
    assert sorted(admitted - callers) == [], f"{overlay}: pods admitted to the API server that hold no credential for it"
    assert sorted(callers - admitted) == [], f"{overlay}: pods that call the API server and are not admitted to it"
