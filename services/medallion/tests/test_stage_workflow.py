"""S1 — the stage workflow waits for the job before anything downstream runs.

The defect these pin is an ORDERING one, and ordering is the one thing a mocked unit test can assert
faithfully: the question is not whether `measure_stage` computes the right numbers (other tests own
that) but whether it is allowed to run before Ray has finished writing what it measures.

So the workflow is driven through a fake `DaprWorkflowContext` that records the sequence of actions
the generator yields. That is exactly what Dapr's replay engine compares against recorded history, so
a test that pins the sequence pins the thing the runtime cares about.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from medallion.workflow import MAX_POLLS, StageJobSpec, _is_terminal, publish_stage_ready, report_stage_outcome, stage_run, submit_stage


class _Action:
    """One yielded action, and the value the fake feeds back in."""

    def __init__(self, kind: str, name: str = "", result: Any = None) -> None:
        self.kind = kind
        self.name = name
        self.result = result


class _Ctx:
    """A replay-faithful stand-in: it records what was yielded and answers with scripted results."""

    def __init__(self, activity_results: dict[str, list[Any]] | None = None) -> None:
        self.actions: list[str] = []
        self._results = {k: list(v) for k, v in (activity_results or {}).items()}
        self.is_replaying = False
        self.instance_id = "wf-test"
        self.current_utc_datetime = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)

    def call_activity(self, activity: Any, *, input: Any = None, retry_policy: Any = None) -> _Action:  # noqa: A002
        name = getattr(activity, "__name__", str(activity))
        self.actions.append(f"call_activity({name})")
        queue = self._results.get(name)
        value = queue.pop(0) if queue else None
        return _Action("activity", name, value)

    def create_timer(self, delay: timedelta) -> _Action:
        self.actions.append(f"create_timer({int(delay.total_seconds())}s)")
        return _Action("timer")


def _drive(ctx: _Ctx, payload: dict[str, Any]) -> dict[str, Any]:
    """Run the generator to completion, feeding each yielded action's scripted result back in."""
    # cast, not a suppression: `_Ctx` implements the slice of DaprWorkflowContext this
    # workflow uses, which is the whole point of driving it by hand.
    gen = stage_run(cast("Any", ctx), payload)
    sent: Any = None
    try:
        while True:
            action = gen.send(sent)
            sent = action.result
    except StopIteration as stop:
        return stop.value or {}


def _spec(**over: Any) -> dict[str, Any]:
    base = StageJobSpec(
        from_uri="s3://wh/p-bronze/pages.lance",
        to_uri="s3://wh/p-silver/pages.lance",
        stage="silver",
        token="tok-1",
        trigger={"token": "tok-1", "dataset": "pages"},
        poll_interval_seconds=30,
        max_polls=5,
    )
    return base.model_copy(update=over).model_dump()


def test_the_publish_happens_ONLY_AFTER_a_terminal_read() -> None:
    """THE defect, stated as an order.

    `transform.py:333` submits and measures back to back, so the measure races the job. Here the
    downstream wake-up must appear after a terminal poll and never before one — if `publish_stage_ready`
    could precede the last `poll_stage`, the mover would measure a dataset the job had not written.
    """
    ctx = _Ctx({"submit_stage": ["ray-silver-tok-1-abc"], "poll_stage": ["PENDING", "RUNNING", "SUCCEEDED"]})

    _drive(ctx, _spec())

    assert ctx.actions == [
        "call_activity(submit_stage)",
        "create_timer(30s)",
        "call_activity(poll_stage)",
        "create_timer(30s)",
        "call_activity(poll_stage)",
        "create_timer(30s)",
        "call_activity(poll_stage)",
        "call_activity(publish_stage_ready)",
    ]
    publish_at = ctx.actions.index("call_activity(publish_stage_ready)")
    last_poll = len(ctx.actions) - 1 - ctx.actions[::-1].index("call_activity(poll_stage)")
    assert publish_at > last_poll, "the mover was woken before the job's terminal state was read"


def test_a_job_that_never_finishes_does_NOT_wake_the_mover() -> None:
    """The abandoned case. Waking the mover here would measure a dataset still being written.

    Reported as `abandoned` rather than `failed` deliberately: the job may still land, and this estate's
    recurring defect is a state reported as something it is not.
    """
    ctx = _Ctx({"submit_stage": ["ray-silver-tok-1-abc"], "poll_stage": ["RUNNING"] * 5})

    outcome = _drive(ctx, _spec(max_polls=5))

    assert "call_activity(publish_stage_ready)" not in ctx.actions
    assert "call_activity(report_stage_outcome)" in ctx.actions
    assert outcome["verdict"] == "abandoned"
    assert outcome["polls"] == 5


@pytest.mark.parametrize("terminal_bad", ["FAILED", "STOPPED"])
def test_a_TERMINAL_BAD_job_does_NOT_wake_the_mover(terminal_bad: str) -> None:
    """A failed job wrote nothing. Publishing would have the mover measure the PRIOR version and emit
    a COMPLETE for rows this job never produced — the silent-wrong branch of the original defect."""
    ctx = _Ctx({"submit_stage": ["ray-silver-tok-1-abc"], "poll_stage": [terminal_bad]})

    outcome = _drive(ctx, _spec())

    assert "call_activity(publish_stage_ready)" not in ctx.actions
    assert outcome["verdict"] == "failed"
    assert outcome["status"] == terminal_bad


def test_the_poll_loop_is_BOUNDED_so_history_cannot_grow_without_limit() -> None:
    """DWF-DET-013. An unbounded `while True` grows workflow history forever; a RUNNING instance is
    never collected by `stateRetentionPolicy`, so the growth has no other ceiling either.

    The bound is what makes S1 shippable ahead of S2's `continue_as_new`. Asserted as a RELATION to
    the constant so raising the ceiling without thinking about history fails here.
    """
    ctx = _Ctx({"submit_stage": ["sub"], "poll_stage": ["RUNNING"] * 50})

    _drive(ctx, _spec(max_polls=7))

    assert ctx.actions.count("call_activity(poll_stage)") == 7, "the loop ran past its bound"
    assert MAX_POLLS * 30 <= 24 * 3600 + 1, "the default ceiling should be about a day of waiting, not a week"


def test_the_workflow_body_yields_ONLY_ctx_actions() -> None:
    """DWF-DET-005/006/007/014 in one assertion: every yielded object came from the fake context.

    A workflow that yields a bare coroutine, an `asyncio.gather`, or an httpx call escapes the
    scheduler and runs once per replay. Driving the generator with a context that can only produce
    `_Action`s makes that structurally visible.
    """
    ctx = _Ctx({"submit_stage": ["sub"], "poll_stage": ["SUCCEEDED"]})
    gen = stage_run(cast("Any", ctx), _spec())
    sent: Any = None
    seen = 0
    try:
        while True:
            action = gen.send(sent)
            assert isinstance(action, _Action), f"the workflow yielded a non-context object: {type(action)!r}"
            seen += 1
            sent = action.result
    except StopIteration:
        pass
    assert seen >= 3


def test_terminality_agrees_with_ray_kits_own_constants() -> None:
    """The workflow body compares against local literals so it has no import-time behaviour. That
    duplication is only safe while it AGREES — this is what stops it drifting."""
    from ray_kit.submit import TERMINAL_BAD, TERMINAL_OK, is_terminal

    for status in (TERMINAL_OK, *TERMINAL_BAD):
        assert _is_terminal(status) is True
        assert is_terminal(status) is True
    for status in ("PENDING", "RUNNING", None):
        assert _is_terminal(status) is False
        assert is_terminal(status) is False


def test_the_submitter_and_the_poller_name_the_SAME_job() -> None:
    """The failure this prevents is invisible: a poller watching an id the submitter never used sees
    `None` forever and abandons a job that completed fine."""
    from medallion.services.ray_submit import stage_submission_id

    a = stage_submission_id("silver", "tok-1", "s3://a", "s3://b")
    b = stage_submission_id("silver", "tok-1", "s3://a", "s3://b")
    assert a == b, "the id is not deterministic — redelivery would start a second job"
    assert stage_submission_id("silver", "tok-1", "s3://a", "s3://c") != a, "from->to must be part of the identity"


def test_every_activity_is_registered() -> None:
    """A definition the runtime does not know fails at runtime with an unhelpful 'no such activity'."""
    from medallion.workflow import ACTIVITIES, WORKFLOWS

    assert set(WORKFLOWS) == {stage_run}
    assert {submit_stage, publish_stage_ready, report_stage_outcome} <= set(ACTIVITIES)
