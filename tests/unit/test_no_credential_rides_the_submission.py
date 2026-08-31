"""Nothing credential-shaped travels in a Ray job's `runtime_env`.

THE REASON IS NOT HYGIENE. The Ray Jobs API echoes `runtime_env` back on `GET /api/jobs/<id>` — an
unauthenticated dashboard, proxied by compute at `/api/ray/*` and published at the edge. One GET
yielded the estate's service credential, which is why `S3_SECRET` was pulled out of the submission
body. `S3_KEY` stayed behind, and that leftover is what this gate removes.

It is also what made the credential unscopeable. Ray merges `runtime_env` OVER the process env, so a
key in the submission BEATS the pod's — the pair therefore had two owners, and repointing the pod
alone gave every job `SignatureDoesNotMatch` (measured on the live estate, twice). With neither half
in the submission, the Ray pod's Secret is the single source and the two cannot disagree.

Ray's own runtime-env auth guidance says the same thing: credentials belong on the cluster, not in
the environment a client submits.

The job's `os.environ` contract is unchanged — `scripts/ray_stage_job.py::_storage_options` still
reads `S3_KEY`/`S3_SECRET` from the environment; they now arrive only from the pod, where
`chart/templates/rayservice.yaml` mounts both from `infra-credentials`.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable

from medallion.services import ray_submit


#: Env names that must never appear in a submitted `runtime_env`. Endpoint, region and bucket are
#: fine — they are topology, not credentials, and a reader learning them gains nothing.
_CREDENTIAL_NAMES = ("S3_KEY", "S3_SECRET", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "LINEAGE_SERVICE_TOKEN", "APP_API_TOKEN")


def _submission_env_literals(func: Callable[..., object]) -> set[str]:
    """The env-var NAMES this submit path writes into its runtime_env dict."""
    return set(re.findall(r'"([A-Z][A-Z0-9_]{2,})":', inspect.getsource(func)))


def test_the_stage_submission_carries_no_credential() -> None:
    named = _submission_env_literals(ray_submit.submit_stage_job)
    assert named, "this gate can no longer see the submission env it is asserting about"
    leaked = sorted(n for n in named if n in _CREDENTIAL_NAMES)
    assert not leaked, f"these ride the submission body and the Jobs API echoes it to any reader: {leaked}"


def test_the_train_submission_carries_no_credential() -> None:
    """The train lane submits to the same cluster through the same API, so it has the same exposure."""
    named = _submission_env_literals(ray_submit.submit_train_job)
    assert named, "this gate can no longer see the submission env it is asserting about"
    leaked = sorted(n for n in named if n in _CREDENTIAL_NAMES)
    assert not leaked, f"these ride the submission body and the Jobs API echoes it to any reader: {leaked}"


def test_the_job_still_reads_them_from_its_environment() -> None:
    """The other half of the contract: removing them from the submission is only safe because the POD
    supplies them. If the job stopped reading the env, this gate would be asserting about nothing."""
    import pathlib

    job = (pathlib.Path(__file__).resolve().parents[2] / "scripts" / "ray_stage_job.py").read_text()
    assert 'os.environ["S3_KEY"]' in job and 'os.environ["S3_SECRET"]' in job, (
        "the stage job no longer reads its credential from the environment, so the pod-supplied pair reaches nothing"
    )
