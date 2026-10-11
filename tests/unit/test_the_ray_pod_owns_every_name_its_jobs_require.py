"""A Ray job's REQUIRED environment comes from the pod that runs it, not from whoever submitted it.

The pod is the engine's: rask installs no Ray (ADR 0153), and `make k3s-up` runs the head from the ray-cluster fixture
(`deploy/fixtures/ray-cluster`), so this reads that fixture's render.

`scripts/ray_stage_job.py:86` reads ``os.environ["S3_ENDPOINT"]`` with a BRACKET — a job that does not
get it dies `KeyError` before it reads a byte. Measured on the deployed head 2026-09-18
(`kubectl get pod -l ray.io/is-ray-node=yes`): the container carries `S3_KEY` and `S3_SECRET` and NOT
`S3_ENDPOINT`. Every stage job on this estate works only because `ray_submit.submit_stage_job` injects
the address into `runtime_env.env_vars` on each submission.

THAT IS THE WRONG OWNER, and the chart's own comment beside the credential already argues the case for
it: "NOTHING credential-shaped rides the submission any more, so this pod is the single source and
there is no second value to keep in agreement." The storage ADDRESS is the same shape of fact as the
credential it is used with — one deployment fact about where this cluster's object store is — and it is
not a secret, so nothing about the secrets rule pushes it onto the wire.

IT IS ALSO WHAT BLOCKS [[LH-159]]. Routing `workflow.py` through `executor_for(RAY_ENGINE)` sends
`WorkOrder.to_env()`, which is 15 `RASK_*` keys and deliberately no deployment facts —
`WorkOrder` is `extra="forbid"`, so the submitter's extra keys cannot be smuggled through it. Measured
against a live submission: 26 keys on the wire today, 15 through the port, and `S3_ENDPOINT` among the
11 lost. Wiring the port before this lands turns every stage job into a `KeyError`.

`S3_REGION` rides along because it is the same kind of fact, even though it is inert today
(`ray_stage_job.py:89` defaults it to `us-east-1`, which is what the medallion sends).

TWO OWNERS IS THE FAILURE THIS MUST NOT CREATE, and the estate has already been burned by it on the
sibling keys: `ray_submit.py` records that S3_KEY/S3_SECRET "had two owners and repointing the pod
alone gave every job `SignatureDoesNotMatch`, measured twice on the live estate", because Ray merges
`runtime_env` OVER the process env and the submission silently wins. The resolution then was not to
pick the submission — it was to make the submission carry NOTHING of that shape and let the pod be the
single source, which is what the chart comment beside those two keys now says.

So the target state for this pair is the same, and it took TWO steps in a fixed order: the pod gained
them and was OBSERVED carrying them, and only then did `ray_submit` stop sending them. Doing it in one
step would have left a window where a job got neither; doing only the first would have left exactly the
two-owner drift this paragraph exists to prevent.

OBSERVED, not rendered: with the head recycled, a job submitted with a `runtime_env` carrying NO S3
names read `S3_ENDPOINT=http://rask-minio:9000` and `S3_REGION=us-east-1` out of its own process env
(Ray dashboard `POST /api/jobs/`, SUCCEEDED). The recycle is part of the procedure, not an accident —
**KubeRay does not recreate a head pod when the RayCluster spec changes**, so the CR carried the new
rows while the running pod did not, and `helm get manifest` showed a change that was not live.
"""

from __future__ import annotations

from tests.unit.chart_render import render_fixture


#: Names `scripts/ray_stage_job.py` reads from the process environment and that no `WorkOrder` can supply, so the
#: POD must carry them. `S3_KEY`/`S3_SECRET` already come from the pod, and a regression that moved them back onto
#: the wire would put a credential into `GET /api/jobs/<id>`, which the dashboard answers to any reader.
#: `RASK_LINEAGE_ENDPOINT` is `build_emitter`'s first alias: without it the stage lane's emitter is a no-op that drops
#: every event at DEBUG while every job reports SUCCESS.
_POD_MUST_PROVIDE = ("S3_ENDPOINT", "S3_KEY", "S3_SECRET", "RASK_LINEAGE_ENDPOINT")


def test_the_head_carries_every_name_a_stage_job_requires() -> None:
    """The job reads these off its own process env; the pod is the only thing that can put them there.

    The worker groups are checked in the same pass because the head's process env is what a job inherits only while
    every task runs on the head: a worker group would need this env too, and the assertion would measure half the cluster.
    """
    clusters = [doc for doc in render_fixture("ray-cluster") if doc.get("kind") == "RayCluster"]
    assert clusters, "the ray-cluster fixture renders no RayCluster — this gate would pass over nothing"

    for cluster in clusters:
        head = next(c for c in cluster["spec"]["headGroupSpec"]["template"]["spec"]["containers"] if c["name"] == "ray-head")
        env = {row["name"] for row in head.get("env") or []}
        missing = [name for name in _POD_MUST_PROVIDE if name not in env]
        assert not missing, f"the Ray head does not provide {missing} — a stage job on it dies or emits into nothing"
        assert not cluster["spec"].get("workerGroupSpecs"), "a worker group exists and inherits nothing from the head's env"
