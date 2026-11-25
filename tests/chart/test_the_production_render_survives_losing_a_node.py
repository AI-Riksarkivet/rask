"""Every serving workload the production render runs survives losing one node, a drain included.

A Deployment does when it runs the replicas its value names, two or more, a PodDisruptionBudget keeps a drain from evicting all of them at
once, and the scheduler is held to placing them on different nodes. With `ScheduleAnyway` it may put both on one node
whenever that scores better, and the node's loss is then the service's. A node the pods may not run on (a tainted
control plane) is no domain to spread over, or a surge pod finding the other nodes full would wait on it forever.
"""

from tests.chart.chart_render import PROD_ARGS, render


def _selects(selector: dict, labels: dict) -> bool:
    return all(labels.get(key) == value for key, value in selector["matchLabels"].items())


def test_every_serving_workload_runs_on_two_nodes_and_keeps_one_through_a_drain() -> None:
    docs = render(*PROD_ARGS, "--set", "maintenance.worker.replicas=3")
    budgets = [doc["spec"] for doc in docs if doc.get("kind") == "PodDisruptionBudget"]

    found = {}
    for doc in docs:
        if doc.get("kind") != "Deployment" or str(doc["metadata"].get("labels", {}).get("helm.sh/chart", "")).startswith("openfga"):
            continue
        pod = doc["spec"]["template"]
        labels = pod["metadata"]["labels"]
        found[doc["metadata"]["name"]] = (
            doc["spec"]["replicas"],
            [budget["minAvailable"] for budget in budgets if _selects(budget["selector"], labels)],
            [
                (rule["topologyKey"], rule["whenUnsatisfiable"], rule.get("nodeTaintsPolicy"), _selects(rule["labelSelector"], labels))
                for rule in pod["spec"].get("topologySpreadConstraints", [])
            ],
        )

    spread = [("kubernetes.io/hostname", "DoNotSchedule", "Honor", True)]
    assert found == {
        "rask-catalog": (2, [1], spread),
        "rask-console": (2, [1], spread),
        "rask-lineage": (2, [1], spread),
        "rask-maintenance-worker": (3, [1], spread),
    }
