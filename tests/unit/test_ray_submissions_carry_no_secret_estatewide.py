"""No Ray Jobs submission built ANYWHERE in the estate may carry credential material.

open_ray-kernel.md move 2 — the cross-plane pin that did not exist, and the reason it is the plan's
highest-value gate: the estate has TWO mechanisms for keeping secrets out of the echoed
`runtime_env` (medallion omits the secret and sources it from the pod; ratch filters the wildcard
forward fail-closed), each pinned only by its own plane's test. That split is how the P0 was fixed
twice — `91d2e50d` closed the medallion, and the identical defect sat live in ratch for days until
`9205f783` — and how ratch's first filter then shipped with a hole the medallion mechanism cannot
have (`MEDIA_API_KEY`, closed `56719c76`). Two mechanisms with two local tests drift; one test that
feeds every seam the SAME adversarial material cannot.

WHY THE JOBS API IS THE THREAT MODEL, restated once so the next seam's author need not rediscover
it: `GET /api/jobs/<id>` returns the submitted `runtime_env` verbatim, the Ray dashboard is
unauthenticated, `services/compute` proxies it at `/api/ray/*`, and the gateway publishes `/api/ray`
at the edge. Anything in a submission body is readable by any caller.

ASSERTED ON VALUES, not key names, exactly as both plane-local tests do: a rename
(`S3_SECRET` -> `AWS_SECRET`) dodges a key check and leaks identically, so every seam's whole
serialized body is searched for the secret material itself.

ENUMERATION IS GUARDED: `_submission_seams_in_tree` greps for the two ways a submission body is
built (`runtime_env` construction near a Jobs POST / `JobSubmissionClient`), so a FOURTH seam added
anywhere under `services/` or `packages/` fails this file until it is represented below — the gate
that only checks the seams someone remembered is the plane-local regime this replaces.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest


async def _submit_stage_job(settings: Any, **kwargs: Any) -> None:
    """The stage lane's submission: the order it builds, posted through the port."""
    from medallion.services import stage_submit as _stage_submit

    order, registration = await _stage_submit.build_stage_order(settings, **kwargs)
    await _stage_submit.submit_stage_order(order, registration)


#: One distinctive value per credential CLASS the estate holds. Every seam is fed all of them.
MATERIAL = {
    "AWS_SECRET_ACCESS_KEY": "estatewide-pin-aws-secret",
    "AWS_SESSION_TOKEN": "estatewide-pin-session-token",
    "MEDIA_S3_SECRET_ACCESS_KEY": "estatewide-pin-media-secret",
    "MEDIA_API_KEY": "estatewide-pin-api-key",
    "APP_API_TOKEN": "estatewide-pin-app-token",
    # In the namespace ray-kit's deleted `lineage_env()` would have forwarded wholesale — it falls
    # back to APP_API_TOKEN, so a "config" env forward that includes it ships the estate credential.
    "RASK_LINEAGE_APP_TOKEN": "estatewide-pin-lineage-token",
}


def _assert_clean(seam: str, body: object) -> None:
    serialized = json.dumps(body, default=str)
    for name, value in MATERIAL.items():
        assert value not in serialized, f"{seam}: the value of {name} rides the submission body, which the Jobs API echoes to any reader"


# ── seam 2: the medallion stage lane (training is an order through the port seam below, CP-044) ───


def _medallion_settings() -> Any:
    from medallion.core.config import MedallionSettings

    return MedallionSettings.model_validate(
        {
            "MEDALLION_COMPUTE_ENABLED": "true",
            "MEDALLION_S3_ENDPOINT": "http://minio.invalid:9000",
            "MEDALLION_S3_ACCESS_KEY_ID": "platform-key",
            "MEDALLION_S3_SECRET_ACCESS_KEY": MATERIAL["AWS_SECRET_ACCESS_KEY"],
            "MEDALLION_TRAIN_LINEAGE_URL": "http://lineage.invalid:8000/events",
        }
    )


@pytest.fixture
def medallion_bodies(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from medallion.services import ray_jobs_api, ray_submit, stage_submit

    seen: dict[str, Any] = {}

    async def _capture(_client: Any, submission_id: str, body: dict[str, Any], **_policy: Any) -> str:
        # `**_policy` swallows the kernel's `on_terminal_failure` keyword: the pin captures BODIES,
        # and pinning the policy signature here would make every kernel-contract change a pin edit.
        seen["stage"] = body
        return "submitted"

    class _Response:
        status_code = 200

    class _Client:
        # Present only so `ray_client()` returns something; with the kernel monkeypatched above,
        # nothing posts through it — the body arrives via `_capture`.
        async def post(self, _path: str, json: dict[str, Any]) -> _Response:  # noqa: A002 — httpx's kwarg name
            raise AssertionError("a submission bypassed the kernel — some seam still carries an inline POST")

    async def _client() -> _Client:
        return _Client()

    async def _resolve(_settings: Any, *, project: str = "") -> None:
        return None

    for name, value in MATERIAL.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(ray_jobs_api, "submit_or_reattach", _capture)
    monkeypatch.setattr(ray_submit, "ray_client", _client)
    monkeypatch.setattr(stage_submit, "resolve_transform_async", _resolve)
    return seen


@pytest.mark.asyncio
async def test_the_medallion_stage_seam_is_clean(medallion_bodies: dict[str, Any]) -> None:

    await _submit_stage_job(_medallion_settings(), from_uri="s3://a/bronze", to_uri="s3://a/silver", stage="silver", token="t1")
    assert "stage" in medallion_bodies, "the stage submission was never captured — the seam moved and this pin is checking nothing"
    _assert_clean("medallion.submit_stage_order", medallion_bodies["stage"])


# ── the enumeration guard: a fourth seam cannot land outside this file ───────────────────────────


@pytest.mark.asyncio
async def test_the_executor_port_seam_is_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """The `Executor` adapter builds its own body, so it is fed the same material as every other seam.

    [[LH-158]]. This one is expected to be clean STRUCTURALLY rather than by filtering — a `WorkOrder`
    has no field a credential can occupy, because `credential_ref` names one and never carries it. The
    test exists anyway: the twice-fixed P0 this file records was not a missing filter but a seam nobody
    fed adversarial material to, and "it cannot leak by construction" is the same shape of claim the
    plane-local tests made before they drifted.
    """
    from medallion.services.rayjobs_api_executor import RayJobsApiExecutor
    from service_kit.lakehouse.executor import TaskRegistration
    from service_kit.lakehouse.work_order import WorkDestination, WorkIdentity, WorkOrder, WorkSource, WorkStamp, derive_idempotency_key

    for name, value in MATERIAL.items():
        monkeypatch.setenv(name, value)

    captured: dict[str, object] = {}

    async def _capture(_client: object, _sub_id: str, body: dict[str, object], **_kw: object) -> str:
        captured["body"] = body
        return "submitted"

    from medallion.services import ray_jobs_api

    monkeypatch.setattr(ray_jobs_api, "submit_or_reattach", _capture)

    order = WorkOrder(
        task="silver",
        source=WorkSource(uri="s3://a/bronze", table_id="bronze$events"),
        destination=WorkDestination(uri="s3://a/silver", table_id="silver$events"),
        stamp=WorkStamp(stage="silver", cardinality="1:1", lineage_document="{}"),
        identity=WorkIdentity(run_id="r1", project="p", code_version="build-1"),
        idempotency_key=derive_idempotency_key(stage="silver", token="t1", from_uri="s3://a/bronze", to_uri="s3://a/silver", code_version="build-1"),
    )
    # A real client, never used: `submit_or_reattach` is patched above, so this only satisfies the
    # adapter's injection seam. `object()` would type-error, and typing the seam loosely to accept it
    # would weaken the production signature to make a test convenient.
    async with httpx.AsyncClient(base_url="http://ray.invalid") as client:
        await RayJobsApiExecutor(client=client).submit(order, TaskRegistration(task="silver", engine="ray", command="python job.py"))

    assert "body" in captured, "the submission was never captured — the seam moved and this pin is checking nothing"
    _assert_clean("rayjobs_api_executor.submit", captured["body"])
