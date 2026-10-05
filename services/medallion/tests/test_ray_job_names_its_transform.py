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

The UNDECLARED case is not a hole to fill with a placeholder. A stage runner with no ``MEDALLION_LANE``
runs the chart's settings and there IS no lane record to link to, so the key is OMITTED — the same
stance `metadata` already takes for an absent originator, where `""` is not an identity and a
reader must never mistake one for the other.
"""

from __future__ import annotations

from typing import Any

import pytest

from medallion.services import ray_jobs_api, stage_submit
from medallion.services.rayjobs_api_executor import RayJobsApiExecutor
from service_kit.lakehouse.work_order import WorkDestination, WorkIdentity, WorkOrder, WorkSource, WorkStamp


async def _submit_stage_job(settings: Any, **kwargs: Any) -> None:
    """The stage lane's submission: the order it builds, posted through the port."""
    from medallion.services import stage_submit as _stage_submit

    order, registration = await _stage_submit.build_stage_order(settings, **kwargs)
    await _stage_submit.submit_stage_order(order, registration)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Intercept the submit body at the ray-kit seam, so nothing needs a live cluster."""
    seen: dict[str, Any] = {}

    async def _capture(_client: Any, submission_id: str, body: dict[str, Any], **_policy: Any) -> str:
        seen["submission_id"] = submission_id
        seen["body"] = body
        return "submitted"  # the real `submit_or_reattach` answers what happened; its caller maps it

    monkeypatch.setattr(ray_jobs_api, "submit_or_reattach", _capture)
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

    monkeypatch.setattr(stage_submit, "resolve_transform_async", _resolve)
    monkeypatch.setattr(stage_submit, "resolve_task_async", _resolve_task)

    await _submit_stage_job(
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

    monkeypatch.setattr(stage_submit, "resolve_transform_async", _resolve)

    await _submit_stage_job(
        _settings(),
        from_uri="s3://acme/bronze",
        to_uri="s3://acme/silver",
        stage="silver",
        token="t0ken",
        project="acme",
    )

    assert "rask.transform" not in captured["body"]["metadata"]


# --------------------------------------------------------------------------- #
# [[LH-159]] the port's Ray adapter DERIVES the metadata from the order
# --------------------------------------------------------------------------- #
# The five facts are read BACK off the Jobs API: `rask.originator` recovers who a dead job was for, and
# `rask.transform` names the declaration. All five live on the `WorkOrder` (`identity.originator`,
# `identity.project`, `stamp.stage`, `stamp.token`, `stamp.transform`), so `RayJobsApiExecutor` derives
# them rather than being handed them per call, which would let each adapter render the same facts its
# own way. A run that names nobody is undeliverable rather than under-delivered
# (`.claude/skills/rask-notifications`).


def _order(**stamp: str) -> WorkOrder:
    return WorkOrder(
        task="transform",
        source=WorkSource(uri="s3://lake/b", table_id="acme-bronze$events"),
        destination=WorkDestination(uri="s3://lake/s", table_id="acme-silver$events"),
        stamp=WorkStamp(stage="silver", cardinality="1:1", **stamp),
        identity=WorkIdentity(run_id="r1", project="acme", originator="alice"),
        idempotency_key="k1",
    )


def test_every_fact_the_lane_stamps_is_derivable() -> None:
    """The five keys, from the order alone — no argument, no second source of truth."""
    meta = RayJobsApiExecutor().job_metadata(_order(token="tok", transform="browserlane"))

    assert meta["rask.originator"] == "alice", "the person a dead job was for must survive the submission"
    assert meta["rask.project"] == "acme"
    assert meta["rask.stage"] == "silver"
    assert meta["rask.token"] == "tok"
    assert meta["rask.transform"] == "browserlane"


def test_an_absent_fact_is_omitted_rather_than_blanked() -> None:
    """A key carrying `""` reads as a stamped fact that is simply absent — the lane omits, so does this.

    Same rule [[XC-066]] settled for `runtime_env`: absent and empty are different claims, and only one
    of them defers.
    """
    meta = RayJobsApiExecutor().job_metadata(_order())

    assert "rask.token" not in meta, f"a blank token was stamped as a fact: {meta}"
    assert "rask.transform" not in meta, f"a blank transform was stamped as a fact: {meta}"
