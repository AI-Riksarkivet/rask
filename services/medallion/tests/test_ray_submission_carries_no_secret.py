"""No secret may ride a Ray Jobs submission body — the Jobs API echoes it to any reader.

docs/DECISIONS.md "The Python estate audit" (P0, E1) — "The S3 secret key and the estate's APP_API_TOKEN are shipped into the
Ray Jobs `runtime_env`, which the Jobs API echoes back to any reader". Confirmed live at HEAD by the
independent re-audit: `GET /api/jobs/<id>` returns the submitted `runtime_env` verbatim, the Ray
dashboard is unauthenticated, `services/compute` proxies it at `/api/ray/*`, and the gateway
publishes `/api/ray` at the edge — one GET of a job record yielded the estate's service credential.

THE FIX MOVES THE TRANSPORT, NOT THE SOURCE (owner decision 2026-08-28, the rask-dapr rubric's
sidecar-less case). The submitting service keeps fetching secrets from the Dapr secret store — that
half was always right. What changes is how they reach the job: Ray pods carry no daprd, so the pods
themselves now hold `S3_SECRET` and `LINEAGE_SERVICE_TOKEN` via a `secretKeyRef` onto the SAME chart
Secrets the estate already syncs through ExternalSecrets from OpenBao — and the submission body
carries neither. Ray merges `runtime_env.env_vars` OVER the worker's process env, so the job's
`os.environ` contract is unchanged and the job scripts need no edit.

ASSERTED ON THE VALUES AS WELL AS THE KEYS: a rename (`S3_SECRET` → `AWS_SECRET`) would dodge a
key-only check while leaking identically, so the whole serialized body is searched for the secret
material itself.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from medallion.core.config import MedallionSettings
from medallion.services import ray_submit


SECRET = "the-platform-s3-secret"
APP_TOKEN = "the-estate-service-credential"


def _settings() -> MedallionSettings:
    return MedallionSettings.model_validate(
        {
            "MEDALLION_COMPUTE_ENABLED": "true",
            "MEDALLION_S3_ENDPOINT": "http://rustfs.invalid:9000",
            "MEDALLION_S3_ACCESS_KEY_ID": "platform-key",
            "MEDALLION_S3_SECRET_ACCESS_KEY": SECRET,
            # The lineage door ON, so the token branch is exercised rather than skipped.
            "MEDALLION_STAGE_LINEAGE_URL": "http://lineage.invalid:8000/events",
            "MEDALLION_TRAIN_LINEAGE_URL": "http://lineage.invalid:8000/events",
        }
    )


@pytest.fixture
def stage_body(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def _capture(_client: Any, submission_id: str, body: dict[str, Any]) -> None:
        seen.update(body)

    async def _resolve(_settings: Any, *, project: str = "") -> None:
        return None

    monkeypatch.setenv("APP_API_TOKEN", APP_TOKEN)
    monkeypatch.setattr(ray_submit.rk, "submit_or_reattach", _capture)
    monkeypatch.setattr(ray_submit, "resolve_transform_async", _resolve)
    return seen


@pytest.fixture
def train_body(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    class _Response:
        status_code = 200

    class _Client:
        async def post(self, _path: str, json: dict[str, Any]) -> _Response:  # noqa: A002 - httpx's own kwarg name
            seen.update(json)
            return _Response()

    async def _client() -> _Client:
        return _Client()

    monkeypatch.setenv("APP_API_TOKEN", APP_TOKEN)
    monkeypatch.setattr(ray_submit, "ray_client", _client)
    return seen


@pytest.mark.asyncio
async def test_the_stage_submission_carries_no_secret_material(stage_body: dict[str, Any]) -> None:
    await ray_submit.submit_stage_job(_settings(), from_uri="s3://acme/bronze", to_uri="s3://acme/silver", stage="silver", token="tok-1")
    env = stage_body["body"]["runtime_env"]["env_vars"] if "body" in stage_body else stage_body["runtime_env"]["env_vars"]
    assert "S3_SECRET" not in env, "the S3 secret still rides runtime_env, which the Jobs API echoes to any reader"
    assert "LINEAGE_SERVICE_TOKEN" not in env, "the estate's service credential still rides runtime_env"

    serialized = json.dumps(stage_body)
    assert SECRET not in serialized, "the secret VALUE is in the submission body under some other key"
    assert APP_TOKEN not in serialized, "the app token VALUE is in the submission body under some other key"


@pytest.mark.asyncio
async def test_the_train_submission_carries_no_secret_material(train_body: dict[str, Any]) -> None:
    await ray_submit.submit_train_job(
        _settings(),
        model="m1",
        features_json="[]",
        config_json="{}",
        token="tok-1",
        originator="",
        project="",
        registry_uri="s3://models/registry",
        artifact_base="s3://models/artifacts",
    )
    env = train_body["runtime_env"]["env_vars"]
    assert "S3_SECRET" not in env
    assert "LINEAGE_SERVICE_TOKEN" not in env

    serialized = json.dumps(train_body)
    assert SECRET not in serialized
    assert APP_TOKEN not in serialized


@pytest.mark.asyncio
async def test_the_NON_secret_platform_contract_still_rides_the_submission(stage_body: dict[str, Any]) -> None:
    """The failure mode that would hide the fix: stripping everything also passes above.

    Endpoint and region are configuration, not secrets — the job needs them per submission (they vary
    by warehouse) — and `LINEAGE_SERVICE_ID` is an identity NAME, not a credential.

    `S3_KEY` USED TO BE IN THIS LIST, on the same reasoning: an access key id is not secret. That
    reasoning still holds, and it is not why the key left. It left because Ray merges
    `runtime_env.env_vars` OVER the worker's process env, so a key sent here BEATS the one the pod
    mounts — which gave the credential two owners. Repointing the Ray pod at a scoped RustFS user
    produced `SignatureDoesNotMatch` on every job (its new secret paired with the submission's old
    key), and repointing the mover instead took the MOVER down: it does its own S3 work
    (`outbox.stage_event` → `create_dir` → HeadBucket), which needs an unconditioned `s3:ListBucket`
    that a prefix-scoped policy correctly refuses. Both measured on the live estate 2026-08-30.

    So the pod now mounts BOTH halves from `infra-credentials` (`chart/templates/rayservice.yaml`)
    and the submission carries neither. The job's `os.environ` contract is unchanged — it still reads
    `S3_KEY`/`S3_SECRET` from the environment, which is asserted in
    `tests/unit/test_no_credential_rides_the_submission.py` so that removing them here can never
    quietly leave the job with nothing to read."""
    await ray_submit.submit_stage_job(_settings(), from_uri="s3://acme/bronze", to_uri="s3://acme/silver", stage="silver", token="tok-1")
    env = stage_body["body"]["runtime_env"]["env_vars"] if "body" in stage_body else stage_body["runtime_env"]["env_vars"]
    for key in ("S3_ENDPOINT", "S3_REGION", "LINEAGE_URL", "LINEAGE_SERVICE_ID"):
        assert key in env, f"`{key}` was stripped with the secrets — the job cannot run without its non-secret config"
    assert "S3_KEY" not in env, "the key must come from the pod alone, or runtime_env overrides it and the pair has two owners"
