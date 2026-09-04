"""A submitted stage job carries the LANE that submitted it, in Ray's own ``metadata``.

The job page could not answer "what is this run doing?". `/compute/jobs/<id>` reads Ray's job
record, and that record named the originator, the project, the token and the stage — everything
except the one field that says WHICH DECLARATION produced it. So a person watching a run had no
path back to the entrypoint and params it was running under, and the two halves of a single
thought lived in different screens.

WHY ``metadata`` AND NOT ``runtime_env.env_vars``. The module already draws this distinction for
the originator, and it is the same distinction here: `metadata` comes back on
``GET /api/jobs/<id>``, so it is readable from OUTSIDE the job and AFTER it fails, which is exactly
the read the job page makes. An env var is only visible to the process.

The UNDECLARED case is not a hole to fill with a placeholder. A mover with no ``MEDALLION_LANE``
runs the chart's settings and there IS no lane record to link to, so the key is OMITTED — the same
stance `metadata` already takes for an absent originator, where `""` is not an identity and a
reader must never mistake one for the other.
"""

from __future__ import annotations

from typing import Any

import pytest

from medallion.services import ray_submit


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Intercept the submit body at the ray-kit seam, so nothing needs a live cluster."""
    seen: dict[str, Any] = {}

    async def _capture(_client: Any, submission_id: str, body: dict[str, Any]) -> None:
        seen["submission_id"] = submission_id
        seen["body"] = body

    monkeypatch.setattr(ray_submit.rk, "submit_or_reattach", _capture)
    return seen


def _settings(**over: object) -> Any:
    """The REAL settings object, not a namespace.

    A hand-rolled `SimpleNamespace` was tried first and failed on `s3_endpoint` — the submit path
    reads more of the config than the feature under test cares about, so a fake here tests the fake.
    `MedallionSettings()` constructs from defaults with no env, and `model_copy` overrides only what
    a case actually varies.
    """
    from medallion.core.config import MedallionSettings

    return MedallionSettings().model_copy(update=over)


@pytest.mark.asyncio
async def test_declared_lane_is_stamped_on_the_job(captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """A run under a declaration names it, so the job page can link back to the record."""
    from service_kit.lakehouse.task_registry import TaskRegistration
    from service_kit.lakehouse.transform_specs import TransformSpec

    spec = TransformSpec.model_validate(
        {
            "name": "browserlane",
            "project": "acme",
            "from_id": "acme-bronze$events",
            "to_id": "acme-silver$browserlane",
            "task": "stage-transform",
            "params": {"demo": "1"},
            "code_version": "8bfb93d9",
        }
    )

    async def _resolve(_settings: Any, *, project: str = "") -> TransformSpec:
        return spec

    # The declaration names a TASK; the registry says what running it means. Both reads are stubbed
    # here because this case is about what lands in the job's `metadata`, not about either lookup —
    # each has its own suite.
    async def _resolve_task(_settings: Any, *, task: str, engine: str) -> TaskRegistration:
        return TaskRegistration(task=task, engine=engine, command="python /home/ray/jobs/ray_stage_job.py")

    monkeypatch.setattr(ray_submit, "resolve_transform_async", _resolve)
    monkeypatch.setattr(ray_submit, "resolve_task_async", _resolve_task)

    await ray_submit.submit_stage_job(
        _settings(lane="browserlane"),
        from_uri="s3://acme/bronze",
        to_uri="s3://acme/silver",
        stage="silver",
        token="t0ken",
        project="acme",
    )

    assert captured["body"]["metadata"]["rask.transform"] == "browserlane"


@pytest.mark.asyncio
async def test_an_undeclared_run_omits_the_key_rather_than_sending_a_blank(captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    """No declaration means no record to link to — omit, never `""`.

    A blank would render as a lane named "" on the job page and link nowhere, which reads as a
    broken link rather than as "this run predates the declaration".
    """

    async def _resolve(_settings: Any, *, project: str = "") -> None:
        return None

    monkeypatch.setattr(ray_submit, "resolve_transform_async", _resolve)

    await ray_submit.submit_stage_job(
        _settings(),
        from_uri="s3://acme/bronze",
        to_uri="s3://acme/silver",
        stage="silver",
        token="t0ken",
        project="acme",
    )

    assert "rask.transform" not in captured["body"]["metadata"]
