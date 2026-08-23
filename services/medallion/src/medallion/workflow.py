"""S1 — the stage transform as a Dapr Workflow, so the MEASURE waits for the JOB.

THE DEFECT THIS CLOSES (the cascade design record, `docs/architecture/medallion-cascade.md`; `transform.py:333`). The Ray branch of
`handle_stage` reads:

    await submit_stage_job(...)                    # returns the instant Ray accepts the submission
    result = await run_in_threadpool(measure_stage, from_uri, to_uri, ...)

`submit_stage_job` is submit-and-ack by design and says so ("Submit … and RETURN — never block").
So `measure_stage` opens the DESTINATION dataset while the job that writes it has, in general, not
run yet. Two outcomes, and the second is the dangerous one:

  * the destination does not exist -> the measure raises, the handler RETRYs, the sidecar redelivers,
    and the transform eventually stumbles into correctness by repetition. Noisy, but not wrong.
  * the destination exists from a PRIOR run -> the measure succeeds against the OLD version. The run
    then emits a COMPLETE stamped with a version and row count this job did not produce, and fires
    the next tier off it. Silver's lineage says gold's rows arrived; nothing anywhere is red.

A13's comment on `submit_stage_job` argues nothing needs a completion poll, "a job's completion
signal is its own registered commit". That is true of the ACK and it is why the poll was deleted. It
stopped being true of this CALLER when the measure was added for column lineage: that measurement is
a question about the job's output, asked before the job has produced one. So S1 does not undo A13 —
it puts the waiting somewhere A13's objection does not apply.

WHY A WORKFLOW AND NOT A LOOP. A13's real objection was never "polling is inelegant", it was that the
poll held a Dapr ack across the job's whole runtime, so a long job exhausted the redelivery window and
the ack contract became a race. A workflow does not have that problem: the trigger handler schedules
an instance and acks in milliseconds, and the WAITING lives in `ctx.create_timer` — a durable,
runtime-managed timer that holds no thread, no connection and no ack. The instance can wait hours
across pod restarts. This is Dapr's documented async-HTTP / monitor pattern.

THE SHAPE. `stage_run` submits, polls to a terminal state, and only then publishes the trigger back to
the mover with `ray_job_done` set — at which point the existing handler runs its measure/emit/cascade
path unchanged, against a dataset that is now actually written. Re-publishing rather than re-hosting
the handler's 520 lines as activities is deliberate: the post-compute path is the same code on every
lane (in-process, Ray), and forking it per lane is how the lanes drift apart.

DETERMINISM (checked against the dapr-skills Python checklist, DWF-DET-001..015). No wall clock — the
deadline is derived from `ctx.current_utc_datetime`. No sleep — `ctx.create_timer`. No I/O, no
`os.environ`, no randomness in the workflow body; every one of those lives in an activity. Logging in
the body is guarded by `not ctx.is_replaying`. The poll loop is BOUNDED (DWF-DET-013): an unbounded
`while True` grows history without limit, and this file deliberately does not reach for
`continue_as_new` — that is S2, which lands with the history-growth assertion that gives it teeth.
Until then the bound is what keeps history finite, and it is asserted in the tests.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import dapr.ext.workflow as wf
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel, Field

from medallion.core.best_effort import best_effort
from medallion.core.metrics import record_promotion_outcome, record_stage_outcome, record_train_outcome


if TYPE_CHECKING:
    from collections.abc import Generator

    from dapr.ext.workflow import DaprWorkflowContext, WorkflowActivityContext


log = logging.getLogger(__name__)


#: Retry for the activities. Matches ingest's policy in shape and reasoning: a transient dashboard or
#: broker blip must not fail a stage that is running fine, and an activity that keeps failing must
#: eventually surface rather than retry forever.
ACTIVITY_RETRY: Final = wf.RetryPolicy(
    first_retry_interval=timedelta(seconds=2),
    max_number_of_attempts=5,
    backoff_coefficient=2.0,
    max_retry_interval=timedelta(seconds=60),
)

#: Seconds between status reads. A stage transform is minutes-to-hours work, so a tight interval buys
#: nothing and costs one history entry per poll — the growth S2 exists to bound.
POLL_INTERVAL_SECONDS: Final = 30

#: Hard ceiling on poll iterations, so history cannot grow without bound (DWF-DET-013). At the default
#: interval this is 24h of waiting. A job still running at the ceiling is NOT killed — the workflow
#: gives up WATCHING it and says so; the job's own registered commit remains the source of truth, and
#: the lineage reconciler still catches a job that dies (A13's argument, which stays true).
MAX_POLLS: Final = 2880

#: Assertion failures that are CORRUPTION rather than a judgement call. A blob pointer that does not
#: resolve and a null key column are structurally wrong — no approval makes the data right, so nobody
#: is asked. Every other failure is the archive's call.
_STRUCTURAL_FAILURES: Final = frozenset({"blob_resolves", "not_null"})


class StageJobSpec(BaseModel):
    """What the workflow needs to submit, watch and hand back — nothing more.

    Deliberately NOT the whole trigger: everything here becomes a Ray submission id, an S3 read URI or
    a bus payload, and a workflow input is persisted to the state store on every checkpoint. Carrying
    the untrusted envelope wholesale would persist it too.
    """

    from_uri: str
    to_uri: str
    stage: str
    token: str | None = None
    lineage_json: str = ""
    #: The trigger to re-publish once the job is terminal, verbatim as the mover will re-parse it.
    #: Held as a dict rather than a `StageTrigger` so a field added to the trigger contract does not
    #: silently drop off the round trip.
    trigger: dict[str, Any] = Field(default_factory=dict)
    poll_interval_seconds: int = POLL_INTERVAL_SECONDS
    max_polls: int = MAX_POLLS
    #: CARRIED ACROSS `continue_as_new`, and load-bearing for it. Each turn starts with EMPTY history,
    #: so a turn has no memory that `submit_stage` already ran — without these two fields the next turn
    #: would submit the same stage job again (once per poll interval, forever, each overwriting the same
    #: output dataset) and would never reach the ceiling because the count restarted at zero.
    #: `None`/`0` mean "first turn": submit, then count from there.
    submission_id: str | None = None
    polls_done: int = 0
    #: When the watch STARTED, as an ISO-8601 string, carried across `continue_as_new` for the same
    #: reason `polls_done` is: each turn begins with empty history and would otherwise re-stamp it.
    #:
    #: NOT `time.perf_counter`, and the difference is load-bearing. `perf_counter` is process-relative
    #: and monotonic WITHIN one process; a workflow turn can resume in a different pod, so a carried
    #: counter value would be meaningless there. It is also non-deterministic, which a replayed
    #: workflow forbids outright (DWF-DET). `ctx.current_utc_datetime` is Dapr's deterministic clock —
    #: replay-safe, identical on every replay, and the only correct source of time inside a workflow.
    started_at: str | None = None


class StageJobOutcome(BaseModel):
    """Why the watch ended. `status` is Ray's own terminal state, or None when it never reached one."""

    submission_id: str
    status: str | None = None
    polls: int = 0
    #: Seconds from the watch starting to this outcome, from the workflow's deterministic clock.
    #: `None` when the spec predates the field (a mid-rollout instance), never a fabricated 0.0.
    duration_seconds: float | None = None
    #: `succeeded` | `failed` | `abandoned` | `unnotified` — the workflow's verdict, which is not the
    #: same as Ray's status. `unnotified` is the odd one: the JOB succeeded and the data landed, but the
    #: wake-up publish could not be delivered, so the cascade stopped with healthy data behind it.
    #: status: `abandoned` means the ceiling was hit with the job still RUNNING, which is neither a
    #: success nor a job failure and must not be reported as either.
    verdict: str = "failed"


def _watch_seconds(ctx: DaprWorkflowContext, started_at: str) -> float | None:
    """Seconds from ``started_at`` to now, on the workflow's DETERMINISTIC clock.

    `ctx.current_utc_datetime` is the only legal source of time inside a workflow body: it is fed from
    recorded history, so every replay computes the same number. A wall clock or `perf_counter` here
    would return a different value on each replay and make the workflow non-deterministic, which is
    the `DWF-DET` class of defect that leaves an instance permanently stuck.

    Returns ``None`` rather than a fabricated 0.0 when the stamp is missing or unparseable — an
    instance that started before this field existed genuinely does not know how long it has run, and a
    zero would land in the histogram's lowest bucket and read as an instant stage.
    """
    from datetime import datetime

    try:
        return max(0.0, (ctx.current_utc_datetime - datetime.fromisoformat(started_at)).total_seconds())
    except (TypeError, ValueError):
        return None


def stage_run(ctx: DaprWorkflowContext, payload: dict[str, Any]) -> Generator[Any, Any, dict[str, Any]]:
    """Submit the stage job, wait for it to reach a terminal state, then wake the mover.

    The ONLY workflow in this service. Its whole reason for existing is the ordering: nothing after
    the poll loop runs until Ray says the job is done, which is the guarantee `transform.py`'s Ray
    branch assumed it had and did not.
    """
    spec = StageJobSpec.model_validate(payload)

    # BOUNDED, like the publish below. An exhausted submit used to raise into the workflow and take
    # the instance terminal FAILED with no report: nothing watching a job that may or may not have
    # been submitted, the trigger already acked in pass 1, and the only record a Dapr instance in a
    # FAILED state nobody queries.
    # FIRST TURN ONLY. After a `continue_as_new` the spec carries the id, and re-submitting would start
    # a second Ray job writing the same output dataset.
    # Stamped on the first turn only, from Dapr's deterministic clock, and carried by the
    # `continue_as_new` below — see `StageJobSpec.started_at` for why this is not `perf_counter`.
    started_at = spec.started_at or ctx.current_utc_datetime.isoformat()
    submission_id: str | None = spec.submission_id
    if submission_id is None:
        try:
            submission_id = yield ctx.call_activity(submit_stage, input=spec.model_dump(), retry_policy=ACTIVITY_RETRY)
        except Exception:
            if not ctx.is_replaying:
                log.error("medallion_stage_submit_failed", extra={"stage": spec.stage, "to_uri": spec.to_uri})
            failed = StageJobOutcome(submission_id="", status=None, polls=0, verdict="failed")
            yield ctx.call_activity(report_stage_outcome, input={"spec": spec.model_dump(), "outcome": failed.model_dump()}, retry_policy=ACTIVITY_RETRY)
            return failed.model_dump()

    # ONE poll per turn — the Monitor pattern. The loop this replaces was bounded, so it was never the
    # literal `while True` anti-pattern, but its bound WAS the history bound: 2880 polls x 30 s meant
    # one instance could carry ~5,760 events, replayed from the start on every continuation. Now each
    # turn begins with empty history and `max_polls` means only "how long are we willing to wait".
    #
    # The timer still comes FIRST. Polling before it would ask the dashboard about a submission it has
    # very likely not registered yet, spending an activity and a history entry to learn nothing — and
    # `job_status` answers None for an unknown id precisely so that race is not fatal.
    status: str | None = None
    polls = spec.polls_done
    yield ctx.create_timer(timedelta(seconds=spec.poll_interval_seconds))
    # An exhausted poll means the dashboard stayed unreachable across the whole retry policy. The JOB
    # may be running perfectly, so this is a LOST WATCH, not a job failure — reporting `failed` would
    # send an operator hunting a healthy job. Fall through to the abandoned path below, whose
    # vocabulary already means exactly "we stopped watching, the job may still land".
    try:
        status = yield ctx.call_activity(poll_stage, input={"submission_id": submission_id}, retry_policy=ACTIVITY_RETRY)
        polls = spec.polls_done + 1
    except Exception:
        if not ctx.is_replaying:
            log.error("medallion_stage_watch_lost", extra={"submission_id": submission_id, "polls": polls})
        status = None

    # STILL RUNNING and budget left: hand the rest to a fresh turn. Everything above is awaited, which
    # matters — `continue_as_new` restarts immediately and DISCARDS any task started but not awaited.
    if not _is_terminal(status) and status is not None and polls < spec.max_polls:
        ctx.continue_as_new(spec.model_copy(update={"submission_id": submission_id, "polls_done": polls, "started_at": started_at}).model_dump())
        return {}

    if not _is_terminal(status):
        # The ceiling, with the job still running. Distinct from a failure ON PURPOSE: reporting a
        # slow job as FAILED would have the mover emit a failure for work that may still land, and
        # this estate's recurring defect is exactly that — a state reported as something it is not.
        outcome = StageJobOutcome(
            submission_id=submission_id, status=status, polls=polls, verdict="abandoned", duration_seconds=_watch_seconds(ctx, started_at)
        )
        if not ctx.is_replaying:
            log.warning("medallion_stage_watch_abandoned", extra={"submission_id": submission_id, "status": status, "polls": polls})
        yield ctx.call_activity(report_stage_outcome, input={"spec": spec.model_dump(), "outcome": outcome.model_dump()}, retry_policy=ACTIVITY_RETRY)
        return outcome.model_dump()

    verdict = "succeeded" if status == _TERMINAL_OK else "failed"
    outcome = StageJobOutcome(submission_id=submission_id, status=status, polls=polls, verdict=verdict, duration_seconds=_watch_seconds(ctx, started_at))
    if not ctx.is_replaying:
        log.info("medallion_stage_job_terminal", extra={"submission_id": submission_id, "status": status, "polls": polls})

    # THE POINT OF THE WHOLE FILE: this runs only on the success branch, and only after a terminal
    # read. The mover's measure/emit/cascade path is downstream of this publish.
    if verdict == "succeeded":
        # AN ERROR BOUNDARY, because pass 1 already acked the trigger. This publish is the ONLY thing
        # that can drive the measure/emit/cascade, so an exhausted retry policy used to raise into the
        # workflow, take the instance terminal FAILED, and skip the report entirely — a job that
        # SUCCEEDED and wrote its data, with nothing anywhere saying the cascade stopped.
        #
        # An activity failure DOES raise into the generator and is catchable (unlike the replay
        # mismatch, which is raised outside it), so this is the boundary Dapr's own guidance describes
        # for compensating a failed activity — not a workaround.
        try:
            yield ctx.call_activity(publish_stage_ready, input={"spec": spec.model_dump(), "outcome": outcome.model_dump()}, retry_policy=ACTIVITY_RETRY)
        except Exception:
            # `unnotified`, not `failed`: the JOB succeeded and the data is on disk. Reporting this as
            # a job failure would send an operator to the Ray dashboard for a healthy job. What broke
            # is the wake-up, and the outcome says so.
            outcome = outcome.model_copy(update={"verdict": "unnotified"})
            if not ctx.is_replaying:
                log.error("medallion_stage_wakeup_lost", extra={"submission_id": outcome.submission_id, "to_uri": spec.to_uri})
            yield ctx.call_activity(report_stage_outcome, input={"spec": spec.model_dump(), "outcome": outcome.model_dump()}, retry_policy=ACTIVITY_RETRY)
    else:
        yield ctx.call_activity(report_stage_outcome, input={"spec": spec.model_dump(), "outcome": outcome.model_dump()}, retry_policy=ACTIVITY_RETRY)
    return outcome.model_dump()


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# Activities — everything non-deterministic lives below this line
# ─────────────────────────────────────────────────────────────────────────────────────────────────


def submit_stage(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> str:
    """Submit (or re-attach to) the Ray job, and return the submission id the poll will watch.

    Re-attach rather than re-submit is what makes a replayed activity safe: the id is deterministic in
    `(stage, token, from->to)`, so a second execution of this activity finds the first one's job.
    """
    from medallion.core.config import get_settings
    from medallion.services.ray_submit import submit_stage_job

    spec = StageJobSpec.model_validate(payload)
    settings = get_settings()
    # RETURN WHAT THE SUBMITTER POSTED — never re-derive it. The id is deterministic, so a second
    # `stage_submission_id(...)` call here reads as equivalent and is not: it has to be handed every
    # axis the submitter uses, and when `code` (the build digest) was added there and not here, this
    # activity started naming a job that does not exist. The poll then 404s, `job_status` answers
    # None, and `stage_run` abandons on its FIRST poll — a fabricated FAIL over a healthy job.
    return _run_async(
        submit_stage_job(
            settings,
            from_uri=spec.from_uri,
            to_uri=spec.to_uri,
            stage=spec.stage,
            token=spec.token,
            lineage_json=spec.lineage_json,
            # The trigger is the carrier for both — see `_build_stage_fail_event`. Passing them here
            # is what lets a FAILED job be traced back to the person whose cascade it was.
            originator=str((spec.trigger or {}).get("originator") or ""),
            project=str((spec.trigger or {}).get("project") or ""),
        )
    )


def poll_stage(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> str | None:
    """ONE status read. The workflow owns the waiting; this owns only the question."""
    import httpx

    from medallion.core.config import get_settings
    from ray_kit.submit import job_status

    settings = get_settings()
    submission_id = str(payload["submission_id"])

    async def _read() -> str | None:
        async with httpx.AsyncClient(base_url=settings.ray_address, timeout=settings.ray_request_timeout_seconds) as client:
            return await job_status(client, submission_id)

    return _run_async(_read())


def publish_stage_ready(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> None:
    """Re-publish the original trigger with `ray_job_done` set, waking the mover's measure path.

    The mover re-parses it through the same `parse_stage_trigger` guard as any bus arrival — this is
    not a privileged back channel, and a payload this activity malformed would be DROPped exactly as
    an external one would.
    """
    from dapr.aio.clients import DaprClient

    from medallion.core.config import get_settings
    from service_kit.dapr_publish import publish_event

    spec = StageJobSpec.model_validate(payload["spec"])
    outcome = StageJobOutcome.model_validate(payload["outcome"])
    settings = get_settings()

    trigger = dict(spec.trigger)
    # The flag the mover branches on. Named for what it ASSERTS — the job reached SUCCEEDED — rather
    # than "skip_submit", so a reader of the handler sees the precondition and not the shortcut.
    trigger["ray_job_done"] = True
    trigger["ray_submission_id"] = outcome.submission_id
    # THE MEASURED SPAN, handed to pass 2 so exactly one place records it. Pass 2's own elapsed time
    # covers only the measure-and-emit tail, so a multi-hour Ray stage would report as seconds; and
    # recording in BOTH the watcher and the handler would double-count every Ray stage. Omitted when
    # unmeasurable (a spec from before this field existed, mid-rollout), and the handler then falls
    # back to its own clock rather than dropping the stage from the histogram entirely.
    if outcome.duration_seconds is not None:
        trigger["ray_duration_seconds"] = outcome.duration_seconds

    async def _publish() -> None:
        # `service_kit.dapr_publish.publish_event`, never the SDK call directly: the bare
        # `publish_event` is unbounded, so a wedged sidecar hangs this activity forever — and a
        # workflow activity that never returns is a workflow that never advances. The estate has an
        # invariant test for exactly this (`test_every_publish_goes_through_the_timeout_wrapper`),
        # which is what caught the first draft of this function.
        async with DaprClient() as client:
            await publish_event(
                client,
                timeout_seconds=settings.publish_timeout_seconds,
                pubsub_name=settings.pubsub,
                # The mover's OWN subscription topic: this wakes the same handler the original trigger
                # reached, so the measure/emit/cascade path is the one already under test — not a fork.
                topic_name=settings.sub_topic,
                data=json.dumps(trigger),
                data_content_type="application/json",
            )

    _run_async(_publish())
    log.info("medallion_stage_ready_published", extra={"submission_id": outcome.submission_id, "topic": settings.sub_topic})


def report_stage_outcome(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> None:
    """Record a job that FAILED, was STOPPED, or outlived the watch.

    A terminal-bad job publishes NOTHING: waking the mover would run the measure against a dataset the
    job did not write, which is the very defect this workflow exists to close. The record is the log
    line and the counter; the lineage reconciler is what reconciles storage truth (A13), and S5's
    compensation slice is where a failed promotion grows a saga.
    """
    outcome = StageJobOutcome.model_validate(payload["outcome"])
    spec = StageJobSpec.model_validate(payload["spec"])

    # THE FAILURE REACHES THE GRAPH, not just this log line. S1 regressed that: before it, a failed job
    # made `measure_stage` raise and the handler's except emitted a FAIL RunEvent with an errorMessage
    # facet. After S1, pass 1 acks the moment the watcher is dispatched, so a job that then FAILS left
    # nothing anywhere — no FAIL in the graph, no failed feed row, nothing the notifications plane can
    # target, and a run that simply never appears to end.
    #
    # Best-effort and suppressed, matching the handler's own FAIL emit (I8): an activity that RAISES is
    # retried and can end FAILED, so a lineage outage would leave the workflow unable to finish
    # reporting a failure — strictly worse than the silence this replaces.
    reason = (
        f"the Ray stage job {outcome.submission_id} ended {outcome.status or 'UNKNOWN'} after {outcome.polls} poll(s)"
        if outcome.verdict == "failed"
        else f"the watch was abandoned after {outcome.polls} poll(s) with the job still {outcome.status or 'UNKNOWN'}"
    )
    # WHY it failed, not just THAT it did. Ray answers `error_type`, `message` and `driver_exit_code`
    # on the same response the poll already reads, and `RayJob` has declared all three since the OOM
    # work — nothing here ever asked. Without them every stage failure in the graph and in a person's
    # inbox reads identically (an id, a status, a poll count), and diagnosis means a Ray head pod that
    # may already be gone and whose logs nothing ships. `driver_exit_code` 137 is SIGKILL — a host-RAM
    # OOM — which is the one cause an operator can act on without reading anything else.
    #
    # Only on the `failed` verdict: an ABANDONED watch means the job was still RUNNING, so Ray has no
    # cause to report and asking for one would invite a misleading answer.
    if (
        outcome.verdict == "failed"
        and (cause := _read_stage_failure(outcome.submission_id)) is not None
        and (summary := cause.summary(_STAGE_FAIL_MESSAGE_CAP))
    ):
        reason = f"{reason} — {summary}"
    # NOT bare. The log.error immediately below still prints when this fails, and this activity's own
    # docstring promises "THE FAILURE REACHES THE GRAPH, not just this log line" — so a swallowed emit
    # makes that line assert a graph write that never happened.
    # The verdict is a fact only this application knows: the orchestrator RETURNS failure rather than
    # raising, so Dapr's own execution counter records `status="success"` for a run that died.
    record_stage_outcome(outcome.verdict, duration_seconds=outcome.duration_seconds)

    # THE SPAN THAT ALREADY EXISTS — daprd's `activity||report_stage_outcome` and the SDK's
    # `activity: report_stage_outcome` are both correctly parented into the run's trace, and neither
    # knows the transition, the submission or the verdict. Opening a third span on the same hop would
    # make the trace view worse; setting attributes on the live one costs nothing.
    #
    # The submission id rides the SPAN, never a metric label: it is per-run and unbounded, which is the
    # series-per-object-id rule §6 records this estate breaking once already. The verdict is a closed
    # set, so it is safe in both places and appears in both.
    span = trace.get_current_span()
    span.set_attribute("lance.medallion.verdict", outcome.verdict)
    span.set_attribute("lance.medallion.submission_id", outcome.submission_id)
    if outcome.verdict != "succeeded":
        # daprd marks the ORCHESTRATION span when an activity RAISES; this one RETURNS its verdict, so
        # without this every activity span stays UNSET — including the ones whose own sidecar metric
        # says `failed`. Trace-based error search would show a clean estate while the cascade dies.
        span.set_status(Status(StatusCode.ERROR, f"stage {outcome.verdict}: {outcome.submission_id}"))

    with best_effort("stage_fail_event", token=spec.token, submission_id=outcome.submission_id):
        _publish_fail_event(_build_stage_fail_event(spec, outcome, reason), spec)

    log.error(
        "medallion_stage_job_not_completed",
        extra={
            "submission_id": outcome.submission_id,
            "status": outcome.status,
            "verdict": outcome.verdict,
            "polls": outcome.polls,
            "stage": spec.stage,
            "to_uri": spec.to_uri,
        },
    )


#: How much of Ray's driver message may ride a FAIL event. The message is UNBOUNDED upstream — it can
#: be an entire driver traceback — and this string becomes the `errorMessage` facet on an event
#: published through the claim-check funnel, which WARNs past 64 KiB and REFUSES past 900 KiB
#: (`service_kit/dapr_publish.py`). Bounding it HERE keeps an upstream string from deciding the size of
#: a governed event, and keeps the refusal a claim-check violation rather than a truncated traceback.
_STAGE_FAIL_MESSAGE_CAP = 800


def _read_stage_failure(submission_id: str) -> Any:
    """Ray's stated cause for a terminal-bad job, or ``None`` when it cannot be read.

    SUPPRESSED, for the same reason `_publish_fail_event` is: this runs on the failure-reporting path,
    so an unreachable dashboard raising here would leave the workflow unable to finish reporting a
    failure — strictly worse than the missing detail it replaces. A cause is an enrichment; the FAIL
    event goes out either way.

    A separate GET rather than a widened `poll_stage`: that activity's return value is recorded in
    workflow history on every tick, so changing its shape breaks replay for in-flight instances, and
    no poll needs a traceback. This costs one extra read, only when a job has actually failed.
    """
    import httpx

    from medallion.core.config import get_settings
    from ray_kit.submit import job_failure

    settings = get_settings()

    async def _read() -> Any:
        async with httpx.AsyncClient(base_url=settings.ray_address, timeout=settings.ray_request_timeout_seconds) as client:
            return await job_failure(client, submission_id)

    # Best-effort by design — a Ray outage must not stop the failure being reported — but the reason a
    # FAIL carries no detail is worth knowing, so the swallow is no longer silent.
    with best_effort("read_stage_failure", submission_id=submission_id):
        return _run_async(_read())
    return None


def _build_stage_fail_event(spec: StageJobSpec, outcome: StageJobOutcome, reason: str) -> dict[str, Any]:
    """A FAIL RunEvent for a stage whose Ray job did not complete.

    Same shape the handler's own failure path builds (`transform.py`): a bare output (the WROTE edge,
    no version — nothing was written) plus the errorMessage facet. Deterministic on the trigger's
    token, so a redelivered trigger MERGEs onto the same run rather than forking a second failure.
    """
    from medallion.core.config import get_settings, project_namespace
    from medallion.schemas.events import build_run_event

    settings = get_settings()
    trigger = spec.trigger or {}
    # PROJECT-QUALIFIED, exactly as the COMPLETE path names them (`transform.py` emits the
    # `project_namespace(...)` locals). They diverged, and the divergence was silent in the worst way:
    # delivery re-derives each recipient's visibility against `table:<output name>`, so a tenant run's
    # FAIL naming the raw `silver$features` while every grant named `acme-silver$features` counted
    # EVERY recipient hidden — the audience computed correctly, then discarded whole. The author was
    # not told about their own failed run, and neither was any watcher.
    #
    # Empty project returns the env value unchanged, so single-tenant stays byte-identical.
    project = trigger.get("project") or ""
    from_namespace = project_namespace(project, settings.from_namespace)
    from_dataset = project_namespace(project, settings.from_dataset)
    to_namespace = project_namespace(project, settings.to_namespace)
    to_dataset = project_namespace(project, settings.to_dataset)
    return build_run_event(
        operation=settings.operation,
        author=settings.author,
        job_namespace=settings.job_namespace,
        inputs=[(from_namespace, from_dataset)],
        output_namespace=to_namespace,
        output_name=to_dataset,
        token=spec.token or trigger.get("token"),
        project=trigger.get("project") or None,
        # THE TRIGGER IS THE CARRIER. By the time a stage fails, the request that started the cascade is
        # long gone — the head is the last place the verified subject existed, so it rides the trigger
        # beside `token` and `project` rather than being re-derived from anything here.
        originator=trigger.get("originator") or None,
        event_type="FAIL",
        error_message=f"{reason} ({outcome.verdict})",
    )


def _publish_fail_event(event: dict[str, Any], spec: StageJobSpec) -> None:
    """Publish the FAIL through the lineage outbox — the same durability the handler's emit has.

    Split from the builder so a test can substitute it: the interesting assertion is WHAT reaches the
    graph, and standing up an outbox to observe that would test the outbox instead.
    """
    from dapr.aio.clients import DaprClient

    from medallion.core.config import get_settings
    from service_kit.lakehouse import outbox

    settings = get_settings()

    async def _send() -> None:
        async with DaprClient() as client:
            await outbox.publish_lineage_with_outbox(
                client,
                outbox_uri=settings.lineage_outbox_uri,
                storage_options=settings.storage_options(),
                run_id=event["run"]["runId"],
                event_json=json.dumps(event),
                pubsub_name=settings.pubsub,
                topic_name=settings.lineage_topic,
                timeout_seconds=settings.publish_timeout_seconds,
            )

    _run_async(_send())


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────────────────────────────────────────────────────────

_TERMINAL_OK: Final = "SUCCEEDED"
_TERMINAL_BAD: Final = ("FAILED", "STOPPED")


def _is_terminal(status: str | None) -> bool:
    """Terminality, decided WITHOUT importing ray_kit into the workflow body.

    The workflow module is imported by the replay path; keeping this a pure comparison over two
    literals means the body has no import-time behaviour to be non-deterministic about. The literals
    are asserted equal to `ray_kit`'s in the tests, so the duplication cannot drift silently.
    """
    return status == _TERMINAL_OK or status in _TERMINAL_BAD


def _run_async(coro: Any) -> Any:  # noqa: ANN401 — mirrors ingest.workflow._run_async
    """Run a coroutine from an activity's worker thread.

    Activities execute on the workflow worker's threads, which have no running event loop, so
    `asyncio.run` is correct here and would be a bug inside the FastAPI handler.
    """
    import asyncio

    return asyncio.run(coro)


def register(runtime: wf.WorkflowRuntime) -> None:
    """Register everything with the runtime — one place, so nothing is silently unregistered."""
    for w in WORKFLOWS:
        runtime.register_workflow(w)
    for a in ACTIVITIES:
        runtime.register_activity(a)


# ─────────────────────────────────────────────────────────────────────────────────────────────────
# The TRAIN watcher — the lane that had none
# ─────────────────────────────────────────────────────────────────────────────────────────────────
#
# `stage_run` above watches a stage job. The train lane had no watcher at all: `handle_train_trigger`
# submits and acks (D2 — training is hours of GPU, and holding a Dapr ack across it is the race A13
# deleted), and the JOB emits its own OpenLineage lifecycle.
#
# That covers a job that RUNS and fails. It does not cover one that dies before emitting anything — a
# bad image, an OOM during runtime-env setup, an entrypoint the image lacks (`exit 2`). Ray knows;
# nobody else does; and the person who asked is told nothing, ever.
#
# This watcher is deliberately THINNER than `stage_run`: it submits nothing (the consumer already did)
# and it wakes nothing (there is no next tier). It only watches, and speaks when the job died.


class TrainJobSpec(BaseModel):
    """What the watcher needs — the identity of the job and of the person it is for.

    Deliberately not the whole trigger: a workflow input is persisted on every checkpoint, and the
    training config is a claim-check payload that has no business being written to the state store
    once per poll.
    """

    token: str
    model: str
    submission_id: str
    #: The verified human `/train` captured, carried the whole way from the door. Without it the FAIL
    #: below names nobody and the notification plane discards it by its own rule.
    originator: str = ""
    #: The configured single-tenant project the model registry lives in — WATCH's key.
    project: str = ""
    poll_interval_seconds: int = POLL_INTERVAL_SECONDS
    max_polls: int = MAX_POLLS
    #: Carried across `continue_as_new`; a fresh turn starts with empty history and would otherwise
    #: count from zero and never reach the ceiling.
    polls_done: int = 0


class TrainJobOutcome(BaseModel):
    """Why the watch ended. `verdict` is `succeeded` | `failed` | `abandoned`."""

    submission_id: str
    status: str | None = None
    polls: int = 0
    verdict: str = "failed"


def train_run(ctx: DaprWorkflowContext, payload: dict[str, Any]) -> Generator[Any, Any, dict[str, Any]]:
    """Watch a submitted training job, and report it ONLY if it died.

    A SUCCEEDED job is silent here on purpose: the job emits its own COMPLETE, and a second success
    event would fork a duplicate run for one training run — so the graph would answer "what produced
    this model" twice.
    """
    spec = TrainJobSpec.model_validate(payload)

    status: str | None = None
    polls = spec.polls_done
    yield ctx.create_timer(timedelta(seconds=spec.poll_interval_seconds))
    # An exhausted poll means the dashboard stayed unreachable across the whole retry policy. The JOB
    # may be training perfectly, so this is a LOST WATCH — it falls through to `abandoned`, whose
    # vocabulary already means "we stopped watching; it may still land". Reporting `failed` would be a
    # lie about somebody's four-hour run.
    try:
        status = yield ctx.call_activity(poll_train, input={"submission_id": spec.submission_id}, retry_policy=ACTIVITY_RETRY)
        polls = spec.polls_done + 1
    except Exception:
        if not ctx.is_replaying:
            log.error("medallion_train_watch_lost", extra={"submission_id": spec.submission_id, "polls": polls})
        status = None

    if not _is_terminal(status) and status is not None and polls < spec.max_polls:
        ctx.continue_as_new(spec.model_copy(update={"polls_done": polls}).model_dump())
        return {}

    if not _is_terminal(status):
        # The ceiling, or a lost watch. NOT a failure: a training job still running at the ceiling is
        # alive and may yet land, and reporting it as dead sends somebody hunting a healthy run.
        outcome = TrainJobOutcome(submission_id=spec.submission_id, status=status, polls=polls, verdict="abandoned")
        if not ctx.is_replaying:
            log.warning("medallion_train_watch_abandoned", extra={"submission_id": spec.submission_id, "status": status, "polls": polls})
        return outcome.model_dump()

    verdict = "succeeded" if status == _TERMINAL_OK else "failed"
    outcome = TrainJobOutcome(submission_id=spec.submission_id, status=status, polls=polls, verdict=verdict)
    if not ctx.is_replaying:
        log.info("medallion_train_job_terminal", extra={"submission_id": spec.submission_id, "status": status, "polls": polls})

    if verdict == "failed":
        yield ctx.call_activity(report_train_outcome, input={"spec": spec.model_dump(), "outcome": outcome.model_dump()}, retry_policy=ACTIVITY_RETRY)
    return outcome.model_dump()


def poll_train(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> str | None:
    """ONE status read of the training job. The workflow owns the waiting."""
    import httpx

    from medallion.core.config import get_settings
    from ray_kit.submit import job_status

    settings = get_settings()
    submission_id = str(payload["submission_id"])

    async def _read() -> str | None:
        async with httpx.AsyncClient(base_url=settings.ray_address, timeout=settings.ray_request_timeout_seconds) as client:
            return await job_status(client, submission_id)

    return _run_async(_read())


def report_train_outcome(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> None:
    """Put a dead training job in the graph, named for the person who asked for it.

    THE RUN ID IS THE JOB'S OWN — `run_id_for(f"train-{token}")`, byte-identical to what
    `scripts/ray_train_job.py` stamps. That is the load-bearing choice:

    * if the job DID emit its own FAIL, this merges onto the same run rather than forking a second
      failure for one training run;
    * if the job died before emitting anything, this is the only record, and it lands under the id
      every other surface already links to.

    Emitting under a fresh id would answer "what happened to this model" with a duplicate.

    Best-effort and suppressed, matching the stage reporter: an activity that RAISES is retried and can
    end FAILED, so a lineage outage would leave the workflow unable to finish reporting a failure —
    strictly worse than the silence it replaces.
    """
    spec = TrainJobSpec.model_validate(payload["spec"])
    outcome = TrainJobOutcome.model_validate(payload["outcome"])

    reason = f"the Ray training job {outcome.submission_id} ended {outcome.status or 'UNKNOWN'} after {outcome.polls} poll(s)"
    record_train_outcome(outcome.verdict)

    with best_effort("train_fail_event", token=spec.token, submission_id=outcome.submission_id):
        _publish_train_fail(spec, reason)

    log.error(
        "medallion_train_job_not_completed",
        extra={"submission_id": outcome.submission_id, "status": outcome.status, "polls": outcome.polls, "model": spec.model},
    )


def _publish_train_fail(spec: TrainJobSpec, reason: str) -> None:
    """The FAIL RunEvent for a training job that died, through the lineage outbox."""
    from dapr.aio.clients import DaprClient

    from medallion.core.config import get_settings
    from medallion.schemas.events import _PRODUCER
    from service_kit.lakehouse import outbox
    from service_kit.openlineage import custom_facet, run_id_for

    settings = get_settings()
    run_id = run_id_for(f"train-{spec.token}")
    lance: dict[str, Any] = {"operation": "training", "token": spec.token}
    # THREE fields decide whether anybody hears, and this comment named only two — which is exactly how
    # the third went missing. `originator` because the job authors as `service-trainer` and
    # `enforce_author` overwrites anything else; `project` because omitting it skips the watcher loop
    # entirely; and `author`, below, because `notifiable()` returns None on an event with no verified
    # author BEFORE `originator_subject()` is ever read. Without it the other two are threaded all the
    # way from /train and then discarded at the last link — and because this goes out on the BUS through
    # the outbox, neither `enforce_author` nor the HTTP door's checks run, and the plane SUCCESS-acks the
    # undeliverable event. Nothing reports the loss.
    if spec.originator:
        lance["originator"] = spec.originator
    if spec.project:
        lance["project"] = spec.project
    # NOT routed through `build_run_event`, deliberately, even though that helper stamps `author` for
    # free. It derives the runId from `(project+)operation+token`, and THE RUN ID HERE IS THE JOB'S OWN —
    # `run_id_for(f"train-{token}")`, byte-identical to `scripts/ray_train_job.py`. That is what merges
    # the watcher's FAIL onto the job's run instead of forking a second failure for one training run.
    # Using the helper would produce `training-<token>` (NUL-qualified by project), a different id, and
    # the graph would answer "what happened to this model" with a duplicate.
    event = {
        "eventType": "FAIL",
        "eventTime": datetime.now(UTC).isoformat(),
        "producer": "rask://medallion/train-watcher",
        "job": {"namespace": settings.job_namespace, "name": f"train_{spec.model}"},
        "run": {
            "runId": run_id,
            "facets": {
                "lance": lance,
                "author": custom_facet(_PRODUCER, name=settings.author, sub=settings.author),
                "errorMessage": {"message": reason, "programmingLanguage": "PYTHON"},
            },
        },
        "outputs": [{"namespace": settings.models_namespace, "name": f"{settings.models_namespace}${spec.model}"}],
    }

    async def _send() -> None:
        async with DaprClient() as client:
            await outbox.publish_lineage_with_outbox(
                client,
                outbox_uri=settings.lineage_outbox_uri,
                storage_options=settings.storage_options(),
                run_id=run_id,
                event_json=json.dumps(event),
                pubsub_name=settings.pubsub,
                topic_name=settings.lineage_topic,
                timeout_seconds=settings.publish_timeout_seconds,
            )

    _run_async(_send())


# --------------------------------------------------------------------------- #
# S3/S4 — the quality gate's third answer: a held promotion a person can release
# --------------------------------------------------------------------------- #


class PromotionSpec(BaseModel):
    """One held promotion, carrying everything needed to resume it (B1: pointers, never payload)."""

    token: str
    project: str = ""
    from_namespace: str
    from_dataset: str
    to_namespace: str
    to_dataset: str
    #: Where the next stage listens; empty means terminal — an approval then records the decision and
    #: promotes nothing, which is the honest outcome for the last tier.
    pub_topic: str = ""
    #: WHICH assertions failed. On the SPEC, set by the caller from the run's own result — never read
    #: from settings inside the body, where a value could change under a running instance.
    reasons: list[str] = Field(default_factory=list)
    #: Who may answer. Resolved at hold time, because by the time this runs there is no request left
    #: to derive an identity from. Empty means nobody can be asked — which BLOCKS, never promotes.
    approver: str = ""
    originator: str = ""
    approval_hours: int = 72
    #: The version the hold was taken on. The resume must publish THIS one — a later commit may have
    #: landed while the approver was deciding, and publishing that would ship a version nobody
    #: reviewed. 0 means the hold predates a tag-driven cascade and can only resume by trigger.
    version: int = 0


def promotion_review(ctx: DaprWorkflowContext, payload: dict[str, Any]) -> Generator[Any, Any, dict[str, Any]]:
    """Hold a promotion, ask a person, resume the cascade only on an explicit yes.

    A workflow rather than a table and a cron because the wait is hours-to-days and must survive every
    pod restart in between: an approval is a plan worth resuming, since losing it loses a decision.

    EVERY path that is not an explicit clean verdict or an explicit approval BLOCKS. A held batch that
    reached promotion by falling off the end of a branch would be the gate failing open, which is
    worse than the permanent DROP this replaces.
    """
    spec = PromotionSpec.model_validate(payload)

    # Resolved by an ACTIVITY, and first: a threshold read in the body changes under a running
    # instance and makes replay disagree with the original turn.
    policy = yield ctx.call_activity(resolve_review_policy, input=spec.model_dump(), retry_policy=ACTIVITY_RETRY)
    verdict = policy.get("verdict", "block")

    if verdict == "block":
        yield ctx.call_activity(
            emit_promotion_outcome,
            input={"spec": spec.model_dump(), "outcome": {"status": "BLOCKED", "reasons": spec.reasons}},
            retry_policy=ACTIVITY_RETRY,
        )
        return {"status": "BLOCKED", "decided_by": None, "reasons": spec.reasons}

    decided_by: str | None = None
    if verdict == "review":
        # ASK BEFORE WAITING, and treat an unsendable ask as a refusal: parking on an event nobody was
        # told about is an outage wearing a pause.
        asked = yield ctx.call_activity(request_approval, input=spec.model_dump(), retry_policy=ACTIVITY_RETRY)
        if not asked:
            yield ctx.call_activity(
                emit_promotion_outcome,
                input={"spec": spec.model_dump(), "outcome": {"status": "BLOCKED", "reasons": [*spec.reasons, "no reachable approver"]}},
                retry_policy=ACTIVITY_RETRY,
            )
            return {"status": "BLOCKED", "decided_by": None, "reasons": [*spec.reasons, "no reachable approver"]}

        approval = ctx.wait_for_external_event("promotion_decision")
        deadline = ctx.create_timer(timedelta(hours=spec.approval_hours))
        winner = yield wf.when_any([approval, deadline])

        if winner is deadline:
            yield ctx.call_activity(
                emit_promotion_outcome,
                input={"spec": spec.model_dump(), "outcome": {"status": "EXPIRED", "reasons": spec.reasons}},
                retry_policy=ACTIVITY_RETRY,
            )
            return {"status": "EXPIRED", "decided_by": None, "reasons": spec.reasons}

        decision = approval.get_result() or {}
        decided_by = decision.get("subject")
        # The DOOR authenticates; this only refuses a decision that names nobody, or one whose subject
        # is the producing service approving its own output.
        if not decided_by or decided_by == settings_author_marker(spec) or not decision.get("approved"):
            status = "REJECTED" if decided_by and decision.get("approved") is False else "BLOCKED"
            yield ctx.call_activity(
                emit_promotion_outcome,
                input={"spec": spec.model_dump(), "outcome": {"status": status, "decided_by": decided_by, "reasons": spec.reasons}},
                retry_policy=ACTIVITY_RETRY,
            )
            return {"status": status, "decided_by": decided_by, "reasons": spec.reasons}

    if spec.pub_topic:
        yield ctx.call_activity(publish_promotion, input=spec.model_dump(), retry_policy=ACTIVITY_RETRY)
    yield ctx.call_activity(
        emit_promotion_outcome,
        input={"spec": spec.model_dump(), "outcome": {"status": "PROMOTED", "decided_by": decided_by}},
        retry_policy=ACTIVITY_RETRY,
    )
    return {"status": "PROMOTED", "decided_by": decided_by, "reasons": spec.reasons}


def settings_author_marker(spec: PromotionSpec) -> str:
    """The producing identity, which must never approve its own promotion.

    Pure and derived from the spec alone so the workflow body stays deterministic — it reads no
    settings and no clock.
    """
    return f"service:{spec.from_namespace}-to-{spec.to_namespace}"


def resolve_review_policy(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Split a hold into corrupt / unusual / clean, and FAIL CLOSED on anything else.

    `blob_resolves` and a null key column are structural: the data is wrong and no approval makes it
    right. Review is opt-in per deployment, because an estate with nobody to ask must keep the old
    behaviour rather than parking promotions on an event no one will raise — and "keep the old
    behaviour" means BLOCK, which is what the gate did before, not promote.
    """
    from medallion.core.config import get_settings

    spec = PromotionSpec.model_validate(payload)
    settings = get_settings()
    if not spec.reasons:
        return {"verdict": "promote", "reasons": []}
    if any(reason in _STRUCTURAL_FAILURES for reason in spec.reasons):
        return {"verdict": "block", "reasons": spec.reasons}
    return {"verdict": "review" if settings.quality_review_enabled else "block", "reasons": spec.reasons}


def request_approval(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> bool:
    """Tell the approver there is something to decide. Returns whether the ask went out.

    Published DIRECTLY rather than through `process_control_emitter()`: the mover never sets a
    process emitter, so that path is a no-op here and the ask would be silently swallowed — the exact
    class of defect this feature exists to fix.
    """
    from dapr.aio.clients import DaprClient

    from medallion.core.config import get_settings
    from service_kit.control_events import CONTROL_TOPIC, CatalogControlEvent
    from service_kit.dapr_publish import publish_event

    spec = PromotionSpec.model_validate(payload)
    if not spec.approver:
        log.warning("medallion_promotion_unapprovable", extra={"dataset": spec.to_dataset, "token": spec.token})
        return False

    settings = get_settings()
    event = CatalogControlEvent(
        action="promotion_review_requested",
        object_type="table",
        object_id=f"table:{_qualified(spec.project, spec.to_dataset)}",
        actor=None,
        extra={"subject": f"user:{spec.approver}", "reasons": spec.reasons, "project": spec.project, "token": spec.token},
    )

    async def _publish() -> None:
        async with DaprClient() as client:
            await publish_event(
                client,
                timeout_seconds=settings.publish_timeout_seconds,
                pubsub_name=settings.pubsub,
                topic_name=CONTROL_TOPIC,
                data=event.model_dump_json(),
                data_content_type="application/json",
            )

    try:
        _run_async(_publish())
    except Exception:
        log.exception("medallion_promotion_ask_failed", extra={"dataset": spec.to_dataset, "token": spec.token})
        return False
    log.info("medallion_promotion_review_requested", extra={"dataset": spec.to_dataset, "approver": spec.approver})
    return True


def _resume_publish(
    *,
    catalog_url: str,
    table_id: str,
    version: int,
    key_column: str,
    accept_assertions: list[str],
    app_token: str,
    service_identity: str,
    token: str | None,
    timeout_seconds: float,
) -> None:
    """The tag move an approval resumes with. A seam so the activity is testable without a catalog."""
    from medallion.services.catalog_register import publish_stage_output

    publish_stage_output(
        catalog_url=catalog_url,
        table_id=table_id,
        version=version,
        key_column=key_column,
        accept_assertions=accept_assertions,
        app_token=app_token,
        service_identity=service_identity,
        token=token,
        timeout_seconds=timeout_seconds,
    )


def publish_promotion(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> None:
    """Resume the promotion a person approved.

    Under a tag-driven cascade the resume MOVES THE TAG — the next-stage trigger no longer exists, so
    firing one would wake nothing. And it cannot be an ordinary re-publish: the version still fails the
    assertion it failed the first time, so the door would refuse it for exactly the reason the approver
    just overruled. It publishes with the findings the review ACCEPTED, which is the door built for
    this — named findings only, and structural ones never.

    Falls back to the trigger when the spec carries no version, which is a hold taken before the
    cascade moved to publishing. That is a migration case with a real answer, not a silent fallback.
    """
    from dapr.aio.clients import DaprClient

    from medallion.core.config import get_settings
    from service_kit.dapr_publish import publish_event

    spec = PromotionSpec.model_validate(payload)
    settings = get_settings()
    if spec.version and settings.cascade_via_publish:
        _resume_publish(
            catalog_url=settings.catalog_url,
            table_id=spec.to_dataset,
            version=spec.version,
            key_column=settings.quality_key_column,
            accept_assertions=list(spec.reasons),
            app_token=settings.app_api_token,
            service_identity=settings.catalog_service_identity,
            token=settings.catalog_token,
            timeout_seconds=settings.publish_timeout_seconds,
        )
        log.info("medallion_promotion_published", extra={"dataset": spec.to_dataset, "version": spec.version, "accepted": spec.reasons})
        return
    trigger: dict[str, Any] = {"token": spec.token, "dataset": spec.to_dataset, "namespace": spec.to_namespace}
    if spec.project:
        trigger["project"] = spec.project
    if spec.originator:
        trigger["originator"] = spec.originator

    async def _publish() -> None:
        async with DaprClient() as client:
            await publish_event(
                client,
                timeout_seconds=settings.publish_timeout_seconds,
                pubsub_name=settings.pubsub,
                topic_name=spec.pub_topic,
                data=json.dumps(trigger),
                data_content_type="application/json",
            )

    _run_async(_publish())
    log.info("medallion_promotion_published", extra={"dataset": spec.to_dataset, "topic": spec.pub_topic})


def emit_promotion_outcome(ctx: WorkflowActivityContext, payload: dict[str, Any]) -> None:
    """Record the decision where the AUDIT lives.

    Workflow history is retention-bounded (7d COMPLETED / 30d FAILED), so it is a cache; lineage is
    the durable record. A decision surviving only in history is one the estate forgets.
    """
    from dapr.aio.clients import DaprClient

    from medallion.core.config import get_settings
    from medallion.schemas.events import build_run_event
    from service_kit.lakehouse import outbox

    spec = PromotionSpec.model_validate(payload["spec"])
    outcome = payload["outcome"]
    settings = get_settings()
    # PROMOTED | REJECTED | BLOCKED — a closed set decided by the review activity, never caller input.
    record_promotion_outcome(str(outcome["status"]))
    approved = outcome["status"] == "PROMOTED"
    event = build_run_event(
        operation=settings.operation,
        author=settings.author,
        job_namespace=settings.job_namespace,
        inputs=[(spec.from_namespace, _qualified(spec.project, spec.from_dataset))],
        output_namespace=spec.to_namespace,
        output_name=_qualified(spec.project, spec.to_dataset),
        token=f"{spec.token}:promotion-{outcome['status'].lower()}",
        project=spec.project or None,
        originator=spec.originator or None,
        event_type="COMPLETE" if approved else "FAIL",
        error_message=None if approved else f"promotion {outcome['status'].lower()}: {', '.join(outcome.get('reasons') or []) or 'quality review'}",
    )
    lance = event["run"]["facets"].setdefault("lance", {})
    lance["promotion_status"] = outcome["status"]
    if outcome.get("decided_by"):
        lance["promotion_decided_by"] = outcome["decided_by"]

    async def _publish() -> None:
        async with DaprClient() as client:
            await outbox.publish_lineage_with_outbox(
                client,
                outbox_uri=settings.lineage_outbox_uri,
                storage_options=settings.storage_options(),
                run_id=event["run"]["runId"],
                event_json=json.dumps(event),
                pubsub_name=settings.pubsub,
                topic_name=settings.lineage_topic,
                timeout_seconds=settings.publish_timeout_seconds,
            )

    # emit_promotion_outcome logged NOTHING at all on this path, while its docstring calls workflow
    # history "a cache; lineage is the durable record" — a dropped publish silently emptied the record.
    with best_effort("promotion_outcome"):
        _run_async(_publish())


def _qualified(project: str, dataset: str) -> str:
    """Project-qualify a dataset id, or return it unchanged for a single-tenant run.

    The A18 defect this file already documents: an unqualified name against tenant-qualified grants
    counts every recipient HIDDEN, so the audience is computed correctly and then discarded whole.
    """
    if not project or dataset.startswith(f"{project}-"):
        return dataset
    return f"{project}-{dataset}"


# The registration tuples live at the END of the module, after every symbol they name. They used to
# sit mid-file, which worked only for as long as nothing was defined below them — adding the train
# watcher turned that into a NameError at import, i.e. a service that cannot start.
WORKFLOWS = (stage_run, train_run, promotion_review)

ACTIVITIES = (
    submit_stage,
    poll_stage,
    publish_stage_ready,
    report_stage_outcome,
    poll_train,
    report_train_outcome,
    resolve_review_policy,
    request_approval,
    publish_promotion,
    emit_promotion_outcome,
)
