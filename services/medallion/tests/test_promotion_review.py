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
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from medallion.workflow import PromotionSpec, promotion_review


class _Action:
    def __init__(self, kind: str, name: str = "", value: Any = None) -> None:
        self.kind, self.name, self.value, self.raises = kind, name, value, False

    def get_result(self) -> Any:
        return self.value


class _Ctx:
    """Replay-faithful double: records what was yielded, answers with scripted results."""

    def __init__(self, results: dict[str, Any] | None = None, *, external: Any = None, winner: str = "event") -> None:
        self.actions: list[str] = []
        self._results = dict(results or {})
        self.is_replaying = False
        self.instance_id = "promo-test"
        self.current_utc_datetime = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
        self._external = external
        self._winner = winner

    def call_activity(self, activity: Any, *, input: Any = None, retry_policy: Any = None) -> _Action:  # noqa: A002
        name = getattr(activity, "__name__", str(activity))
        self.actions.append(f"call_activity({name})")
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
        "pub_topic": "medallion.gold",
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
    while True:
        try:
            action = gen.send(sent)
        except StopIteration as stop:
            return stop.value or {}
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
    def test_the_request_is_SENT_before_the_wait(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Order is the whole property: parking on an event nobody was told about is an outage
        wearing a pause."""
        ctx = _Ctx(
            {"resolve_review_policy": {"verdict": "review", "reasons": ["row_delta_band"]}, "request_approval": True},
            external={"approved": True, "subject": "CiQwOGE4Njg0Yi1kYjg4"},
        )

        _drive(ctx, _spec(), monkeypatch)

        asked = ctx.actions.index("call_activity(request_approval)")
        waited = ctx.actions.index("wait_for_external_event(promotion_decision)")
        assert asked < waited, f"the workflow waited before asking: {ctx.actions}"

    def test_an_APPROVAL_publishes_the_promotion_and_records_who_said_yes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _Ctx(
            {"resolve_review_policy": {"verdict": "review", "reasons": ["row_delta_band"]}, "request_approval": True},
            external={"approved": True, "subject": "CiQwOGE4Njg0Yi1kYjg4"},
        )

        result = _drive(ctx, _spec(), monkeypatch)

        assert result["status"] == "PROMOTED"
        assert result["decided_by"] == "CiQwOGE4Njg0Yi1kYjg4", "the decision is part of the audit, not a side effect"
        assert "call_activity(publish_promotion)" in ctx.actions, "an approved promotion must actually resume the cascade"

    def test_an_approved_promotion_on_a_TERMINAL_tier_STILL_MOVES_THE_TAG(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The tier the chart actually gates on `can_promote` is the one that promoted nothing.

        `promotion_review` guarded the resume with `if spec.pub_topic:` — a condition written when
        pub_topic WAS the promotion mechanism. Under a tag-driven cascade the TAG MOVE is the
        promotion, and `publish_promotion` already chooses between them on `spec.version`. So on the
        terminal tiers the chart ships with `pubTopic: ""` — silver-to-gold (`toDataset:
        gold$catalog`, `requiredAction: can_promote`) and media-to-silver — a person approved, the
        tag never moved, and `emit_promotion_outcome` recorded PROMOTED one line below regardless.
        Wrong data and a lying audit trail from one stale condition.
        """
        ctx = _Ctx(
            {"resolve_review_policy": {"verdict": "review", "reasons": ["row_delta_band"]}, "request_approval": True},
            external={"approved": True, "subject": "CiQwOGE4Njg0Yi1kYjg4"},
        )

        result = _drive(ctx, _spec(pub_topic="", version=7), monkeypatch)

        assert result["status"] == "PROMOTED"
        assert "call_activity(publish_promotion)" in ctx.actions, "an approved promotion on the last tier recorded PROMOTED without moving the tag"

    def test_a_REJECTION_records_the_decision_and_promotes_NOTHING(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ctx = _Ctx(
            {"resolve_review_policy": {"verdict": "review", "reasons": ["row_delta_band"]}, "request_approval": True},
            external={"approved": False, "subject": "CiQxYjZlMmY1YQ"},
        )

        result = _drive(ctx, _spec(), monkeypatch)

        assert result["status"] == "REJECTED"
        assert result["decided_by"] == "CiQxYjZlMmY1YQ"
        assert "call_activity(publish_promotion)" not in ctx.actions


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
    def test_the_review_band_is_resolved_by_an_ACTIVITY_not_read_in_the_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A threshold compiled into the workflow body changes under a running instance and makes
        replay disagree with the original run. It is resolved once, by an activity, and carried."""
        ctx = _Ctx({"resolve_review_policy": {"verdict": "promote", "reasons": []}})

        _drive(ctx, _spec(), monkeypatch)

        assert ctx.actions[0] == "call_activity(resolve_review_policy)", f"policy must be resolved first, by an activity: {ctx.actions}"

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

        verdict = resolve_review_policy(cast(Any, None), _spec(reasons=[]).model_dump())

        assert verdict["verdict"] == "promote"

    def test_a_STRUCTURAL_failure_blocks_even_with_review_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Corruption is not a question. `blob_resolves` failing means the data is wrong, and no
        approval makes it right — so review being ON must not turn it into an ask."""
        monkeypatch.setenv("MEDALLION_QUALITY_REVIEW_ENABLED", "true")
        from medallion.core.config import get_settings
        from medallion.workflow import resolve_review_policy

        get_settings.cache_clear()
        verdict = resolve_review_policy(cast(Any, None), _spec(reasons=["blob_resolves"]).model_dump())
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
        verdict = resolve_review_policy(cast(Any, None), _spec(reasons=["row_delta_band"]).model_dump())

        assert verdict["verdict"] == "block", "review off must BLOCK — the gate must never fail open"

    def test_an_ask_with_no_approver_returns_FALSE_rather_than_pretending(self) -> None:
        """`request_approval` returning True on an unsendable ask would park the workflow on an event
        nobody can raise. It reports the failure so the body can block instead."""
        from medallion.workflow import request_approval

        assert request_approval(cast(Any, None), _spec(approver="").model_dump()) is False

    def test_the_dataset_id_is_PROJECT_QUALIFIED_or_the_approver_is_hidden(self) -> None:
        """A18, again: an unqualified name against tenant-qualified grants counts every recipient
        HIDDEN, so the audience is computed correctly and then discarded whole."""
        from medallion.workflow import _qualified

        assert _qualified("acme", "gold$catalog") == "acme-gold$catalog"
        assert _qualified("acme", "acme-gold$catalog") == "acme-gold$catalog", "already-qualified must not double-prefix"
        assert _qualified("", "gold$catalog") == "gold$catalog", "a single-tenant run stays unqualified"

    def test_a_service_cannot_approve_its_own_promotion(self) -> None:
        from medallion.workflow import settings_author_marker

        spec = _spec()
        assert settings_author_marker(spec) == "service:acme-silver-to-acme-gold"


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
