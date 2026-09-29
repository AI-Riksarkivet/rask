"""The chart can run maintenance as a planner pod plus dedicated executor pods.

docs/DECISIONS.md "Maintenance leaves the planner pod", which Lakekeeper's docs state as the production practice: "we
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
from chart_yaml import FAST_LOADER

from tests.unit.test_invariants import _helm_template


#: The split needs a queue: with no work topic there is nothing for a worker to consume, so the chart
#: renders no worker at all. Pinned by `test_the_split_requires_a_queue`.
_QUEUE = "maintenance.workTopic=maintenance.work.v1"


def _deployments(*sets: str) -> dict[str, dict]:
    docs = [d for d in yaml.load_all(_helm_template(*sets), Loader=FAST_LOADER) if d]
    return {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Deployment"}


def _env(dep: dict) -> dict[str, str]:
    return {e["name"]: e.get("value", "") for c in dep["spec"]["template"]["spec"]["containers"] for e in (c.get("env") or [])}


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
