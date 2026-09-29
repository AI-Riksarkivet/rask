"""Maintenance can run as a PLANNER pod and separate EXECUTOR pods over the same queue.

docs/DECISIONS.md "Maintenance leaves the planner pod". Compaction, index-optimize and prune run today in one deployment at
`replicas: 1`, 1 CPU / 512Mi — the work bounded to fit the pod rather than the pod sized to fit the
work. Lakekeeper's documentation reaches the same split and states it as the production practice:
"we recommend running expire snapshots workers in dedicated pods to avoid impacting REST API
performance", achieved by setting the API pod's worker count to zero.

rask needs no new mechanism for this. The tick already PLANS and publishes one `DatasetWorkItem` per
dataset; `/maintenance-work` already EXECUTES one; the work Component already carries
`queueGroupName: maintenance`, so N replicas share the queue with no code change. The only thing
pinning `replicas: 1` is the PLANNER's in-process sweep lock — `bindings.cron` fires on every replica
with no lease. The executor half has never needed that lock: the broker single-flights a unit.

So the split is two switches, and both default to today's behaviour:

* an EXECUTOR pod configures no cron binding names — and `build_router` must then mount NOTHING. It
  currently mounts `f"/{name}"` unconditionally, so an empty name serves a token-guarded cron door at
  the pod ROOT, which is both wrong and reachable;
* a PLANNER pod keeps its bindings and its `work_topic` (it PUBLISHES to the queue) while declining to
  SUBSCRIBE, which needs its own switch — `work_topic` cannot express it, because both halves need it.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from maintenance.api.work import register_work_route
from maintenance.core.config import MaintenanceSettings


def _settings(
    *,
    binding_name: str = "maintenance-cron",
    reconcile_binding_name: str = "maintenance-reconcile-cron",
    work_topic: str = "",
    execute_work: bool = True,
) -> MaintenanceSettings:
    """Named parameters rather than a `**kwargs` splat: `MaintenanceSettings` is fully typed, and
    splatting an untyped mapping erases every one of those signatures."""
    return MaintenanceSettings(
        MAINTENANCE_S3_BUCKET="lance-catalog",
        MAINTENANCE_S3_ACCESS_KEY_ID="k",
        MAINTENANCE_BINDING_NAME=binding_name,
        MAINTENANCE_RECONCILE_BINDING_NAME=reconcile_binding_name,
        MAINTENANCE_WORK_TOPIC=work_topic,
        MAINTENANCE_EXECUTE_WORK=execute_work,
    )


@pytest.mark.parametrize(("execute_work", "subscribes"), [pytest.param(False, False, id="planner"), pytest.param(True, True, id="executor")])
def test_only_an_EXECUTOR_subscribes_while_both_keep_the_topic(execute_work: bool, subscribes: bool) -> None:
    """The switch `work_topic` cannot express: the planner needs the topic to PUBLISH onto it, and must
    not also consume from it, or the split does nothing."""
    app = FastAPI()
    settings = _settings(work_topic="maintenance.work.v1", execute_work=execute_work)
    assert (register_work_route(app, settings) is not None) is subscribes
    assert settings.work_topic == "maintenance.work.v1", "the planner still needs the topic to publish onto"
