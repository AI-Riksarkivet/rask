"""`RayJob` keeps Ray's `metadata`, because that is where a job's identity lives.

The medallion stamps `rask.originator`, `rask.project`, `rask.token`, `rask.stage` and `rask.lane`
into Ray's own `metadata` — deliberately there rather than in `runtime_env.env_vars`, because
`metadata` comes back on `GET /api/jobs/<id>` and is therefore readable from OUTSIDE the job and
AFTER it fails.

This model dropped the field, so every one of those keys died at the service boundary: the
medallion wrote them, Ray returned them, and `RayJob` silently discarded them before any reader saw
one. Measured against the live estate — a job carrying `rask.lane: dummy` in Ray came back through
`/api/ray/jobs` with no `metadata` key at all, so `/compute/jobs/<id>` could not say which lane
submitted it no matter what the page rendered.

DEFAULTS TO `{}` rather than being required. Ray OMITS the key entirely for a job submitted without
metadata, and most jobs are: the frontend's schema made this field required once and a single
metadata-less job threw `Invalid key: Expected "metadata"`, blanking the whole board.
"""

from __future__ import annotations

from ray_kit.schemas import RayJob


def test_metadata_survives_the_model() -> None:
    """The identity the medallion stamped reaches a reader."""
    job = RayJob.model_validate(
        {
            "submission_id": "ray-silver-laneproof-1",
            "status": "FAILED",
            "entrypoint": "python /home/ray/jobs/ray_dummy_job.py",
            "metadata": {"rask.lane": "dummy", "rask.stage": "silver", "rask.project": "acme"},
        }
    )
    assert job.metadata["rask.lane"] == "dummy"
    assert job.metadata["rask.stage"] == "silver"


def test_a_job_with_no_metadata_still_parses() -> None:
    """Ray omits the key entirely for most jobs; that must not be an error."""
    job = RayJob.model_validate({"submission_id": "raysubmit_plain", "status": "SUCCEEDED"})
    assert job.metadata == {}
