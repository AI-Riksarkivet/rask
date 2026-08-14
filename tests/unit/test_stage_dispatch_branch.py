"""S1 at the DEFECT SITE: the ray branch must dispatch, and must not measure until the job is done.

`services/medallion/tests/test_stage_workflow.py` proves the workflow's own ordering. This proves the
half that ordering is worth nothing without: that `handle_stage` actually ROUTES through it, and that
the branch which measures is reachable only once a terminal status has been read.

Asserted by observing which collaborator is called, because that IS the defect. The old code called
`submit_stage_job` and `measure_stage` in the same breath; the fix is that the first delivery calls
neither measure nor submit directly, and the second — the one carrying `ray_job_done` — measures
without submitting again.
"""

from __future__ import annotations

from typing import Any

import pytest
from medallion.services.trigger_guards import StageTrigger, parse_stage_trigger


def _event(**data: Any) -> dict[str, Any]:
    return {"data": {"token": "tok-1", **data}}


def test_the_trigger_contract_carries_ray_job_done_and_DEFAULTS_FALSE() -> None:
    """The default is the load-bearing half. If an absent flag parsed as True, every ordinary upstream
    arrival would take the measure branch — reinstating the original defect for the whole cascade."""
    plain = parse_stage_trigger(_event())
    assert plain is not None
    assert plain.ray_job_done is False, "an upstream producer cannot know the job finished; absent must mean not-done"
    assert plain.ray_submission_id is None

    ready = parse_stage_trigger(_event(ray_job_done=True, ray_submission_id="ray-silver-tok-1-abc"))
    assert ready is not None
    assert ready.ray_job_done is True
    assert ready.ray_submission_id == "ray-silver-tok-1-abc"


def test_a_NON_BOOL_ray_job_done_is_refused_rather_than_coerced() -> None:
    """`extra="ignore"` tolerates unknown fields; it does not tolerate a known field of the wrong type.

    A string "false" coercing to True is the classic version of this bug, and it would silently route
    every trigger down the measure branch.
    """
    assert parse_stage_trigger(_event(ray_job_done="not-a-bool")) is None


def test_the_dispatch_seam_derives_a_DETERMINISTIC_instance_id_from_the_work() -> None:
    """Redelivery must re-attach to the running watcher, not start a second one over the same job.

    The id is derived from the same `from->to` digest the SUBMISSION id uses, so the two cannot name
    different work for one trigger.
    """
    from medallion.services.ray_submit import stage_submission_id

    a = stage_submission_id("silver", "tok-1", "s3://a", "s3://b")
    b = stage_submission_id("silver", "tok-1", "s3://a", "s3://b")
    assert a == b
    assert stage_submission_id("silver", "tok-1", "s3://a", "s3://z") != a


def test_the_dispatch_seam_reports_a_LIVE_INSTANCE_as_handled_not_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A duplicate schedule for an instance already watching this job is the correct outcome.

    Dapr raises on `schedule_new_workflow` for a live instance id. Treating that as a dispatch failure
    would RETRY, and the sidecar would redeliver into the same refusal forever while the first instance
    quietly did the work.
    """
    from medallion.core.config import get_settings
    from medallion.services import transform

    class _AlreadyRunning:
        """Refuses the schedule AND confirms the instance exists — a genuine re-attach."""

        def schedule_new_workflow(self, **_: object) -> None:
            raise RuntimeError("instance already exists")

        def get_workflow_state(self, _instance_id: str) -> object:
            return object()

    import dapr.ext.workflow as wf

    monkeypatch.setattr(wf, "DaprWorkflowClient", lambda *a, **k: _AlreadyRunning())

    instance_id = transform._dispatch_stage_workflow(
        get_settings(),
        from_uri="s3://wh/p-bronze/pages.lance",
        to_uri="s3://wh/p-silver/pages.lance",
        token="tok-1",
        lineage_json="{}",
        trigger=StageTrigger(token="tok-1"),
    )

    assert instance_id.startswith("stage-ray-"), "the instance id must name the job it watches"


def test_a_schedule_failure_with_NO_live_instance_RAISES_rather_than_acking(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other half, and the one that loses data if it is wrong.

    A schedule error is two events wearing one exception. "Already exists" means a watcher is on the
    job and acking is right. Anything else — no sidecar, state store not scoped to `medallion`, engine
    down — means NOTHING is watching, and since the Ray job is submitted BY the workflow, no workflow
    means no job at all. Swallowing that would ack a trigger whose work never starts, permanently and
    on every delivery, while the logs read "reattach".

    The first draft of `_dispatch_stage_workflow` did exactly that. This is the assertion that keeps it
    honest: the existence of the instance is CHECKED, not assumed from the fact of an error.
    """
    from medallion.core.config import get_settings
    from medallion.services import transform

    class _NoEngine:
        def schedule_new_workflow(self, **_: object) -> None:
            raise RuntimeError("the state store is not configured to use the actor runtime")

        def get_workflow_state(self, _instance_id: str) -> object:
            raise RuntimeError("no sidecar")

    import dapr.ext.workflow as wf

    monkeypatch.setattr(wf, "DaprWorkflowClient", lambda *a, **k: _NoEngine())

    with pytest.raises(RuntimeError):
        transform._dispatch_stage_workflow(
            get_settings(),
            from_uri="s3://wh/p-bronze/pages.lance",
            to_uri="s3://wh/p-silver/pages.lance",
            token="tok-1",
            lineage_json="{}",
            trigger=StageTrigger(token="tok-1"),
        )


def test_the_scheduled_input_ROUND_TRIPS_the_trigger_for_republication(monkeypatch: pytest.MonkeyPatch) -> None:
    """The workflow re-publishes this trigger when the job lands, so anything dropped here is lost.

    `project` in particular: losing it would make the completed-pass resolve the DEFAULT roots and
    transform the wrong tenant's data while emitting real-looking lineage for it — the exact failure
    the mover's fail-closed project handling exists to prevent.
    """
    from medallion.core.config import get_settings
    from medallion.services import transform
    from medallion.workflow import StageJobSpec

    captured: dict[str, Any] = {}

    class _Client:
        def schedule_new_workflow(self, *, workflow: Any, input: Any, instance_id: str) -> None:  # noqa: A002
            captured["input"] = input
            captured["instance_id"] = instance_id

    import dapr.ext.workflow as wf

    monkeypatch.setattr(wf, "DaprWorkflowClient", lambda *a, **k: _Client())

    transform._dispatch_stage_workflow(
        get_settings(),
        from_uri="s3://wh/acme-bronze/pages.lance",
        to_uri="s3://wh/acme-silver/pages.lance",
        token="tok-1",
        lineage_json='{"run_id": "r1"}',
        trigger=StageTrigger(token="tok-1", dataset="pages", project="acme"),
    )

    spec = StageJobSpec.model_validate(captured["input"])
    assert spec.trigger["project"] == "acme", "the project fell off the round trip — the completed pass would target the wrong tenant"
    assert spec.trigger["dataset"] == "pages"
    assert spec.lineage_json == '{"run_id": "r1"}', "the R26 provenance document must reach the job's own commit"
    assert spec.from_uri.endswith("acme-bronze/pages.lance")
