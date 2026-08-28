"""The train path submits through `ray_kit.submit`, with its D2 divergence as a PARAMETER.

open_ray-kernel.md move 14. `submit_train_job` carried its own inline copy of the kernel's
POST-then-reattach dance — the second implementation `ray_kit.submit`'s own header calls "the sort
of thing a second implementation gets wrong", written before the kernel existed and never collapsed
onto it. The estate has already measured what a mirrored submission seam costs twice over: the
credential fix that landed in one and not the other, and the work-axis fix that landed in three
files and not the fourth.

THE DIVERGENCE THAT MUST SURVIVE THE COLLAPSE — D2 (docs/RAY-TRAIN.md): the kernel's default
deletes a terminally-failed prior job and resubmits, because a stage trigger is an at-least-once
redelivery and retrying the work is the point. Training inverts that — compute is expensive, so a
failed run is terminal until a HUMAN posts /train with a fresh token, and the handler must get
"already_failed" so it can DROP attributably. Collapsing train onto the kernel WITHOUT carrying
that as an explicit policy would silently turn every failed training run into an automatic,
expensive retry — which is why the D2 tests here are as load-bearing as the single-seam one.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from medallion.core.config import MedallionSettings
from medallion.services import ray_submit


def _settings() -> MedallionSettings:
    return MedallionSettings.model_validate(
        {
            "MEDALLION_COMPUTE_ENABLED": "true",
            "MEDALLION_S3_ENDPOINT": "http://rustfs.invalid:9000",
            "MEDALLION_S3_ACCESS_KEY_ID": "platform-key",
            "MEDALLION_S3_SECRET_ACCESS_KEY": "s",
        }
    )


class _FakeClient:
    """A Jobs API double that scripts the POST outcome and records every verb."""

    def __init__(self, *, post_status: int = 200, existing_status: str | None = None) -> None:
        self.post_status = post_status
        self.existing_status = existing_status
        self.calls: list[tuple[str, str]] = []

    async def post(self, path: str, json: dict[str, Any]) -> Any:  # noqa: A002 — httpx's kwarg name
        self.calls.append(("POST", path))
        return _Resp(self.post_status)

    async def get(self, path: str) -> Any:
        self.calls.append(("GET", path))
        return _Resp(200, {"status": self.existing_status}) if self.existing_status else _Resp(404)

    async def delete(self, path: str) -> Any:
        self.calls.append(("DELETE", path))
        return _Resp(200)


class _Resp:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            request = httpx.Request("POST", "http://ray.invalid/api/jobs/")
            raise httpx.HTTPStatusError("boom", request=request, response=httpx.Response(self.status_code, request=request))


async def _submit(client: _FakeClient, monkeypatch: pytest.MonkeyPatch) -> str:
    async def _client() -> _FakeClient:
        return client

    monkeypatch.setattr(ray_submit, "ray_client", _client)
    return await ray_submit.submit_train_job(
        _settings(),
        model="m1",
        features_json="[]",
        config_json="{}",
        token="tok-9",
        originator="",
        project="",
        registry_uri="s3://models/registry",
        artifact_base="s3://models/artifacts",
    )


def test_train_has_no_inline_copy_of_the_kernel() -> None:
    """The single-seam assertion. The inline dance (POST, then GET-and-branch, then re-raise) is
    exactly what `submit_or_reattach` is; a second copy is where the next one-sided fix lands."""
    source = inspect.getsource(ray_submit.submit_train_job)
    assert "submit_or_reattach" in source, "submit_train_job does not ride ray_kit.submit — it still carries its own copy of the submission dance"
    assert 'post("/api/jobs/"' not in source, "an inline POST remains beside the kernel call — the copy was added to, not collapsed"


@pytest.mark.asyncio
async def test_a_fresh_submission_reports_submitted(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(post_status=200)
    assert await _submit(client, monkeypatch) == "submitted"


@pytest.mark.asyncio
async def test_a_running_prior_job_is_attached_not_raced(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient(post_status=400, existing_status="RUNNING")
    assert await _submit(client, monkeypatch) == "attached"
    assert ("DELETE", f"/api/jobs/{ray_submit.train_submission_id('tok-9')}") not in client.calls


@pytest.mark.asyncio
async def test_D2_a_failed_prior_run_is_reported_never_resubmitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """The kernel's DEFAULT would delete-and-resubmit here. Train must not: compute is expensive,
    and a failed run is terminal until a human posts a fresh token."""
    client = _FakeClient(post_status=400, existing_status="FAILED")
    assert await _submit(client, monkeypatch) == "already_failed"
    verbs = [verb for verb, _ in client.calls]
    assert "DELETE" not in verbs, "the collapse onto the kernel lost D2 — a failed training run was deleted for an automatic, expensive resubmit"
    assert verbs.count("POST") == 1, "a second POST means the failed run was resubmitted — D2 forbids exactly this"
