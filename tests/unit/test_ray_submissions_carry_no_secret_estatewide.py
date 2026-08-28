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
import pathlib
from typing import Any

import pytest


REPO = pathlib.Path(__file__).resolve().parents[2]

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


# ── seams 2+3: medallion stage + train (services/medallion/services/ray_submit.py) ───────────────


def _medallion_settings() -> Any:
    from medallion.core.config import MedallionSettings

    return MedallionSettings.model_validate(
        {
            "MEDALLION_COMPUTE_ENABLED": "true",
            "MEDALLION_S3_ENDPOINT": "http://rustfs.invalid:9000",
            "MEDALLION_S3_ACCESS_KEY_ID": "platform-key",
            "MEDALLION_S3_SECRET_ACCESS_KEY": MATERIAL["AWS_SECRET_ACCESS_KEY"],
            "MEDALLION_STAGE_LINEAGE_URL": "http://lineage.invalid:8000/events",
            "MEDALLION_TRAIN_LINEAGE_URL": "http://lineage.invalid:8000/events",
        }
    )


@pytest.fixture
def medallion_bodies(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from medallion.services import ray_submit

    seen: dict[str, Any] = {}

    async def _capture(_client: Any, submission_id: str, body: dict[str, Any]) -> None:
        seen["stage"] = body

    class _Response:
        status_code = 200

    class _Client:
        async def post(self, _path: str, json: dict[str, Any]) -> _Response:  # noqa: A002 — httpx's kwarg name
            seen["train"] = json
            return _Response()

    async def _client() -> _Client:
        return _Client()

    async def _resolve(_settings: Any, *, project: str = "") -> None:
        return None

    for name, value in MATERIAL.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(ray_submit.rk, "submit_or_reattach", _capture)
    monkeypatch.setattr(ray_submit, "ray_client", _client)
    monkeypatch.setattr(ray_submit, "resolve_transform_async", _resolve)
    return seen


@pytest.mark.asyncio
async def test_the_medallion_stage_seam_is_clean(medallion_bodies: dict[str, Any]) -> None:
    from medallion.services import ray_submit

    await ray_submit.submit_stage_job(_medallion_settings(), from_uri="s3://a/bronze", to_uri="s3://a/silver", stage="silver", token="t1")
    assert "stage" in medallion_bodies, "the stage submission was never captured — the seam moved and this pin is checking nothing"
    _assert_clean("medallion.submit_stage_job", medallion_bodies["stage"])


@pytest.mark.asyncio
async def test_the_medallion_train_seam_is_clean(medallion_bodies: dict[str, Any]) -> None:
    from medallion.services import ray_submit

    await ray_submit.submit_train_job(
        _medallion_settings(),
        model="m1",
        features_json="[]",
        config_json="{}",
        token="t1",
        originator="",
        project="",
        registry_uri="s3://models/registry",
        artifact_base="s3://models/artifacts",
    )
    assert "train" in medallion_bodies, "the train submission was never captured — the seam moved and this pin is checking nothing"
    _assert_clean("medallion.submit_train_job", medallion_bodies["train"])


# ── the enumeration guard: a fourth seam cannot land outside this file ───────────────────────────


#: Files KNOWN to build a Ray Jobs submission body, each represented by a test above.
_REPRESENTED = {
    "services/medallion/src/medallion/services/ray_submit.py",
    # ray-kit is the shared kernel: it SHIPS bodies callers build, and builds none itself —
    # `submit_or_reattach(client, sub_id, body)` takes the body as an argument. The callers are the
    # seams. If it ever grows an env-building helper, it joins the list and gets a test.
    "packages/ray-kit/src/ray_kit/submit.py",
}


def _submission_seams_in_tree() -> set[str]:
    """Every non-test file under the fleet's planes that submits to the Ray Jobs surface."""
    seams: set[str] = set()
    for base in (REPO / "services", REPO / "packages"):
        for path in base.rglob("*.py"):
            if "tests" in path.parts or ".venv" in path.parts:
                continue
            text = path.read_text()
            # CALL sites, not imports, re-exports or comments: a seam is a file that actually fires
            # a submission (`.submit_job(` / `submit_or_reattach(`). `compute` holds a full
            # `JobSubmissionClient` and never submits (read-only introspection — verified), so a
            # client import alone must not count or the guard cries wolf on every reader.
            if "submit_job(" in text or "submit_or_reattach(" in text:
                seams.add(str(path.relative_to(REPO)))
    return seams


def test_every_submission_seam_is_represented_here() -> None:
    unrepresented = _submission_seams_in_tree() - _REPRESENTED
    assert not unrepresented, (
        "these files submit to the Ray Jobs surface and are not covered by this cross-plane pin — "
        "add a test feeding them MATERIAL, or the next seam repeats the twice-fixed P0:\n  " + "\n  ".join(sorted(unrepresented))
    )


def test_the_enumeration_guard_sees_the_known_seams() -> None:
    """A guard on the guard: if the grep stops matching the three known files, it is matching nothing."""
    assert _submission_seams_in_tree() >= _REPRESENTED, "the seam grep no longer finds the known submitters — the enumeration is checking nothing"
