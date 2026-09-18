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
    cmd += ["--set-string", "frontend.oidc.sessionSecret=test-session-secret-32-chars-minimum"]
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


def test_the_ADDRESS_is_a_value_and_the_CREDENTIAL_is_a_reference() -> None:
    """The two are different kinds of fact and must not be delivered the same way.

    An endpoint is public routing information: an inline value is correct and a Secret would only hide
    it from the reader who needs it. A key is not, and has to stay a `secretKeyRef` — inlining one here
    would put it in `helm get manifest` and in the pod spec that `GET /api/jobs/<id>` mirrors.
    """
    env = _ray_head_env(_render("--set", "ray.cluster.enabled=true"))

    assert env["S3_ENDPOINT"].get("value"), "the endpoint should be a plain value"
    for secret in ("S3_KEY", "S3_SECRET"):
        assert "valueFrom" in env[secret], f"{secret} stopped being a reference"
        assert "value" not in env[secret], f"{secret} is inlined into the manifest"


def test_the_stage_job_still_reads_the_name_this_gate_provisions() -> None:
    """The gate is only worth anything while the job actually requires it.

    Asserted against the script rather than assumed: if `ray_stage_job.py` ever resolves the endpoint
    another way, this whole file should be deleted rather than left passing over a dead requirement.
    """
    source = (REPO / "scripts/ray_stage_job.py").read_text()

    assert 'os.environ["S3_ENDPOINT"]' in source, "the stage job no longer hard-requires S3_ENDPOINT — re-justify this gate"


def test_the_submitter_sends_no_S3_NAME_AT_ALL() -> None:
    """STEP TWO. Ray merges `runtime_env` OVER the process env, so a name sent by the submitter BEATS
    the pod's — which is the two-owner drift the module docstring is about, and which this estate has
    already paid for once on the credential halves.

    Asserted on the SUBMISSION BODIES, not on the module: `settings.s3_endpoint` is still read
    elsewhere in this file (the medallion does its own S3 work), and a grep for the setting would go
    red for the wrong reason.
    """
    source = (REPO / "services/medallion/src/medallion/services/ray_submit.py").read_text()

    for name in ("S3_ENDPOINT", "S3_KEY", "S3_SECRET", "S3_REGION"):
        assert f'"{name}": ' not in source, f"{name} is back on the submission body — the pod and the submitter now disagree silently"


def test_the_LOCAL_head_owns_the_same_pair() -> None:
    """The invariant is "the head that runs the job supplies it", and there are two heads.

    `make ray-up` starts the dev head, and it already exports `S3_SECRET` for exactly this reason. With
    the names off the submission body, a local head missing them makes every `make dev-micro` stage job
    die `KeyError` — a break that appears nowhere in the chart and nowhere in CI.
    """
    ray_up = (REPO / "Makefile").read_text().split("ray-up:", 1)[1].split("\n\n", 1)[0]

    for name in ("S3_ENDPOINT", "S3_REGION", "S3_SECRET"):
        assert f"{name}=" in ray_up, f"the local dev head does not export {name}"
