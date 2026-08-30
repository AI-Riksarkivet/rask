"""An approved promotion whose publish is REFUSED must still reach the audit trail.

`publish_promotion` -> `_resume_publish` -> `publish_stage_output` raises `RegisterError` on any
catalog 4xx/5xx. The call site was bare, unlike the structurally identical `publish_stage_ready` in
`stage_run`, which IS wrapped with a comment saying exactly why: "an exhausted retry policy used to
raise into the workflow, take the instance terminal FAILED, and skip the report entirely".

The trigger is not hypothetical and not transient. The approval window defaults to 72 hours, and
`PromotionSpec.version` anticipates in its own comment that "a later commit may have landed while
the approver was deciding". If a later version was also published in that window, the catalog's
`publication.publish` refuses to move `published` backwards -- an `InvalidTableStateError` that is
DETERMINISTIC, so all five ACTIVITY_RETRY attempts fail identically.

What that cost before this fix: the exception propagated out of the generator, the instance went
terminal FAILED, and `emit_promotion_outcome` -- the only writer of the durable record -- never ran.
The validator got their 202, the tag did not move, and lineage, the metric and `GET /promotions/{id}`
all showed nothing. A person's governance decision was destroyed, leaving a daprd-side counter.

It is NOT swallowed into PROMOTED: the tag did not move, so `PROMOTION_FAILED` is a distinct status
and `emit_promotion_outcome` emits it as a FAIL event, not a COMPLETE.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from medallion.workflow import PromotionSpec, promotion_review


def _recorded(payload: object) -> dict[str, Any]:
    """An activity input as the RUNTIME records it: JSON, never the model instance.

    Activities declare Pydantic inputs now (DWF-ACT-009) and the SDK coerces on the worker side, so a
    workflow body hands `call_activity` a MODEL while history keeps the serialized form. A fake that
    stored the instance would let assertions read attributes the real recorded payload does not have.
    """
    dump = getattr(payload, "model_dump", None)
    if callable(dump):
        dumped = dump(mode="json")
        return dumped if isinstance(dumped, dict) else {}
    return payload if isinstance(payload, dict) else {}


class _Action:
    def __init__(self, kind: str, name: str = "", value: Any = None) -> None:
        self.kind, self.name, self.value = kind, name, value

    def get_result(self) -> Any:
        return self.value


class _Ctx:
    """The same replay-faithful double `test_promotion_review` uses, plus a failing activity."""

    def __init__(self, results: dict[str, Any] | None = None, *, external: Any = None, fails: str = "") -> None:
        self.actions: list[str] = []
        self.inputs: dict[str, Any] = {}
        self._results = dict(results or {})
        self.is_replaying = False
        self.instance_id = "promo-test"
        self.current_utc_datetime = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
        self._external = external
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
        self.actions.append("when_any")
        won = next((t for t in tasks if t.kind == "external"), tasks[0])
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
    """Like `test_promotion_review._drive`, but a named activity RAISES at its yield point.

    That is what an exhausted `ACTIVITY_RETRY` actually does to a workflow body, and it is the whole
    subject here — a driver that only returns values cannot exercise an error boundary.
    """
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


_APPROVED = {"approved": True, "subject": "CiQwOGE4Njg0Yi1kYjg4"}
_POLICY = {"resolve_review_policy": {"verdict": "review", "reasons": ["row_delta_band"]}, "request_approval": {"delivered": True}}


def test_a_refused_publish_is_recorded_as_PROMOTION_FAILED_not_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wedge: before the boundary, the instance died here and the decision left no record."""
    ctx = _Ctx(_POLICY, external=_APPROVED, fails="publish_promotion")

    result = _drive(ctx, _spec(), monkeypatch)

    assert result["status"] == "PROMOTION_FAILED"
    assert result["decided_by"] == "CiQwOGE4Njg0Yi1kYjg4", "who approved it survives the failure — that is the part worth keeping"
    assert "call_activity(emit_promotion_outcome)" in ctx.actions, "the durable record is the only place this decision survives"
    outcome = ctx.inputs["emit_promotion_outcome"]["outcome"]
    assert outcome["status"] == "PROMOTION_FAILED"
    assert any("backwards" in reason for reason in outcome["reasons"]), f"the reason the publish was refused must ride along; got {outcome['reasons']}"


def test_a_refused_publish_is_NEVER_reported_as_PROMOTED(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swallowing into PROMOTED would be worse than the crash: the tag did not move, and a lying
    audit trail is unrecoverable in a way a missing one is not."""
    ctx = _Ctx(_POLICY, external=_APPROVED, fails="publish_promotion")

    result = _drive(ctx, _spec(), monkeypatch)

    assert result["status"] != "PROMOTED"
    assert ctx.inputs["emit_promotion_outcome"]["outcome"]["status"] != "PROMOTED"


def test_the_SUCCESS_path_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """The boundary must not change what an ordinary approval does — same actions, same order."""
    ctx = _Ctx(_POLICY, external=_APPROVED)

    result = _drive(ctx, _spec(), monkeypatch)

    assert result["status"] == "PROMOTED"
    assert ctx.actions[-2:] == ["call_activity(publish_promotion)", "call_activity(emit_promotion_outcome)"]
