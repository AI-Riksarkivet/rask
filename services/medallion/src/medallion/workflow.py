"""S1 — the stage transform as a Dapr Workflow, so the MEASURE waits for the JOB.

THE DEFECT THIS CLOSES (`open_medallion_workflow.md` §7 S1, `transform.py:333`). The Ray branch of
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
lane (in-process, HTR, Ray), and forking it per lane is how the lanes drift apart.

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
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Final

import dapr.ext.workflow as wf
from pydantic import BaseModel, Field


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


class StageJobOutcome(BaseModel):
    """Why the watch ended. `status` is Ray's own terminal state, or None when it never reached one."""

    submission_id: str
    status: str | None = None
    polls: int = 0
    #: `succeeded` | `failed` | `abandoned` — the workflow's verdict, which is not the same as Ray's
    #: status: `abandoned` means the ceiling was hit with the job still RUNNING, which is neither a
    #: success nor a job failure and must not be reported as either.
    verdict: str = "failed"


def stage_run(ctx: DaprWorkflowContext, payload: dict[str, Any]) -> Generator[Any, Any, dict[str, Any]]:
    """Submit the stage job, wait for it to reach a terminal state, then wake the mover.

    The ONLY workflow in this service. Its whole reason for existing is the ordering: nothing after
    the poll loop runs until Ray says the job is done, which is the guarantee `transform.py`'s Ray
    branch assumed it had and did not.
    """
    spec = StageJobSpec.model_validate(payload)

    submission_id: str = yield ctx.call_activity(submit_stage, input=spec.model_dump(), retry_policy=ACTIVITY_RETRY)

    status: str | None = None
    polls = 0
    # BOUNDED, not `while True` (DWF-DET-013). The bound is the history bound; S2 replaces it with
    # `continue_as_new`, at which point the loop can be indefinite because history resets each turn.
    for attempt in range(spec.max_polls):
        # The timer comes FIRST. Polling before it would ask the dashboard about a submission it has
        # very likely not registered yet, spending an activity and a history entry to learn nothing —
        # and `job_status` answers None for an unknown id precisely so that race is not fatal.
        yield ctx.create_timer(timedelta(seconds=spec.poll_interval_seconds))
        status = yield ctx.call_activity(poll_stage, input={"submission_id": submission_id}, retry_policy=ACTIVITY_RETRY)
        polls = attempt + 1
        if _is_terminal(status):
            break

    if not _is_terminal(status):
        # The ceiling, with the job still running. Distinct from a failure ON PURPOSE: reporting a
        # slow job as FAILED would have the mover emit a failure for work that may still land, and
        # this estate's recurring defect is exactly that — a state reported as something it is not.
        outcome = StageJobOutcome(submission_id=submission_id, status=status, polls=polls, verdict="abandoned")
        if not ctx.is_replaying:
            log.warning("medallion_stage_watch_abandoned", extra={"submission_id": submission_id, "status": status, "polls": polls})
        yield ctx.call_activity(report_stage_outcome, input={"spec": spec.model_dump(), "outcome": outcome.model_dump()}, retry_policy=ACTIVITY_RETRY)
        return outcome.model_dump()

    verdict = "succeeded" if status == _TERMINAL_OK else "failed"
    outcome = StageJobOutcome(submission_id=submission_id, status=status, polls=polls, verdict=verdict)
    if not ctx.is_replaying:
        log.info("medallion_stage_job_terminal", extra={"submission_id": submission_id, "status": status, "polls": polls})

    # THE POINT OF THE WHOLE FILE: this runs only on the success branch, and only after a terminal
    # read. The mover's measure/emit/cascade path is downstream of this publish.
    if verdict == "succeeded":
        yield ctx.call_activity(publish_stage_ready, input={"spec": spec.model_dump(), "outcome": outcome.model_dump()}, retry_policy=ACTIVITY_RETRY)
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
    from medallion.services.ray_submit import stage_submission_id, submit_stage_job

    spec = StageJobSpec.model_validate(payload)
    settings = get_settings()
    _run_async(
        submit_stage_job(
            settings,
            from_uri=spec.from_uri,
            to_uri=spec.to_uri,
            stage=spec.stage,
            token=spec.token,
            lineage_json=spec.lineage_json,
        )
    )
    return stage_submission_id(spec.stage, spec.token, spec.from_uri, spec.to_uri)


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
    from dapr.clients import DaprClient

    from medallion.core.config import get_settings

    spec = StageJobSpec.model_validate(payload["spec"])
    outcome = StageJobOutcome.model_validate(payload["outcome"])
    settings = get_settings()

    trigger = dict(spec.trigger)
    # The flag the mover branches on. Named for what it ASSERTS — the job reached SUCCEEDED — rather
    # than "skip_submit", so a reader of the handler sees the precondition and not the shortcut.
    trigger["ray_job_done"] = True
    trigger["ray_submission_id"] = outcome.submission_id

    with DaprClient() as client:
        client.publish_event(
            pubsub_name=settings.pubsub,
            # The mover's OWN subscription topic: this wakes the same handler the original trigger
            # reached, so the measure/emit/cascade path is the one already under test — not a fork.
            topic_name=settings.sub_topic,
            data=json.dumps(trigger),
            data_content_type="application/json",
        )
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


WORKFLOWS = (stage_run,)

ACTIVITIES = (
    submit_stage,
    poll_stage,
    publish_stage_ready,
    report_stage_outcome,
)


def register(runtime: wf.WorkflowRuntime) -> None:
    """Register everything with the runtime — one place, so nothing is silently unregistered."""
    for w in WORKFLOWS:
        runtime.register_workflow(w)
    for a in ACTIVITIES:
        runtime.register_activity(a)
