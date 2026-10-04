"""The quality gate gains a third answer: a human can say yes.

WHAT WAS WRONG. A failed assertion returned `_QUALITY_BLOCKED = {"status": "DROP"}` and the run died
there. Right for a corrupt blob pointer; wrong for a promotion that is UNUSUAL rather than broken —
a row-count delta outside the expected band, a first promotion of a newly ingested volume. Those were
either auto-promoted (no assertion covered them) or dropped forever (one did). There was no third
answer, and no human could supply one.

WHY A WORKFLOW and not a table plus a cron: the wait is hours-to-days and must survive every pod
restart in between. The maintenance sweep is the instructive contrast — it re-derives its work each
tick and so needs no resumption, while an approval IS a plan worth resuming, because losing it loses
a person's decision.

The three things this must get right are the three the design record names, and each has a test here:
the decision is recorded in LINEAGE (workflow history is retention-bounded, so it is a cache); the
review BAND is resolved by an activity rather than compiled into the body (a threshold read in the
body is a determinism hazard the moment it changes mid-run); and a hold that reaches nobody is an
outage wearing a pause.

THE PRODUCER SIGNS THE ASK AND THE RECORD ([[XC-078]]), so both activities raise while its key holder has
not resolved the key, which it heals in place. The review waits that out for as long as the holder takes,
and a key that never comes back ends it BLOCKED, never a FAILED instance with the hold acknowledged and
nobody told. Proved through the Dapr SDK's own orchestration executor, which applies the retry policy the
body hands it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from dapr.ext.workflow._durabletask import worker
from dapr.ext.workflow._durabletask.internal import helpers
from dapr.ext.workflow._durabletask.internal import protos as pb
from dapr.ext.workflow._durabletask.internal.timer import new_timer_created_event, new_timer_fired_event
from dapr.ext.workflow._durabletask.task import OrchestrationContext
from dapr.ext.workflow.dapr_workflow_context import DaprWorkflowContext
from pydantic import BaseModel

from medallion.workflow import PromotionSpec, promotion_review
from service_kit.governed.signing_key import MAX_REFRESH_SECONDS, SigningKeyUnavailableError


class _Action:
    def __init__(self, kind: str, name: str = "", value: Any = None) -> None:
        self.kind, self.name, self.value, self.raises = kind, name, value, False

    def get_result(self) -> Any:
        return self.value


def _recorded(payload: object) -> dict[str, Any]:
    """An activity input as the RUNTIME records it: JSON, never the model instance.

    Activities declare Pydantic inputs (DWF-ACT-009) and the SDK coerces on the worker side, so a
    workflow body hands `call_activity` a MODEL while history keeps the serialized form. A fake that
    stored the instance would let assertions read attributes the real recorded payload does not have.
    """
    dump = getattr(payload, "model_dump", None)
    if callable(dump):
        dumped = dump(mode="json")
        return dumped if isinstance(dumped, dict) else {}
    return payload if isinstance(payload, dict) else {}


class _Ctx:
    """Replay-faithful double: records what was yielded, answers with scripted results.

    `fails` names an activity that RAISES at its yield point, which is what an exhausted
    `ACTIVITY_RETRY` does to a workflow body; `_drive` throws it into the generator.
    """

    def __init__(self, results: dict[str, Any] | None = None, *, external: Any = None, winner: str = "event", fails: str = "") -> None:
        self.actions: list[str] = []
        self.inputs: dict[str, Any] = {}
        self._results = dict(results or {})
        self.is_replaying = False
        self.instance_id = "promo-test"
        self.current_utc_datetime = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
        self._external = external
        self._winner = winner
        self.fails = fails

    def call_activity(self, activity: Any, *, input: Any = None, retry_policy: Any = None) -> _Action:  # noqa: A002
        name = getattr(activity, "__name__", str(activity))
        self.actions.append(f"call_activity({name})")
        self.inputs[name] = _recorded(input)
        return _Action("activity", name, self._results.get(name))

    def create_timer(self, delay: timedelta) -> _Action:
        self.actions.append(f"create_timer({int(delay.total_seconds())}s)")
        return _Action("timer", "timer")

    def wait_for_external_event(self, name: str) -> _Action:
        self.actions.append(f"wait_for_external_event({name})")
        return _Action("external", name, self._external)

    def pick(self, tasks: list[Any]) -> Any:
        """Stand in for the module-level `wf.when_any` — which is where it really lives.

        `DaprWorkflowContext` has no `when_any`; the first version of this double invented one, so
        the real `wf.when_any` ran against fake tasks and raised inside Dapr's composite-task
        bookkeeping. A double that does not match the API under test proves nothing.
        """
        self.actions.append("when_any")
        won = next((t for t in tasks if t.kind == ("external" if self._winner == "event" else "timer")), tasks[0])
        # `yield wf.when_any(...)` resolves to the WINNING TASK, not to that task's result — which is
        # what lets the body ask `winner is deadline`. Wrapping it so the driver's uniform
        # `.get_result()` hands the body the task itself.
        return _Action("when_any", "when_any", won)


def _spec(**over: Any) -> PromotionSpec:
    base: dict[str, Any] = {
        "token": "tok-1",
        "project": "acme",
        "from_namespace": "acme-silver",
        "from_dataset": "acme-silver$features",
        "to_namespace": "acme-gold",
        "to_dataset": "acme-gold$catalog",
        "operation": "aggregate_gold",
        "author": "analyst",
        "version": 7,
        "approver": "CiQwOGE4Njg0Yi1kYjg4",
        "reasons": ["row_delta_band"],
        "originator": "CiQwOGE4Njg0Yi1kYjg4",
    }
    return PromotionSpec.model_validate(base | over)


def _drive(ctx: _Ctx, spec: PromotionSpec, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import medallion.workflow as workflow_mod

    monkeypatch.setattr(workflow_mod.wf, "when_any", ctx.pick)
    gen = promotion_review(cast(Any, ctx), spec.model_dump())
    sent: Any = None
    throw: BaseException | None = None
    while True:
        try:
            action = gen.throw(throw) if throw is not None else gen.send(sent)
        except StopIteration as stop:
            return stop.value or {}
        throw = None
        if action.kind == "activity" and action.name == ctx.fails:
            throw = RuntimeError(f"catalog refused publishing {spec.to_dataset}: refusing to move 'published' backwards")
            continue
        sent = action.get_result()


class TestAHardFailureStillDropsWithoutAsking:
    def test_a_corrupt_batch_is_not_a_question_for_a_person(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The DROP that is correct today stays correct. A blob pointer that does not resolve is not
        ambiguous, and waking somebody at 3am to confirm it is worse than useless."""
        ctx = _Ctx({"resolve_review_policy": {"verdict": "block", "reasons": ["blob_resolves"]}})

        result = _drive(ctx, _spec(), monkeypatch)

        assert result["status"] == "BLOCKED"
        assert "wait_for_external_event(promotion_decision)" not in ctx.actions, "nobody should be asked about a corrupt batch"
        assert "call_activity(request_approval)" not in ctx.actions
        assert "call_activity(emit_promotion_outcome)" in ctx.actions, "the refusal must still reach the graph"


class TestAnUnusualPromotionAsksAPerson:
    def test_an_APPROVAL_publishes_the_promotion_and_records_who_said_yes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _Ctx(
            {"resolve_review_policy": {"verdict": "review", "reasons": ["row_delta_band"]}, "request_approval": True},
            external={"approved": True, "subject": "CiQwOGE4Njg0Yi1kYjg4"},
        )

        result = _drive(ctx, _spec(), monkeypatch)

        assert result["status"] == "PROMOTED"
        assert result["decided_by"] == "CiQwOGE4Njg0Yi1kYjg4", "the decision is part of the audit, not a side effect"
        assert "call_activity(publish_promotion)" in ctx.actions, "an approved promotion must actually resume the cascade"

    def test_a_REJECTION_records_the_decision_and_promotes_NOTHING(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _Ctx(
            {"resolve_review_policy": {"verdict": "review", "reasons": ["row_delta_band"]}, "request_approval": True},
            external={"approved": False, "subject": "CiQxYjZlMmY1YQ"},
        )

        result = _drive(ctx, _spec(), monkeypatch)

        assert result["status"] == "REJECTED"
        assert result["decided_by"] == "CiQxYjZlMmY1YQ"
        assert "call_activity(publish_promotion)" not in ctx.actions


def test_a_refused_publish_is_recorded_as_PROMOTION_FAILED_not_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    """An approved promotion whose publish is REFUSED must still reach the audit trail.

    `publish_promotion` raises `RegisterError` on any catalog 4xx/5xx, and the trigger is deterministic:
    the approval window defaults to 72 hours, and if a later version was published in it, the catalog
    refuses to move `published` backwards, so every `ACTIVITY_RETRY` attempt fails identically. Unwrapped,
    the exception takes the instance terminal FAILED and `emit_promotion_outcome`, the only writer of the
    durable record, never runs: the validator got their 202 and the decision leaves no record. It is NOT
    swallowed into PROMOTED either: the tag did not move, so `PROMOTION_FAILED` is its own status.
    """
    ctx = _Ctx(
        {"resolve_review_policy": {"verdict": "review", "reasons": ["row_delta_band"]}, "request_approval": {"delivered": True}},
        external={"approved": True, "subject": "CiQwOGE4Njg0Yi1kYjg4"},
        fails="publish_promotion",
    )

    result = _drive(ctx, _spec(), monkeypatch)

    assert result["status"] == "PROMOTION_FAILED"
    assert result["decided_by"] == "CiQwOGE4Njg0Yi1kYjg4", "who approved it survives the failure — that is the part worth keeping"
    assert "call_activity(emit_promotion_outcome)" in ctx.actions, "the durable record is the only place this decision survives"
    outcome = ctx.inputs["emit_promotion_outcome"]["outcome"]
    assert outcome["status"] == "PROMOTION_FAILED"
    assert any("backwards" in reason for reason in outcome["reasons"]), f"the reason the publish was refused must ride along; got {outcome['reasons']}"


#: How long the producer's key may stay unresolved while its holder is healing rather than out: the holder re-reads an
#: unresolved key every 15 s and a resolved one at least every `MAX_REFRESH_SECONDS`, so one refresh period and one re-read.
_HOLDER_HEALS_WITHIN = timedelta(seconds=MAX_REFRESH_SECONDS + 15)

#: What an activity that signs raises while the holder has no key.
_KEY_MISS = SigningKeyUnavailableError("service-medallion-producer cannot sign: the signing key cannot be read from the secret store")


class _Review(BaseModel):
    """Where a review stands when the engine stops: the runtime status it completed with, or that it waits on the person;
    the status it returned; and the outcome lineage took a record of."""

    status: str
    result: str | None = None
    recorded: str | None = None


def _orchestrate(ctx: OrchestrationContext, payload: dict[str, Any]) -> Generator[Any, Any, dict[str, Any]]:
    """`promotion_review` behind the context the runtime hands it, as `WorkflowRuntime.register_workflow` wraps it."""
    return promotion_review(DaprWorkflowContext(ctx), payload)


def _review_while_the_key_is_unresolved(key_resolves_after: timedelta | None) -> _Review:
    """One review run by the Dapr SDK's orchestration executor, with this function playing the engine.

    The engine's part: confirm each task and timer the review schedules, fire each activity-retry timer at its time, and
    answer each activity. The policy asks for review. An activity that signs fails with a key miss on every attempt that
    starts before ``key_resolves_after`` has passed since the review began (every attempt, for None), and after that the
    ask goes out and lineage takes the record. Stops when the review completes, or sets a timer that is no retry: the
    deadline it waits on the person under.
    """
    registry = worker._Registry()
    registry.add_named_orchestrator("promotion_review", _orchestrate)
    executor = worker._OrchestrationExecutor(registry, logging.getLogger(__name__))
    began = now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    history: list[pb.HistoryEvent] = []
    new = [helpers.new_workflow_started_event(now), helpers.new_execution_started_event("promotion_review", "promo-tok-1", _spec().model_dump_json())]
    recorded: str | None = None
    for _turn in range(200):
        actions = executor.execute("promo-tok-1", history, new).actions
        history += new
        events: list[pb.HistoryEvent] = []
        for action in actions:
            if action.HasField("completeWorkflow"):
                done = action.completeWorkflow
                result = json.loads(done.result.value)["status"] if done.HasField("result") else None
                return _Review(status=pb.OrchestrationStatus.Name(done.workflowStatus), result=result, recorded=recorded)
            if action.HasField("createTimer"):
                if not action.createTimer.HasField("activityRetry"):
                    return _Review(status="waiting on the person", recorded=recorded)
                fire_at = action.createTimer.fireAt.ToDatetime().replace(tzinfo=UTC)
                events += [new_timer_created_event(action.id, fire_at), new_timer_fired_event(action.id, fire_at)]
                now = max(now, fire_at)
            elif action.HasField("scheduleTask"):
                task = action.scheduleTask
                events.append(helpers.new_task_scheduled_event(action.id, task.name))
                if task.name == "resolve_review_policy":
                    events.append(helpers.new_task_completed_event(action.id, json.dumps({"verdict": "review", "reasons": ["row_delta_band"]})))
                elif key_resolves_after is None or now - began < key_resolves_after:
                    events.append(helpers.new_task_failed_event(action.id, _KEY_MISS))
                else:
                    if task.name == "emit_promotion_outcome":
                        recorded = json.loads(task.input.value)["outcome"]["status"]
                    events.append(helpers.new_task_completed_event(action.id, json.dumps(True if task.name == "request_approval" else None)))
        new = [helpers.new_workflow_started_event(now), *events]
    raise AssertionError("the review neither completed nor waited on the person within 200 turns")


@pytest.mark.parametrize(
    ("key_resolves_after", "review"),
    [
        pytest.param(_HOLDER_HEALS_WITHIN, _Review(status="waiting on the person"), id="an-ask-the-holder-heals-in-time-for-reaches-the-approver"),
        pytest.param(
            _HOLDER_HEALS_WITHIN + timedelta(seconds=30),
            _Review(status="ORCHESTRATION_STATUS_COMPLETED", result="BLOCKED", recorded="BLOCKED"),
            id="an-ask-that-never-left-is-recorded-blocked",
        ),
        pytest.param(None, _Review(status="ORCHESTRATION_STATUS_COMPLETED", result="BLOCKED"), id="a-record-the-producer-never-signs-still-ends-the-review"),
    ],
)
def test_a_review_waits_out_the_producers_key_and_never_fails_over_it(key_resolves_after: timedelta | None, review: _Review) -> None:
    assert _review_while_the_key_is_unresolved(key_resolves_after) == review


class TestAnUnansweredHoldExpiresRatherThanWaitingForever:
    def test_the_timer_wins_and_nothing_is_promoted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A held promotion nobody answers must end, and end visibly. An unbounded wait is a
        workflow that never completes and an operator who never learns why."""
        ctx = _Ctx(
            {"resolve_review_policy": {"verdict": "review", "reasons": ["row_delta_band"]}, "request_approval": True},
            winner="timer",
        )

        result = _drive(ctx, _spec(approval_hours=48), monkeypatch)

        assert result["status"] == "EXPIRED"
        assert "create_timer(172800s)" in ctx.actions, f"the timer must use the SPEC's window, not a default: {ctx.actions}"
        assert "call_activity(publish_promotion)" not in ctx.actions


class TestTheBodyStaysDeterministic:
    def test_a_clean_promotion_asks_nobody_and_still_promotes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _Ctx({"resolve_review_policy": {"verdict": "promote", "reasons": []}})

        result = _drive(ctx, _spec(), monkeypatch)

        assert result["status"] == "PROMOTED"
        assert result["decided_by"] is None, "nobody decided — the assertions passed"
        assert "wait_for_external_event(promotion_decision)" not in ctx.actions


class TestTheActivitiesActuallyRUN:
    """The first version of this file monkeypatched every activity, so nothing here was ever
    executed — four of them referenced undefined names and would have NameError'd on first use. A
    green suite that never calls the code it covers is a mirage; these call them for real.
    """

    def test_a_clean_run_promotes_without_asking(self) -> None:
        from medallion.workflow import resolve_review_policy

        verdict = resolve_review_policy(cast(Any, None), _spec(reasons=[]))

        assert verdict["verdict"] == "promote"

    def test_a_STRUCTURAL_failure_blocks_even_with_review_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Corruption is not a question. `blob_resolves` failing means the data is wrong, and no
        approval makes it right — so review being ON must not turn it into an ask."""
        monkeypatch.setenv("MEDALLION_QUALITY_REVIEW_ENABLED", "true")
        from medallion.core.config import get_settings
        from medallion.workflow import resolve_review_policy

        get_settings.cache_clear()
        verdict = resolve_review_policy(cast(Any, None), _spec(reasons=["blob_resolves"]))
        get_settings.cache_clear()

        assert verdict["verdict"] == "block"

    def test_review_DISABLED_blocks_rather_than_promoting(self) -> None:
        """The default must be the OLD behaviour, not a weaker one.

        This is the hazard the surface map caught in my first draft: `needs_review = False` did not
        restore the DROP, it removed the only branch that stopped the promotion — so the shipped
        default would have promoted every held batch. "Off by default" read as safe and was the exact
        opposite.
        """
        from medallion.core.config import get_settings
        from medallion.workflow import resolve_review_policy

        get_settings.cache_clear()
        verdict = resolve_review_policy(cast(Any, None), _spec(reasons=["row_delta_band"]))

        assert verdict["verdict"] == "block", "review off must BLOCK — the gate must never fail open"


class TestTheGateNeverFailsOpen:
    def test_an_unreachable_approver_BLOCKS_instead_of_waiting_forever(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _Ctx({"resolve_review_policy": {"verdict": "review", "reasons": ["row_delta_band"]}, "request_approval": False})

        result = _drive(ctx, _spec(approver=""), monkeypatch)

        assert result["status"] == "BLOCKED"
        assert "wait_for_external_event(promotion_decision)" not in ctx.actions, "never park on an event nobody was told about"

    def test_a_decision_naming_NOBODY_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An approval with no subject is not an approval — it is an unattributable promotion."""
        ctx = _Ctx(
            {"resolve_review_policy": {"verdict": "review", "reasons": ["row_delta_band"]}, "request_approval": True},
            external={"approved": True},
        )

        result = _drive(ctx, _spec(), monkeypatch)

        assert result["status"] == "BLOCKED"
        assert "call_activity(publish_promotion)" not in ctx.actions

    def test_the_PRODUCING_service_cannot_approve_itself(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _Ctx(
            {"resolve_review_policy": {"verdict": "review", "reasons": ["row_delta_band"]}, "request_approval": True},
            external={"approved": True, "subject": "service:acme-silver-to-acme-gold"},
        )

        result = _drive(ctx, _spec(), monkeypatch)

        assert result["status"] == "BLOCKED"
        assert "call_activity(publish_promotion)" not in ctx.actions
