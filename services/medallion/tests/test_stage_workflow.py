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
from medallion.workflow import MAX_POLLS, StageJobOutcome, StageJobSpec, _is_terminal, publish_stage_ready, report_stage_outcome, stage_run, submit_stage


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
        #: Set by `continue_as_new`; None means the workflow ended inside this turn.
        self.continued: Any = None

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

    def continue_as_new(self, new_input: Any, *, save_events: bool = False) -> None:
        """Record the turn boundary AND the input it carries.

        The carried input is the whole risk of the pattern: a fresh turn has empty history, so if the
        submission id or the poll count did not ride along, the next turn re-submits the Ray job and
        counts from zero. `_drive` below feeds this straight back in, which is what makes the multi-turn
        tests exercise the real hand-off rather than a single turn in isolation.
        """
        self.actions.append("continue_as_new")
        self.continued = new_input


def _drive(ctx: _Ctx, payload: dict[str, Any]) -> dict[str, Any]:
    """Run the workflow to completion ACROSS `continue_as_new` turns.

    A turn is a fresh generator over the input the previous turn handed forward — which is exactly what
    the runtime does, and why this is driven as a loop rather than one generator. The `ctx` is REUSED so
    `ctx.actions` accumulates the whole run: a single turn would prove almost nothing now that one turn
    performs one poll.

    Feeding `ctx.continued` back in is what makes these tests exercise the hand-off. If the workflow
    ever stopped carrying the submission id, the next turn would re-enter the submit branch and the
    action list would show a second `submit_stage` — visible here, invisible in a single-turn drive.
    """
    # cast, not a suppression: `_Ctx` implements the slice of DaprWorkflowContext this
    # workflow uses, which is the whole point of driving it by hand.
    current = payload
    result: dict[str, Any] = {}
    # Bounded so a broken hand-off fails the test instead of hanging the suite.
    for _ in range(200):
        ctx.continued = None
        gen = stage_run(cast("Any", ctx), current)
        try:
            action = gen.send(None)
            while True:
                if action.raises:
                    action = gen.throw(RuntimeError(f"{action.name} exhausted its retries"))
                    continue
                action = gen.send(action.result)
        except StopIteration as stop:
            result = stop.value or {}
        if ctx.continued is None:
            return result
        current = ctx.continued
    raise AssertionError("continue_as_new never converged — the workflow handed forward 200 times")


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

    # ONE poll per turn now, with `continue_as_new` marking each boundary. The submit appears EXACTLY
    # once across all three turns — a second one would mean the submission id stopped riding the
    # carried input and every poll interval was starting a fresh Ray job over the same output.
    assert ctx.actions == [
        "call_activity(submit_stage)",
        "create_timer(30s)",
        "call_activity(poll_stage)",  # PENDING
        "continue_as_new",
        "create_timer(30s)",
        "call_activity(poll_stage)",  # RUNNING
        "continue_as_new",
        "create_timer(30s)",
        "call_activity(poll_stage)",  # SUCCEEDED -> terminal, so no further turn
        "call_activity(publish_stage_ready)",
    ]
    assert ctx.actions.count("call_activity(submit_stage)") == 1, "the job was submitted more than once across turns"
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


# --------------------------------------------------------------------------- #
# The ACTIVITY BODIES — the gap that let three silent defects through
# --------------------------------------------------------------------------- #
#
# Every S1 defect the audit found lived in a path no test drove: the FAILED-job report, the second
# clock, the exhausted publish. All three passed 4,300 tests because the ORDER tests assert what the
# workflow schedules, never what the activities DO. These pin the properties a defect would break.


def test_submit_returns_THE_SAME_id_it_submitted_under(monkeypatch: pytest.MonkeyPatch) -> None:
    """The poll watches whatever this returns. If it derives a DIFFERENT id from the one
    `submit_stage_job` used, the poll reads `None` forever, the watch abandons at the ceiling, and a
    perfectly healthy job is reported as never finishing — with no error anywhere."""
    from medallion.services import ray_submit
    from medallion.workflow import submit_stage

    submitted: dict[str, str] = {}

    async def _fake_submit(_settings: Any, *, from_uri: str, to_uri: str, stage: str, token: str | None, lineage_json: str = "") -> None:
        submitted["id"] = ray_submit.stage_submission_id(stage, token, from_uri, to_uri)

    monkeypatch.setattr("medallion.services.ray_submit.submit_stage_job", _fake_submit)

    returned = submit_stage(cast("Any", None), _spec())

    assert returned == submitted["id"], "the poll would watch an id the submitter never used"


def test_poll_answers_NONE_for_an_id_the_dashboard_has_not_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The submit/register race, and why it must not be fatal.

    The workflow polls seconds after submitting; the dashboard may not know the id yet. Raising there
    would abort a healthy job on a timing artefact, so `job_status` answers None and the loop simply
    asks again.
    """
    from medallion.workflow import poll_stage

    async def _unknown(_client: Any, _sub: str) -> str | None:
        return None

    monkeypatch.setattr("ray_kit.submit.job_status", _unknown)

    assert poll_stage(cast("Any", None), {"submission_id": "not-registered-yet"}) is None


def test_poll_RAISES_on_transport_failure_rather_than_reporting_no_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable dashboard is not evidence ABOUT THE JOB.

    Swallowing it into `None` would make an outage indistinguishable from "not registered yet" — the
    watch would poll to its ceiling and report `abandoned` for a job that may well have succeeded.
    The activity's retry policy is the right owner of a transport blip.
    """
    from medallion.workflow import poll_stage

    from ray_kit.submit import RayJobError

    async def _down(_client: Any, _sub: str) -> str | None:
        raise RayJobError("dashboard unreachable")

    monkeypatch.setattr("ray_kit.submit.job_status", _down)

    with pytest.raises(RayJobError):
        poll_stage(cast("Any", None), {"submission_id": "sub"})


def test_the_wakeup_carries_the_FLAG_and_goes_to_the_movers_OWN_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three properties in one publish, each of which silently breaks the cascade if wrong.

    `ray_job_done` absent -> the mover dispatches a SECOND watcher instead of measuring, forever.
    The wrong topic -> nothing consumes it and the run stops with data on disk.
    A bare `.publish_event` -> unbounded, so a wedged sidecar hangs the activity and the workflow
    never advances (the estate has an invariant test for that one).
    """
    from medallion.core.config import get_settings
    from medallion.workflow import publish_stage_ready

    sent: dict[str, Any] = {}

    async def _capture(_client: Any, *, timeout_seconds: float, **kwargs: Any) -> None:
        sent.update(kwargs)
        sent["timeout_seconds"] = timeout_seconds

    monkeypatch.setattr("service_kit.dapr_publish.publish_event", _capture)

    class _NoopClient:
        async def __aenter__(self) -> _NoopClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr("dapr.aio.clients.DaprClient", lambda *a, **k: _NoopClient())

    publish_stage_ready(
        cast("Any", None),
        {"spec": _spec(), "outcome": {"submission_id": "ray-silver-tok-1-abc", "status": "SUCCEEDED", "polls": 1, "verdict": "succeeded"}},
    )

    settings = get_settings()
    body = json.loads(sent["data"])
    assert body["ray_job_done"] is True, "without the flag the mover dispatches another watcher instead of measuring"
    assert body["ray_submission_id"] == "ray-silver-tok-1-abc"
    assert sent["topic_name"] == settings.sub_topic, "the wake-up must reach the mover's OWN subscription"
    assert sent["timeout_seconds"] > 0, "an unbounded publish hangs the activity on a wedged sidecar"


def test_register_ACTUALLY_registers_every_definition() -> None:
    """The failure this prevents is a deploy-time one with an unhelpful message.

    `test_every_activity_is_registered` checks the CONTENTS of WORKFLOWS/ACTIVITIES; nothing checked
    that `register()` hands them to the runtime. A definition present in the tuple but never passed to
    `register_activity` fails at runtime with "no such activity" — after the workflow has already
    started, on a pod whose probes are green. That is precisely the asymmetry ingest's lifespan
    docstring describes ("a definition registered in the API process but not the worker").
    """
    from medallion.workflow import ACTIVITIES, WORKFLOWS, register

    class _Runtime:
        def __init__(self) -> None:
            self.workflows: list[Any] = []
            self.activities: list[Any] = []

        def register_workflow(self, fn: Any) -> None:
            self.workflows.append(fn)

        def register_activity(self, fn: Any) -> None:
            self.activities.append(fn)

    runtime = _Runtime()
    register(cast("Any", runtime))

    assert set(runtime.workflows) == set(WORKFLOWS), "a workflow in the tuple never reached the runtime"
    assert set(runtime.activities) == set(ACTIVITIES), "an activity in the tuple never reached the runtime"
    # Every activity the workflow SCHEDULES must be registered — the tuple is hand-kept, so this is
    # where a new `ctx.call_activity(...)` with a forgotten registration shows up.
    assert {submit_stage, publish_stage_ready, report_stage_outcome} <= set(runtime.activities)


def test_the_FAIL_emit_goes_through_the_OUTBOX_not_a_bare_publish(monkeypatch: pytest.MonkeyPatch) -> None:
    """The durability half of the FAIL fix, which the earlier tests stub past.

    A bare publish loses the FAIL on a crash between the decision and the send — and the FAIL is the
    ONLY record that a succeeded-then-unreported run exists. The handler's own failure path stages
    through the outbox for exactly that reason; this asserts the workflow's does too, and that it
    carries the deterministic run_id so a redelivery MERGEs rather than forking a second failure.
    """
    from medallion.workflow import _build_stage_fail_event, _publish_fail_event

    staged: dict[str, Any] = {}

    async def _capture(_client: Any, **kwargs: Any) -> None:
        staged.update(kwargs)

    monkeypatch.setattr("service_kit.lakehouse.outbox.publish_lineage_with_outbox", _capture)

    class _NoopClient:
        async def __aenter__(self) -> _NoopClient:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr("dapr.aio.clients.DaprClient", lambda *a, **k: _NoopClient())

    spec = StageJobSpec.model_validate(_spec())
    outcome = StageJobOutcome(submission_id="sub", status="FAILED", polls=2, verdict="failed")
    event = _build_stage_fail_event(spec, outcome, "the Ray stage job sub ended FAILED after 2 poll(s)")

    _publish_fail_event(event, spec)

    assert staged, "the FAIL never reached the outbox"
    assert staged["run_id"] == event["run"]["runId"], "a redelivery would fork a second failure run"
    assert json.loads(staged["event_json"])["eventType"] == "FAIL"


def test_a_SUBMIT_that_exhausts_its_retries_still_reports() -> None:
    """The third silent path, and the earliest one.

    `submit_stage` had a retry policy and no boundary, so an exhausted submit raised into the
    workflow and took the instance terminal FAILED with no report. Nothing was watching a job that
    may or may not have been submitted, the trigger was already acked in pass 1, and the only record
    was a Dapr instance in a FAILED state nobody queries.
    """
    ctx = _Ctx({"submit_stage": ["sub"]})
    ctx.raise_on = "submit_stage"

    outcome = _drive(ctx, _spec())

    assert "call_activity(report_stage_outcome)" in ctx.actions, "an exhausted submit reported nothing"
    assert outcome["verdict"] == "failed"
    assert "call_activity(poll_stage)" not in ctx.actions, "nothing should be polled when the submit never landed"


def test_a_POLL_that_exhausts_its_retries_reports_ABANDONED() -> None:
    """The watch lost, which is not the same as the job failing.

    An exhausted poll means the dashboard stayed unreachable across the whole retry policy. The JOB
    may be running perfectly. Reporting `failed` would send an operator hunting a healthy job, so this
    reuses `abandoned` — the vocabulary already carrying "we stopped watching, the job may still
    land" — rather than inventing a fourth verdict for the same fact.
    """
    ctx = _Ctx({"submit_stage": ["sub"], "poll_stage": ["RUNNING"]})
    ctx.raise_on = "poll_stage"

    outcome = _drive(ctx, _spec())

    assert "call_activity(report_stage_outcome)" in ctx.actions, "a lost watch reported nothing"
    assert outcome["verdict"] == "abandoned"
    assert "call_activity(publish_stage_ready)" not in ctx.actions, "an unwatched job must not wake the mover"


def test_a_stage_FAIL_names_the_human_the_cascade_is_running_for() -> None:
    """THE CASCADE'S TARGETING DEFECT.

    A mover authors its events with a chart ROLE LITERAL (`MEDALLION_AUTHOR` — `data_eng`, `analyst`,
    `htr`, `ray`), so the FAIL a failed Ray stage emits addressed an inbox actor named `data_eng`:
    nobody. The person whose ingest started the cascade was told nothing about the failure of their own
    run, and only someone who had explicitly opted into watching the project heard anything.

    The originator rides the TRIGGER — the same carrier as `token` and `project` — because by the time a
    stage runs, the request that started it is long gone; the cascade head is the last place the verified
    subject exists. `author` is deliberately left alone: the mover really did run the stage, and
    overwriting attribution to fix targeting trades one wrong answer for another.
    """
    from medallion.workflow import _build_stage_fail_event

    spec = StageJobSpec.model_validate(_spec(trigger={"token": "tok-1", "dataset": "pages", "originator": "alice"}))
    outcome = StageJobOutcome(submission_id="sub", status="FAILED", polls=2, verdict="failed")
    event = _build_stage_fail_event(spec, outcome, "the Ray stage job sub ended FAILED after 2 poll(s)")

    assert event["run"]["facets"]["lance"]["originator"] == "alice"
    assert event["run"]["facets"]["author"]["sub"] != "alice", "attribution stays with the mover that ran it"


def test_a_stage_FAIL_without_an_originator_is_unchanged() -> None:
    """Absent is the normal case for a single-tenant estate and for every trigger published before this
    field existed. It must stay byte-identical, so the field can land at one producer at a time."""
    from medallion.workflow import _build_stage_fail_event

    spec = StageJobSpec.model_validate(_spec())
    outcome = StageJobOutcome(submission_id="sub", status="FAILED", polls=2, verdict="failed")
    event = _build_stage_fail_event(spec, outcome, "reason")

    assert "originator" not in event["run"]["facets"]["lance"]


def test_a_stage_FAIL_names_the_PROJECT_QUALIFIED_datasets_like_the_COMPLETE_does() -> None:
    """THE FAILURE NOBODY COULD BE TOLD ABOUT, because of a name.

    `transform.py`'s COMPLETE emits the project-QUALIFIED locals — `project_namespace(project, ...)`,
    e.g. `acme-silver` / `acme-silver$features` — which is what the catalog's grants are written
    against. This FAIL builder read the raw env values instead (`settings.to_namespace`), so a tenant
    run's failure named `silver$features` while every grant named `acme-silver$features`.

    The consequence is worse than a cosmetic mismatch, and it is silent. Delivery re-derives each
    recipient's visibility against `table:<output name>`, so a name nobody holds a grant on counts
    EVERY recipient HIDDEN — the audience is computed correctly and then discarded whole. The author
    is not told about their own failed run, and neither is any watcher.

    The two paths must agree because they describe the same dataset; that they diverged at all is the
    defect, and pinning both halves is what keeps them together.
    """
    from medallion.workflow import _build_stage_fail_event

    spec = StageJobSpec.model_validate(_spec(trigger={"token": "tok-1", "dataset": "pages", "project": "acme"}))
    outcome = StageJobOutcome(submission_id="sub", status="FAILED", polls=2, verdict="failed")
    event = _build_stage_fail_event(spec, outcome, "the Ray stage job sub ended FAILED")

    output = event["outputs"][0]
    assert output["namespace"].startswith("acme-"), f"unqualified output namespace: {output['namespace']}"
    assert output["name"].startswith("acme-"), f"unqualified output name: {output['name']}"
    assert event["inputs"][0]["namespace"].startswith("acme-"), "the input edge must be qualified too"


def test_a_projectless_stage_FAIL_is_byte_identical() -> None:
    """Single-tenant is the default and must not move: with no project the names are exactly the env
    values, which is what `project_namespace` returns for an empty project."""
    from medallion.workflow import _build_stage_fail_event

    spec = StageJobSpec.model_validate(_spec())
    outcome = StageJobOutcome(submission_id="sub", status="FAILED", polls=1, verdict="failed")
    event = _build_stage_fail_event(spec, outcome, "reason")

    assert "-" not in event["outputs"][0]["namespace"].split("$")[0].replace("gold-htr", "goldhtr")
