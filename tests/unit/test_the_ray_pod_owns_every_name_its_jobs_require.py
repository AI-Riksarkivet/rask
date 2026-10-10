"""A Ray job's REQUIRED environment comes from the pod that runs it, not from whoever submitted it.

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

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from chart_yaml import FAST_LOADER

from tests.unit.chart_render import ESO_ARGS, RAY_ARGS


REPO = Path(__file__).resolve().parents[2]

#: Names `scripts/ray_stage_job.py` reads from the process environment and that no `WorkOrder` can
#: supply, so the POD must carry them. `S3_KEY`/`S3_SECRET` are here for completeness: they already
#: come from the pod, and a regression that moved them back onto the wire would put a credential into
#: `GET /api/jobs/<id>`, which the dashboard answers to any reader.
_POD_MUST_PROVIDE = ("S3_ENDPOINT", "S3_KEY", "S3_SECRET")


def _render(*extra: str) -> list[dict]:
    if not shutil.which("helm"):  # pragma: no cover - CI installs helm
        pytest.skip("helm not on PATH")
    cmd = ["helm", "template", "rask", str(REPO / "chart")]
    cmd += [*ESO_ARGS, *RAY_ARGS]
    cmd += ["--set-string", "frontend.oidc.publicIssuer=http://localhost:8080/dex"]
    cmd += ["--set-string", "frontend.oidc.publicOrigin=http://localhost:8080"]
    cmd += ["--set", "image.localImages=true", *extra]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.load_all(out, Loader=FAST_LOADER) if d]


def _ray_head_env(docs: list[dict]) -> dict[str, dict]:
    """Every env row on the RayCluster head container, by name."""
    for doc in docs:
        if doc.get("kind") != "RayCluster":
            continue
        for container in doc["spec"]["headGroupSpec"]["template"]["spec"]["containers"]:
            if container["name"] in {"ray-head", "ray"}:
                return {row["name"]: row for row in container.get("env", [])}
    raise AssertionError("no RayCluster head container rendered — this gate would pass over nothing")


def test_the_head_carries_every_name_a_stage_job_requires() -> None:
    """The job reads these off its own process env; the pod is the only thing that can put them there."""
    env = _ray_head_env(_render("--set", "ray.cluster.enabled=true"))

    missing = [name for name in _POD_MUST_PROVIDE if name not in env]
    assert not missing, f"the Ray head does not provide {missing} — a stage job on it dies before reading a byte"
