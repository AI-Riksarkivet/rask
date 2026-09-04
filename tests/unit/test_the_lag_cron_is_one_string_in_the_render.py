"""The lag cron's Component name, the producer's env var and the served path are one string — rendered.

The service-side gate (`services/medallion/tests/test_the_lag_cron_route_is_one_string.py`) proves the
PATH follows the setting. This proves the CHART hands the same string to both sides, which is the half
a unit test of the app cannot see.

Dapr delivers an input binding to `POST /<component name>` at the pod ROOT. So a Component named one
thing while the app is told another is a cron that fires into a 404 forever, with the Component
healthy, the pod green and the detector silent — indistinguishable from a cascade with no lag.
`rask-services-fleet` records the same rule for the notifications reconciler, where all three are
rendered from one values key for exactly this reason.
"""

from __future__ import annotations

import yaml

from tests.unit.test_invariants import _helm_template


def _render() -> list[dict]:
    return [d for d in yaml.safe_load_all(_helm_template("medallion.enabled=true", "dapr.enabled=true")) if d]


def test_the_component_and_the_env_var_carry_the_SAME_name() -> None:
    docs = _render()
    components = [d for d in docs if d.get("kind") == "Component" and d["spec"]["type"] == "bindings.cron" and "cascade-lag" in d["metadata"]["name"]]
    assert len(components) == 1, f"expected exactly one cascade-lag cron Component, got {[c['metadata']['name'] for c in components]}"
    component_name = components[0]["metadata"]["name"]

    told: list[str] = []
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            for env in container.get("env") or []:
                if env.get("name") == "MEDALLION_CASCADE_LAG_BINDING_NAME":
                    told.append((doc["metadata"]["name"], env.get("value")))

    assert told, "no Deployment is told the binding name — the Component fires into a 404 forever"
    assert {value for _, value in told} == {component_name}, f"Component is {component_name!r} but the app is told {told!r}"


def test_only_the_PRODUCER_is_told() -> None:
    """The door lives on the producer — it is the only service holding `transform_routes`, and so the
    only one that can see a first-ever hop. A mover carrying the name would advertise a route its own
    sidecar has no Component for."""
    docs = _render()
    carriers = [
        doc["metadata"]["name"]
        for doc in docs
        if doc.get("kind") == "Deployment"
        for container in doc["spec"]["template"]["spec"]["containers"]
        for env in container.get("env") or []
        if env.get("name") == "MEDALLION_CASCADE_LAG_BINDING_NAME"
    ]
    assert carriers == [c for c in carriers if c.endswith("-medallion-producer")], carriers


def test_the_component_is_scoped_to_the_producer_app_id() -> None:
    """An unscoped Component is invisible to the sidecar that must deliver it."""
    docs = _render()
    component = next(d for d in docs if d.get("kind") == "Component" and "cascade-lag" in d["metadata"]["name"])
    assert component.get("scopes"), "the cascade-lag cron Component is scoped to no app-id, so nothing delivers it"
