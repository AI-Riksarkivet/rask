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

So the target state for this pair is the same, and it takes TWO steps in a fixed order: the pod gains
them and is OBSERVED carrying them (this gate), and only then does `ray_submit` stop sending them
(`test_the_submitter_owns_no_deployment_fact` below, which is skipped until the pod is deployed with
them). Doing it in one step leaves a window where a job gets neither; doing only the first leaves
exactly the two-owner drift this paragraph exists to prevent.
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


def test_the_submitter_owns_no_deployment_fact() -> None:
    """STEP TWO, and it is a real assertion rather than a note: once the pod carries the pair,
    `ray_submit` must stop sending it, or the two-owner drift above is simply recreated.

    It is `xfail(strict=True)` rather than skipped, so it fails the moment someone deletes the keys
    from the submitter WITHOUT deleting this marker — and it fails loudly today if it unexpectedly
    passes, which would mean step two landed while this file still claimed it had not.
    """
    source = (REPO / "services/medallion/src/medallion/services/ray_submit.py").read_text()

    pytest.xfail("step two: the pod is deployed with the pair first, then the submitter stops sending it")
    assert '"S3_ENDPOINT"' not in source
