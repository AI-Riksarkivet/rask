"""Maintenance's worker is its only workload: a process of its own, in pods whose grace holds its drain ([[LH-494]], [[XC-199]]).

The worker takes everything it does off the bus on a connection of its own (its tasks, its fired schedules, the
store's change events and the commit announcements), so its pod serves nothing but its probes: no Service selects it,
and it runs no sidecar, whose shutdown would otherwise have to be held open across the worker's drain. A stopping
worker lets its running pass finish for `maintenance.queue.drainSeconds`, so the pod's grace must hold preStop, that
drain and the teardown; the render refuses a drain it cannot hold (`test_invariants.py`), and this holds the rendered
pod to the same arithmetic with every lifecycle number moved, so only a computed grace passes it.
"""

from __future__ import annotations

import pytest
import yaml

from maintenance.core.config import MaintenanceSettings
from tests.unit.chart_render import DEFAULT_ARGS, REPO, render, selects


@pytest.mark.parametrize(
    "moved",
    [
        pytest.param({}, id="the-shipped-lifecycle"),
        pytest.param(
            {"terminationGracePeriodSeconds": 61, "maintenanceTerminationGracePeriodSeconds": 97, "preStopSeconds": 3, "teardownSeconds": 7},
            id="every-lifecycle-number-moved",
        ),
    ],
)
def test_the_worker_runs_in_pods_of_its_own_with_no_sidecar_and_a_grace_that_holds_its_drain(moved: dict[str, int]) -> None:
    """Its replicas spread over the nodes by their own label, so one node's loss leaves a worker pulling; it takes a
    resources tier of its own, sized from its measured working set; and the audit, the scan and an orphan check are each
    bounded by a value of their own, which reaches the worker's settings as that kind's bound."""
    lifecycle = {**yaml.safe_load((REPO / "chart" / "values.yaml").read_text())["lifecycle"], **moved}
    docs = render(
        *DEFAULT_ARGS,
        *("--set", "maintenance.queue.drainSeconds=60", "--set", "resources.maintenanceWorker.requests.memory=333Mi"),
        *("--set", "maintenance.queue.auditMaxSeconds=1234", "--set", "maintenance.queue.scanMaxSeconds=4321"),
        *(arg for key, value in moved.items() for arg in ("--set", f"lifecycle.{key}={value}")),
    )

    workloads = {
        doc["metadata"]["name"]: doc["spec"]["template"] for doc in docs if doc.get("kind") == "Deployment" and "maintenance" in doc["metadata"]["name"]
    }

    assert list(workloads) == ["rask-maintenance-worker"]
    worker = workloads["rask-maintenance-worker"]
    process = worker["spec"]["containers"][0]
    env = {entry["name"]: entry.get("value") for entry in process["env"]}
    grace = worker["spec"]["terminationGracePeriodSeconds"]
    pre_stop = int(" ".join(process["lifecycle"]["preStop"]["exec"]["command"]).rsplit(" ", 1)[-1])
    selecting = [
        doc["metadata"]["name"]
        for doc in docs
        if doc.get("kind") == "Service" and selects({"matchLabels": doc["spec"].get("selector") or {"none": "none"}}, worker["metadata"]["labels"])
    ]
    of_a_sidecar = sorted(key for key in {**worker["metadata"].get("annotations", {}), **worker["metadata"]["labels"]} if key.startswith("dapr.io/"))
    assert process["args"][-3:] == ["python", "-m", "maintenance.worker"]
    assert (selecting, of_a_sidecar) == ([], [])
    assert [selects(rule["labelSelector"], worker["metadata"]["labels"]) for rule in worker["spec"]["topologySpreadConstraints"]] == [True]
    assert process["resources"]["requests"]["memory"] == "333Mi"
    bounds = MaintenanceSettings(
        s3_bucket="b",
        audit_max_seconds=int(env["MAINTENANCE_AUDIT_MAX_SECONDS"]),
        scan_max_seconds=int(env["MAINTENANCE_SCAN_MAX_SECONDS"]),
        orphan_check_max_seconds=int(env["MAINTENANCE_ORPHAN_CHECK_MAX_SECONDS"]),
    ).max_seconds_by_kind
    assert bounds == {"audit": 1234, "scan": 4321, "orphans": 1800}
    assert (grace, pre_stop) == (int(lifecycle["maintenanceTerminationGracePeriodSeconds"]), int(lifecycle["preStopSeconds"]))
    assert pre_stop + int(env["MAINTENANCE_DRAIN_SECONDS"]) < grace - int(lifecycle["teardownSeconds"])
    assert str(process["ports"][0]["containerPort"]) == env["MAINTENANCE_WORKER_PORT"]
    assert (env["MAINTENANCE_ARRIVAL_ENABLED"], process["livenessProbe"]["httpGet"]["path"], process["readinessProbe"]["httpGet"]["path"]) == (
        "true",
        "/livez",
        "/readyz",
    )
