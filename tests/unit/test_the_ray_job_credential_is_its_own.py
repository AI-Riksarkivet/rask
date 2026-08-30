"""The credential a Ray stage job runs under is configurable INDEPENDENTLY of the mover's.

THE STRUCTURAL BUG THIS CLOSES, found by driving it on the live estate rather than by reading:
`submit_stage_job` exports `S3_KEY` into the job's `runtime_env` from the MOVER's own settings, while
`S3_SECRET` is deliberately NOT exported — the Ray pods hold it themselves, because the Jobs API
echoes `runtime_env` back to any reader of `GET /api/jobs/<id>`.

So the key and the secret come from TWO DIFFERENT PLACES and must be changed together. Repointing the
Ray pod at a scoped credential produced `SignatureDoesNotMatch` on every job, because the pod's new
secret was being paired with the mover's old key. And the obvious repair — setting the mover's key to
the scoped one — broke the MOVER instead: it does its own S3 work (`outbox.stage_event` calls
`create_dir`, which issues a `HeadBucket`), and `HeadBucket` needs an UNCONDITIONED `s3:ListBucket`
that a prefix-scoped policy correctly refuses.

The two callers genuinely need different grants. A mover writes the lineage outbox and probes buckets;
a stage job reads one tier and writes another. Giving them one setting forces one grant, which is why
the scoping could not land.

DEFAULT IS UNCHANGED: with nothing configured the job runs under the mover's key exactly as before.
"""

from __future__ import annotations

import inspect

from medallion.core.config import MedallionSettings


def _settings(**over: object) -> MedallionSettings:
    return MedallionSettings.model_validate({"from_uri": "s3://b/f", "to_uri": "s3://b/t", **over})


def test_the_ray_credential_defaults_to_the_movers() -> None:
    """An estate that has not split them behaves exactly as it always did."""
    s = _settings(MEDALLION_S3_ACCESS_KEY_ID="rustfsadmin")
    assert s.ray_s3_access_key == "rustfsadmin"


def test_the_ray_credential_can_be_set_INDEPENDENTLY() -> None:
    """The whole point: a scoped key for the compute plane without touching the mover's own grant."""
    s = _settings(MEDALLION_S3_ACCESS_KEY_ID="rustfsadmin", MEDALLION_RAY_S3_ACCESS_KEY_ID="rask-ray-compute")
    assert s.ray_s3_access_key == "rask-ray-compute"
    assert s.s3_access_key_id == "rustfsadmin", "splitting the job's key must not move the mover's"


def test_the_submit_path_exports_the_RAY_key_not_the_movers() -> None:
    """A setting nothing reads is not a fix. The job's env must carry the ray key."""
    from medallion.services import ray_submit

    source = inspect.getsource(ray_submit.submit_stage_job)
    assert "ray_s3_access_key" in source, (
        "submit_stage_job still exports the mover's own key, so the job's S3_KEY cannot be paired with the scoped secret the Ray pod holds"
    )
