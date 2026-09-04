"""The chart can run maintenance as a planner pod plus dedicated executor pods.

`open_maintenance_compute.md` M1, which Lakekeeper's docs state as the production practice: "we
recommend running expire snapshots workers in dedicated pods to avoid impacting REST API performance",
with the API pod's worker count set to zero.

Today one deployment does both at `replicas: 1`, 1 CPU / 512Mi — and that pod runs compaction,
index-optimize and prune. The `replicas: 1` pin belongs to the PLANNER (its sweep lock, because
`bindings.cron` fires on every replica with no lease); the EXECUTOR has never needed it, because the
broker single-flights a unit and the work Component already carries `queueGroupName: maintenance`.

OFF BY DEFAULT. An estate that has not opted in must render exactly what it renders today — a split
arriving by upgrade would move compaction to a pod nobody sized.
"""

from __future__ import annotations

import yaml

from tests.unit.test_invariants import _helm_template


#: The split needs a queue: with no work topic there is nothing for a worker to consume, so the chart
#: renders no worker at all. Pinned by `test_the_split_requires_a_queue`.
_QUEUE = "maintenance.workTopic=maintenance.work.v1"


def _deployments(*sets: str) -> dict[str, dict]:
    docs = [d for d in yaml.safe_load_all(_helm_template(*sets)) if d]
    return {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Deployment"}


def _env(dep: dict) -> dict[str, str]:
    return {e["name"]: e.get("value", "") for c in dep["spec"]["template"]["spec"]["containers"] for e in (c.get("env") or [])}


def test_OFF_by_default_renders_exactly_one_maintenance_deployment() -> None:
    names = [n for n in _deployments("maintenance.enabled=true") if "maintenance" in n]
    assert len(names) == 1, f"the split must be opt-in; got {names}"


def test_ON_renders_a_separate_executor() -> None:
    deps = _deployments("maintenance.enabled=true", _QUEUE, "maintenance.dedicatedWorkers.enabled=true")
    names = sorted(n for n in deps if "maintenance" in n)
    assert len(names) == 2, f"expected a planner and an executor, got {names}"


def test_the_PLANNER_keeps_its_crons_and_does_not_consume() -> None:
    deps = _deployments("maintenance.enabled=true", _QUEUE, "maintenance.dedicatedWorkers.enabled=true")
    planner = next(d for n, d in deps.items() if n.endswith("-maintenance"))
    env = _env(planner)
    assert env.get("MAINTENANCE_EXECUTE_WORK") == "false", "the planner still consumes the queue, so the split does nothing"
    assert env.get("MAINTENANCE_BINDING_NAME"), "the planner lost its sweep cron"
    assert env.get("MAINTENANCE_WORK_TOPIC"), "the planner needs the topic to PUBLISH onto"
    assert planner["spec"]["replicas"] == 1, "the planner's sweep lock is process-local; it cannot scale"


def test_the_EXECUTOR_consumes_and_serves_NO_cron() -> None:
    deps = _deployments("maintenance.enabled=true", _QUEUE, "maintenance.dedicatedWorkers.enabled=true")
    executor = next(d for n, d in deps.items() if n.endswith("-maintenance-worker"))
    env = _env(executor)
    assert env.get("MAINTENANCE_WORK_TOPIC"), "the executor has no queue to consume"
    assert env.get("MAINTENANCE_EXECUTE_WORK") != "false"
    assert env.get("MAINTENANCE_BINDING_NAME", "") == "", (
        "the executor is configured with a cron binding — it would run a second whole-estate sweep beside the planner's"
    )
    assert env.get("MAINTENANCE_RECONCILE_BINDING_NAME", "") == ""


def test_the_EXECUTOR_may_scale_and_is_sized_for_the_work() -> None:
    """The point of the exercise. `queueGroupName` makes replicas competing consumers, and the memory
    ceiling is what currently bounds compaction to `batch_size=64`."""
    deps = _deployments("maintenance.enabled=true", _QUEUE, "maintenance.dedicatedWorkers.enabled=true", "maintenance.dedicatedWorkers.replicas=3")
    executor = next(d for n, d in deps.items() if n.endswith("-maintenance-worker"))
    assert executor["spec"]["replicas"] == 3
    limits = executor["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]
    assert limits.get("memory"), "the executor has no memory limit of its own — it inherits the planner's 512Mi"


def test_the_split_requires_a_queue() -> None:
    """Dedicated workers with no work topic would be pods subscribed to nothing — a silently idle
    deployment that looks like capacity. The chart renders no worker at all."""
    deps = _deployments("maintenance.enabled=true", "maintenance.dedicatedWorkers.enabled=true")
    assert [n for n in deps if n.endswith("-maintenance-worker")] == []
