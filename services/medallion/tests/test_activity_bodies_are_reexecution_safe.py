"""Three activity-body defects that only show up when an activity RUNS TWICE, or when it fails.

Dapr guarantees at-least-once activity execution: a worker that crashes after doing the work but
before recording the result re-executes the whole body on recovery. Everything an activity does must
therefore be safe to do twice, and everything it swallows must be findable afterwards.

1. DWF-ACT-002 -- `request_approval` minted a fresh `event_id` per execution, so a re-executed
   activity double-notified the approver. `event_id` is documented as "the client-side dedupe key",
   and a `uuid4()` default makes it a fresh key every time, which is the one value that cannot dedupe.
2. `emit_promotion_outcome`'s failure log named NOTHING -- no token, no dataset, no decider -- while
   this activity's own docstring calls lineage "the durable record". A dropped publish emptied the
   record and left a log line nobody could tie to a promotion.
3. `report_stage_outcome` enriched a failure with Ray's cause without checking the submission id was
   non-empty, so a permanently-failed SUBMIT sent the reporter at Ray's job LIST endpoint.
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from typing import Any, cast

import pytest
from medallion.workflow import PromotionOutcome, PromotionReport, PromotionSpec, StageJobOutcome, StageJobSpec, StageReport, request_approval


def _spec(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "token": "tok-1",
        "project": "acme",
        "from_namespace": "acme-silver",
        "from_dataset": "acme-silver$features",
        "to_namespace": "acme-gold",
        "to_dataset": "acme-gold$catalog",
        "pub_topic": "",
        "reasons": ["row_delta_band"],
        "approver": "CiQwOGE4Njg0Yi1kYjg4",
        "originator": "CiQwOGE4Njg0Yi1kYjg4",
        "approval_hours": 72,
    }
    return PromotionSpec.model_validate(base | over).model_dump()


def _published(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Capture the control events `request_approval` publishes, without a sidecar.

    Patched at `medallion.workflow`'s own import site: the activity imports `publish_event` LOCALLY
    inside the function body, so patching the defining module binds nothing the call will look at --
    the same local-import trap `test_fanin_return_ceiling` records for ingest.
    """
    seen: list[dict[str, Any]] = []

    async def _publish(_client: Any, *, timeout_seconds: float, data: str = "", **_kw: Any) -> None:
        seen.append(json.loads(data))

    monkeypatch.setattr("service_kit.dapr_publish.publish_event", _publish)

    class _Dapr:
        async def __aenter__(self) -> _Dapr:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

    monkeypatch.setattr("dapr.aio.clients.DaprClient", lambda *a, **k: _Dapr())
    return seen


def test_a_RE_EXECUTED_request_approval_carries_the_SAME_dedupe_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE WEDGE. Dapr re-executes an activity whose result was not recorded; a fresh uuid4 per
    execution is precisely the key that cannot dedupe, so the approver is asked twice."""
    seen = _published(monkeypatch)

    request_approval(cast("Any", None), PromotionSpec.model_validate(_spec()))
    request_approval(cast("Any", None), PromotionSpec.model_validate(_spec()))

    assert len(seen) == 2, f"the fixture did not capture both publishes: {seen}"
    assert seen[0]["event_id"] == seen[1]["event_id"], f"a re-executed activity minted a fresh dedupe key: {seen[0]['event_id']} vs {seen[1]['event_id']}"


def test_the_dedupe_key_is_DERIVED_from_the_promotion_not_shared_across_them(monkeypatch: pytest.MonkeyPatch) -> None:
    """A constant would dedupe every promotion into the first one -- worse than the bug."""
    seen = _published(monkeypatch)

    request_approval(cast("Any", None), PromotionSpec.model_validate(_spec(token="tok-1")))
    request_approval(cast("Any", None), PromotionSpec.model_validate(_spec(token="tok-2")))

    assert seen[0]["event_id"] != seen[1]["event_id"], "two different promotions collapsed onto one dedupe key"


def test_a_lost_promotion_audit_NAMES_the_promotion(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """`emit_promotion_outcome` is the only writer of the durable record. When its publish fails, the
    log line is all that is left -- and it named nothing, against three sibling `best_effort` calls in
    the same file that all pass identifying kwargs."""
    from medallion import workflow as workflow_mod

    def _boom() -> None:
        raise RuntimeError("sidecar refused")

    monkeypatch.setattr(workflow_mod, "_run_async", lambda _coro: _boom())
    monkeypatch.setattr(workflow_mod, "record_promotion_outcome", lambda _s: None)

    payload = PromotionReport(spec=PromotionSpec.model_validate(_spec()), outcome=PromotionOutcome(status="PROMOTED", decided_by="CiQwOGE4Njg0Yi1kYjg4"))
    with caplog.at_level(logging.ERROR):
        workflow_mod.emit_promotion_outcome(cast("Any", None), payload)

    record = next((r for r in caplog.records if "best_effort_emit_failed_promotion_outcome" in r.message), None)
    assert record is not None, f"the lost audit was not reported at all; saw {[r.message for r in caplog.records]}"
    named = {getattr(record, "token", None), getattr(record, "dataset", None), getattr(record, "status", None), getattr(record, "decided_by", None)}
    assert "tok-1" in named, f"the lost-audit line does not name the promotion: {record.__dict__}"
    assert "acme-gold$catalog" in named, f"the lost-audit line does not name the dataset: {record.__dict__}"


def test_an_EMPTY_submission_id_never_reaches_rays_job_list_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """A permanently failed SUBMIT reports with an empty submission id.

    `_read_stage_failure("")` builds the dashboard URL without one, which is Ray's job LIST endpoint
    -- so the "cause" returned is whatever job happens to be first, attributed to a run that never
    started. Worse than no cause: a plausible, wrong one, pinned into lineage as this stage's reason.
    """
    from medallion import workflow as workflow_mod

    asked: list[str] = []

    def _read(submission_id: str) -> None:
        asked.append(submission_id)
        return

    monkeypatch.setattr(workflow_mod, "_read_stage_failure", _read)
    monkeypatch.setattr(workflow_mod, "record_stage_outcome", lambda *a, **k: None)
    monkeypatch.setattr(workflow_mod, "_publish_stage_fail_event", lambda *a, **k: None, raising=False)

    payload = StageReport(
        spec=StageJobSpec.model_validate(
            {
                "from_uri": "s3://wh/p-bronze/pages.lance",
                "to_uri": "s3://wh/p-silver/pages.lance",
                "stage": "silver",
                "token": "tok-1",
                "trigger": {"token": "tok-1"},
            }
        ),
        outcome=StageJobOutcome.model_validate({"submission_id": "", "status": None, "polls": 0, "verdict": "failed"}),
    )
    with suppress(Exception):
        # The SUBJECT is whether Ray is asked at all. What the reporter does afterwards reaches a bus
        # and is covered by test_stage_workflow; suppressed rather than stubbed so this test cannot
        # start silently asserting something about a path it does not model.
        workflow_mod.report_stage_outcome(cast("Any", None), payload)

    assert asked == [], f"an empty submission id was handed to the Ray failure reader: {asked}"
