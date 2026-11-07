"""An autoscaled service's Deployment declares no replica count, so a reconciler never undoes the autoscaler ([[XC-164]]).

A rendered `replicas` is desired state: Argo CD's self-heal sets it back every time the
HorizontalPodAutoscaler moves it, and the two flap. Without the field the Deployment keeps whatever count the autoscaler
set, and the autoscaler's `minReplicas` is the floor its PodDisruptionBudget has to protect.
"""

from __future__ import annotations

from tests.unit.chart_render import DEFAULT_ARGS, render


def test_an_autoscaled_service_takes_its_replica_bounds_from_the_autoscaler() -> None:
    docs = render(*DEFAULT_ARGS, "--set", "autoscaling.enabled=true", "--set", "podDisruptionBudget.enabled=true")

    scaled = {doc["spec"]["scaleTargetRef"]["name"] for doc in docs if doc.get("kind") == "HorizontalPodAutoscaler"}
    replicas = {doc["metadata"]["name"]: doc["spec"].get("replicas") for doc in docs if doc.get("kind") == "Deployment"}
    budgets = {doc["metadata"]["name"] for doc in docs if doc.get("kind") == "PodDisruptionBudget" and doc["metadata"]["name"] in scaled}

    assert scaled == {"rask-catalog", "rask-lineage"}
    assert {name: replicas[name] for name in scaled} == {"rask-catalog": None, "rask-lineage": None}
    # The autoscaler's floor is two pods of each, so a drain that took both would take the service down.
    assert budgets == {"rask-catalog", "rask-lineage"}
