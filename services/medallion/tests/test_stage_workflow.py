"""S1 — the stage workflow waits for the job before anything downstream runs.

The defect these pin is an ORDERING one, and ordering is the one thing a mocked unit test can assert
faithfully: the question is not whether `measure_stage` computes the right numbers (other tests own
that) but whether it is allowed to run before Ray has finished writing what it measures.

So the workflow is driven through a fake `DaprWorkflowContext` that records the sequence of actions
the generator yields. That is exactly what Dapr's replay engine compares against recorded history, so
a test that pins the sequence pins the thing the runtime cares about.
"""

from __future__ import annotations

import json
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
        #: True when the runtime would raise this task's failure into the generator.
        self.raises = False


class _Ctx:
    """A replay-faithful stand-in: it records what was yielded and answers with scripted results."""

    def __init__(self, activity_results: dict[str, list[Any]] | None = None) -> None:
        self.actions: list[str] = []
        self._results = {k: list(v) for k, v in (activity_results or {}).items()}
        self.is_replaying = False
        #: Activity name whose scheduled task should RAISE, modelling an exhausted retry policy.
        self.raise_on: str | None = None
        self.instance_id = "wf-test"
        self.current_utc_datetime = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)

    def call_activity(self, activity: Any, *, input: Any = None, retry_policy: Any = None) -> _Action:  # noqa: A002
        name = getattr(activity, "__name__", str(activity))
        self.actions.append(f"call_activity({name})")
        queue = self._results.get(name)
        value = queue.pop(0) if queue else None
        action = _Action("activity", name, value)
        action.raises = name == self.raise_on
        return action

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
        action = gen.send(None)
        while True:
            if action.raises:
                action = gen.throw(RuntimeError(f"{action.name} exhausted its retries"))
                continue
            action = gen.send(action.result)
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


# --------------------------------------------------------------------------- #
# A failed job must reach the GRAPH, not just the log
# --------------------------------------------------------------------------- #


def test_a_TERMINAL_BAD_job_emits_a_lineage_FAIL(monkeypatch: pytest.MonkeyPatch) -> None:
    """S1 REGRESSED failure visibility and this is the fix.

    Before S1 the ray branch submitted and measured in one pass, so a failed job made `measure_stage`
    raise, the handler's `except` emitted a FAIL RunEvent with an errorMessage facet, and the trigger
    RETRYd. After S1, pass 1 acks SUCCESS the moment the watcher is dispatched — so a job that then
    FAILS produced a log line and NOTHING ELSE. No FAIL in the graph, no failed feed row, nothing for
    the notifications plane to target, and the run simply never appears to end.

    `report_stage_outcome`'s own docstring claimed "the record is the log line and the counter". There
    was no counter either.

    The emit is best-effort and suppressed, exactly like the handler's: a lineage outage must not turn
    a reported failure into a retried activity, because the ONE thing worse than a silent failure here
    is a workflow that cannot finish reporting one.
    """
    from medallion.workflow import report_stage_outcome

    published: list[dict[str, Any]] = []

    def _capture(event: dict[str, Any], _spec: Any) -> None:
        published.append(event)

    monkeypatch.setattr("medallion.workflow._publish_fail_event", _capture)

    report_stage_outcome(
        cast("Any", None),
        {
            "spec": _spec(),
            "outcome": {"submission_id": "ray-silver-tok-1-abc", "status": "FAILED", "polls": 3, "verdict": "failed"},
        },
    )

    assert published, "a FAILED Ray job emitted no lineage event — the failure exists only in a log line"
    event = published[0]
    assert event["eventType"] == "FAIL"
    assert "FAILED" in json.dumps(event), "the FAIL must name the Ray status that caused it"


def test_an_ABANDONED_watch_also_reaches_the_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ceiling case. A job still RUNNING when the watch gives up is not a job failure — but it is
    equally invisible, and an operator needs to see that the estate stopped watching something."""
    from medallion.workflow import report_stage_outcome

    published: list[dict[str, Any]] = []
    monkeypatch.setattr("medallion.workflow._publish_fail_event", lambda e, _s: published.append(e))

    report_stage_outcome(
        cast("Any", None),
        {"spec": _spec(), "outcome": {"submission_id": "sub", "status": "RUNNING", "polls": 2880, "verdict": "abandoned"}},
    )

    assert published, "an abandoned watch reported nothing to the graph"
    assert "abandoned" in json.dumps(published[0]).lower()


def test_a_lineage_OUTAGE_does_not_fail_the_reporting_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Best-effort, and this is the reason. An activity that raises is retried and can end FAILED, so
    a lineage outage would leave the workflow unable to finish reporting a failure — strictly worse
    than the silent log this replaces."""
    from medallion.workflow import report_stage_outcome

    def _boom(_e: Any, _s: Any) -> None:
        raise RuntimeError("lineage is down")

    monkeypatch.setattr("medallion.workflow._publish_fail_event", _boom)

    report_stage_outcome(
        cast("Any", None),
        {"spec": _spec(), "outcome": {"submission_id": "sub", "status": "FAILED", "polls": 1, "verdict": "failed"}},
    )


def test_a_publish_that_EXHAUSTS_its_retries_still_reports() -> None:
    """The lost wake-up, and the last silent path in S1.

    Pass 1 acks the trigger, so the ONLY thing that can drive the measure/emit/cascade is this
    workflow's `publish_stage_ready`. It is called with a retry policy and — until this test — no
    error boundary: an exhausted publish raised into the workflow, the instance went terminal FAILED,
    and `report_stage_outcome` never ran. A Ray job that SUCCEEDED, wrote its data, and then could not
    wake the mover left nothing anywhere. That is the same silence the FAILED-job fix closed, arriving
    by the other door.

    An activity failure DOES raise into the generator and can be caught (unlike the replay-mismatch
    error, which is raised outside it) — so the boundary is legitimate here, and Dapr's own guidance
    is that compensating for a failed activity is what a workflow's try/except is for.
    """
    ctx = _Ctx({"submit_stage": ["sub"], "poll_stage": ["SUCCEEDED"]})
    ctx.raise_on = "publish_stage_ready"

    outcome = _drive(ctx, _spec())

    assert "call_activity(report_stage_outcome)" in ctx.actions, (
        "the wake-up publish exhausted its retries and NOTHING reported it — the job succeeded, the data landed, and the cascade simply stopped"
    )
    assert outcome["verdict"] == "unnotified", "the outcome must name WHY the run did not continue"
