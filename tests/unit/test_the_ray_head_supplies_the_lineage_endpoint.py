"""The Ray head's own env carries the lineage endpoint, so a `WorkOrder` does not.

Measured live 2026-09-21, the Ray head's own process env already carries
`RASK_LINEAGE_ENDPOINT=http://rask-lineage:8000` (rendered by `lance.lineageEmitEnv`), which is
`build_emitter`'s FIRST alias choice. It is one deployment fact about where this cluster's lineage
ingest is, the same shape as `S3_ENDPOINT`, which left the submission for the same reason. Sending it too
would give one value two owners, and Ray merges `runtime_env.env_vars` OVER the worker's process env, so
the submission would WIN: a repointed pod keeps talking to the old address and nothing says so.
"""

from __future__ import annotations


def test_the_ray_head_supplies_the_endpoint_the_submission_no_longer_carries() -> None:
    """Why this is a gate rather than a comment.

    Dropping `LINEAGE_URL` from the submission is only correct because the POD holds the endpoint. If
    `lance.lineageEmitEnv` ever leaves the Ray head, the stage lane loses its transport and the loss is
    SILENT BY CONSTRUCTION — `build_emitter` logs one warning and returns `NoopEmitter`, which drops
    every event at DEBUG, so a lane emitting into nothing is indistinguishable from outside from a lane
    that finished cleanly. The graph is simply empty and every job reports SUCCESS.

    The worker groups are counted in the same pass because the head's process env is what a job
    inherits. `workerGroupSpecs` is empty today, so every task runs on the head and the head's env is
    the whole story; the moment a worker group exists it needs this env too, and the assertion below
    will be measuring only half the cluster. Failing then is correct.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import chart_render

    heads = [
        (doc["metadata"]["name"], container, spec)
        for doc in chart_render.render("--set", "ray.enabled=true", "--set", "ray.cluster.enabled=true", "--set", "image.localImages=true")
        if doc.get("kind") in ("RayCluster", "RayService")
        for spec in [doc["spec"].get("rayClusterConfig", doc["spec"])]
        for container in spec["headGroupSpec"]["template"]["spec"]["containers"]
    ]
    assert heads, "no Ray head rendered — this gate would pass while asserting about nothing"

    for name, container, spec in heads:
        assert chart_render.env_of(container).get("RASK_LINEAGE_ENDPOINT"), (
            f"{name}/{container['name']} carries no RASK_LINEAGE_ENDPOINT, and the submission stopped sending one — the stage lane emits into a no-op"
        )
        assert not spec.get("workerGroupSpecs"), "a worker group exists and inherits nothing from the head's env — this gate now covers half the cluster"
