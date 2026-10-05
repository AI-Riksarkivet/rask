"""A Ray stage job is PLANNED before it is submitted, and reaches exactly one terminal (CP-029 S1).

Pass 1 writes a plan (`service_kit.lakehouse.run_plans`) under the job's content-addressed key, publishes it on the
control lane, submits the order, and acks, so nothing waits on the job and no deadline turns a long run into a guessed
outcome. The job reports its own terminal state through this stage runner's outcome door
(`api/stage_outcomes.py`); the sweep resolves what no report arrived for. Both resolve through ONE function
(`run_outcomes.resolve`), with this module's :class:`StageOutcomeLane` deciding what each terminal does:

* succeeded: re-wake this stage runner's own pass 2 (the trigger, re-published to its own subscription with
  ``ray_job_done``), which measures the destination, emits the COMPLETE and promotes. A hand-off that cannot be
  published leaves the plan open for the next tick, so a job Ray says SUCCEEDED never produces a FAIL.
* failed: a FAIL through the lineage outbox, naming the version the run's commit marker records when it committed
  before it failed (the destination's Lance history, `commit_marker.marked_version`), bare otherwise.

THE SWEEP (D-6), one tick per cron firing, over this stage runner's open plans and through the executor port only:
a RUNNING or PENDING job is left alone at any age (a running job is not failed; `/cascade/stalled` and its alert
surface a hung hop); SUCCEEDED, FAILED and STOPPED resolve; a job the engine no longer knows (seen and then gone, or
never registered within :data:`MAX_UNSEEN_TICKS`) resolves succeeded when its marker is found; otherwise it is
resubmitted under the same key within :data:`MAX_RESUBMITS` and fails after that, unless the engine advertises
`Capability.DURABLE_RECORD`, whose lost record is not a lost run: that one fails at once (CP-044).

`bindings.cron` FIRES ON EVERY REPLICA, and this sweep needs no lease: every plan mutation is ETag CAS, the close is
first-wins, a hand-off is replay-safe (pass 2 dedupes its volume, `metrics.record_stage_completion`), and a resubmit
re-attaches under the same key, so two replicas sweeping one plan converge on one terminal.

THE IN-PROCESS LANE NEEDS NONE OF THIS. It learns its outcome synchronously inside `handle_stage`, so it writes no
plan and its commits carry no marker.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel, ConfigDict

from medallion.core.best_effort import best_effort
from medallion.core.config import MedallionSettings, project_namespace
from medallion.core.lineage_publish import emit_lineage
from medallion.core.metrics import record_outcome_conflict, record_stage_outcome
from medallion.schemas.events import build_run_event
from medallion.services.planned_runs import FAIL_MESSAGE_CAP, MAX_UNSEEN_TICKS, destination_version, publish_plan, ray_executor, run_handle
from medallion.services.stage_submit import build_stage_order, submit_stage_order
from service_kit import dapr_publish
from service_kit.lakehouse.commit_marker import marked_version
from service_kit.lakehouse.executor import Capability, Executor, RunState
from service_kit.lakehouse.naming import CATALOG_DELIMITER
from service_kit.lakehouse.run_outcomes import OutcomeNotRecordedError, OutcomeReport, SweepReport, SweepVisit, resolve, sweep_plans
from service_kit.lakehouse.run_plans import OutcomeConflictError, OutcomeStatus, PlanDocument, PlanKind, PlanStore, RunOutcome
from service_kit.lakehouse.task_registry import TaskRegistration


if TYPE_CHECKING:
    from lineage_kit.consume import LineageDoc
    from medallion.services.trigger_guards import StageTrigger


log = logging.getLogger(__name__)

#: How many times a stage whose job the engine lost may be submitted again under the same key. Safe because the key is
#: deterministic and the engine runs the STAGE contract (`on_terminal_failure="resubmit"`): the same id creates a fresh
#: job when the record is gone or re-attaches to one that turned out to be alive. TWO, because a head restart is one
#: event but a rolling restart can bounce; bounded so a head that keeps losing jobs reports instead of resubmitting.
MAX_RESUBMITS: Final = 2


class StageHandOffError(OutcomeNotRecordedError):
    """The succeeded run's pass-2 trigger could not be published; the plan stays open for the next attempt."""


class StageStopError(RuntimeError):
    """The compute engine could not stop a planned run's job; the plan stays open."""


def plan_store(settings: MedallionSettings) -> PlanStore:
    """This stage runner's plans: its control root, its own identity as the owner."""
    return PlanStore(settings.control_root, settings.storage_options(), owner=settings.fga_service_identity)


def _registration(plan: PlanDocument) -> TaskRegistration:
    """What the plan's first submission ran, so a resubmit runs the same program under the same key."""
    return TaskRegistration(task=plan.task, engine=plan.engine, command=plan.command, code_version=plan.code_version)


def _seconds(plan: PlanDocument, at: datetime) -> float:
    return max(0.0, (at - plan.submitted_at).total_seconds())


def _namespace_of(table_id: str, fallback: str) -> str:
    namespace, sep, _ = table_id.partition(CATALOG_DELIMITER)
    return namespace if sep and namespace else fallback


def stage_fail_event(settings: MedallionSettings, plan: PlanDocument, outcome: RunOutcome) -> dict[str, Any]:
    """The FAIL RunEvent for a planned stage that did not complete.

    Named exactly as the run's START and COMPLETE name it: the catalog ids the stage runner resolved at dispatch
    (project-qualified, or the declared ones), so delivery's visibility check reads the same `table:` objects the
    grants name. Keyed on the trigger's token, so it MERGEs onto the run the START opened. A marker found above the
    plan's base version puts the committed version on the output (the WROTE edge with its version, clause e); without
    one the output stays bare.
    """
    trigger = plan.trigger
    project = str(trigger.get("project") or "")
    from_id = plan.from_id or project_namespace(project, settings.from_dataset)
    to_id = plan.to_id or project_namespace(project, settings.to_dataset)
    return build_run_event(
        operation=settings.operation,
        author=settings.author,
        author_subject=settings.fga_service_identity,
        job_namespace=settings.job_namespace,
        inputs=[(_namespace_of(from_id, project_namespace(project, settings.from_namespace)), from_id)],
        output_namespace=_namespace_of(to_id, project_namespace(project, settings.to_namespace)),
        output_name=to_id,
        token=trigger.get("token") or None,
        cascade_id=trigger.get("cascade_id") or None,
        project=project or None,
        # The trigger is the carrier: the request that started the cascade is long gone, and the head was the last place
        # the verified subject existed.
        originator=trigger.get("originator") or None,
        event_type="FAIL",
        error_message=outcome.error or f"the stage job {plan.action_id} failed",
        committed_version=outcome.committed_version,
    )


class StageOutcomeLane:
    """What a planned stage's terminal does: re-wake pass 2 on success, a FAIL through the outbox on failure."""

    def __init__(self, settings: MedallionSettings, dapr: object) -> None:
        self._settings = settings
        self._dapr = dapr

    async def committed_version(self, plan: PlanDocument) -> int | None:
        """The destination version the run's marker names above the plan's base version (a run leaves 2-3 commits)."""
        return await asyncio.to_thread(marked_version, plan.to_uri, self._settings.storage_options(), action_id=plan.action_id, above=plan.base_version)

    async def hand_off(self, plan: PlanDocument, outcome: RunOutcome) -> None:
        """Re-publish the run's trigger to this stage runner's own subscription, as the job-landed pass 2 parses it.

        Not a privileged back channel: pass 2 re-parses it through the same `parse_stage_trigger` guard as any bus
        arrival. The measured span rides it so exactly one place (pass 2) records the stage's duration.

        Raises:
            StageHandOffError: the publish did not land; the plan stays open.
        """
        trigger = {
            **plan.trigger,
            "ray_job_done": True,
            "ray_submission_id": plan.action_id,
            "ray_duration_seconds": _seconds(plan, outcome.recorded_at),
        }
        if outcome.committed_version is not None:
            trigger["ray_committed_version"] = outcome.committed_version
        landed = await dapr_publish.publish_json(
            self._dapr,
            pubsub_name=self._settings.pubsub,
            topic_name=self._settings.sub_topic,
            payload=trigger,
            timeout_seconds=self._settings.publish_timeout_seconds,
            failure_event="medallion_stage_handoff_failed",
            context={"action_id": plan.action_id, "token": trigger.get("token")},
        )
        if not landed:
            raise StageHandOffError(f"the pass-2 trigger for {plan.action_id} did not reach {self._settings.sub_topic}")
        record_stage_outcome("succeeded", duration_seconds=_seconds(plan, outcome.recorded_at))
        log.info("medallion_stage_handed_off", extra={"action_id": plan.action_id, "source": outcome.source, "committed_version": outcome.committed_version})

    async def record_failure(self, plan: PlanDocument, outcome: RunOutcome) -> None:
        """Count the failure, mark the span, and emit the FAIL best-effort: the plan closes whether or not it lands."""
        record_stage_outcome("failed", duration_seconds=_seconds(plan, outcome.recorded_at))
        # The verdict and the run ride the live span, never a metric label: the action id is per-run and unbounded.
        span = trace.get_current_span()
        span.set_attribute("lance.medallion.verdict", outcome.status)
        span.set_attribute("lance.medallion.submission_id", plan.action_id)
        span.set_status(Status(StatusCode.ERROR, f"stage failed: {plan.action_id}"))
        with best_effort("stage_fail_event", token=plan.trigger.get("token"), action_id=plan.action_id):
            await emit_lineage(self._dapr, self._settings, stage_fail_event(self._settings, plan, outcome))
        log.error(
            "medallion_stage_job_failed",
            extra={"action_id": plan.action_id, "source": outcome.source, "committed_version": outcome.committed_version, "to_uri": plan.to_uri},
        )

    def record_conflict(self, plan: PlanDocument, refused: OutcomeStatus) -> None:
        """Count and log an outcome the closed plan refused."""
        record_outcome_conflict(PlanKind.STAGE.value, refused)
        recorded = plan.outcome.status if plan.outcome is not None else None
        log.warning("medallion_stage_outcome_conflict", extra={"action_id": plan.action_id, "recorded": recorded, "refused": refused})


async def dispatch(
    settings: MedallionSettings,
    dapr: object,
    *,
    trigger: StageTrigger,
    from_uri: str,
    to_uri: str,
    from_id: str,
    to_id: str,
    lineage_doc: LineageDoc,
    event_time: str,
    pre_row_count: int | None,
    project: str,
) -> str:
    """Plan this run, announce it, submit it, and answer its action id (D-7). Pass 1 acks on return.

    The plan is written first and idempotently: a redelivery finds it. A key naming a run that SUCCEEDED submits
    nothing new; one naming a run that FAILED reopens it for another attempt, which is what makes a re-run with a
    supplied token (`api/rerun.py`) re-drive the same run. A submit failure is logged and acked: the plan is open, so
    the sweep owns the retry budget.

    ``event_time`` and ``pre_row_count`` ride the stored trigger because pass 2 cannot observe them: the instant is
    pass 1's (R26), and the predecessor's row count is gone once the job writes.

    Raises:
        UndeclaredTransformError: the stage runner names a lane the catalog has no declaration for.
        OSError: the plan cannot be written to the control root (the handler answers RETRY).
    """
    order, registration = await build_stage_order(
        settings,
        from_uri=from_uri,
        to_uri=to_uri,
        stage=settings.to_namespace,
        token=trigger.token,
        lineage_json=lineage_doc.to_json(),
        originator=trigger.originator or "",
        project=project,
        from_version=trigger.from_version,
        from_id=from_id,
        to_id=to_id,
        run_id=lineage_doc.run_id,
    )
    carried = trigger.model_dump()
    carried["event_time"] = event_time
    if pre_row_count is not None:
        carried["pre_row_count"] = pre_row_count
    store = plan_store(settings)
    base_version = await asyncio.to_thread(destination_version, to_uri, settings.storage_options())
    now = datetime.now(UTC)
    planned = PlanDocument.for_order(
        order, kind=PlanKind.STAGE, engine=registration.engine, command=registration.command, base_version=base_version, trigger=carried, submitted_at=now
    )
    plan, created = await asyncio.to_thread(store.create, planned)
    if plan.outcome is not None:
        if plan.outcome.status == "succeeded":
            log.info("medallion_stage_already_landed", extra={"action_id": plan.action_id})
            return plan.action_id
        plan = await asyncio.to_thread(store.reopen, plan.action_id, base_version=base_version, submitted_at=now)
    plan = await publish_plan(settings, dapr, store, plan)
    stored = plan.order if plan.order is not None else order
    try:
        await submit_stage_order(stored, _registration(plan))
    except Exception as exc:  # noqa: BLE001 — the plan is open, and the sweep's resubmit budget owns a lost submit
        log.warning("medallion_stage_submit_deferred", extra={"action_id": plan.action_id, "plan_created": created, "error": str(exc)[:300]})
    return plan.action_id


async def _failure_text(executor: Executor, plan: PlanDocument, state: RunState) -> str:
    """Why the engine says the job ended, bounded: Ray's own classification and message when it can be read."""
    wire = "STOPPED" if state is RunState.CANCELLED else "FAILED"
    reason = f"the Ray stage job {plan.action_id} ended {wire}"
    cause = None
    with best_effort("read_stage_failure", action_id=plan.action_id):
        cause = await executor.failure(run_handle(plan))
    summary = cause.summary(FAIL_MESSAGE_CAP) if cause is not None else ""
    return f"{reason} — {summary}" if summary else reason


async def _tick(store: PlanStore, plan: PlanDocument, executor: Executor, lane: StageOutcomeLane) -> SweepVisit:
    """One plan, one engine read: what the sweep did with it."""
    state = await executor.status(run_handle(plan))
    if state in (RunState.RUNNING, RunState.PENDING):
        if not plan.seen or plan.unseen_ticks:
            await asyncio.to_thread(store.update, plan.action_id, lambda p: p.model_copy(update={"seen": True, "unseen_ticks": 0}))
        return "watching"
    if state is RunState.SUCCEEDED:
        await resolve(store, plan, OutcomeReport(status="succeeded"), lane, source="sweep")
        return "resolved"
    if state in (RunState.FAILED, RunState.CANCELLED):
        await resolve(store, plan, OutcomeReport(status="failed", error=await _failure_text(executor, plan, state)), lane, source="sweep")
        return "resolved"
    vanished = plan.seen
    never_registered = not plan.seen and plan.unseen_ticks + 1 >= MAX_UNSEEN_TICKS
    if not (vanished or never_registered):
        await asyncio.to_thread(store.update, plan.action_id, lambda p: p.model_copy(update={"unseen_ticks": p.unseen_ticks + 1}))
        return "watching"
    # THE ENGINE LOST THE RECORD. Its Lance history is what remains: a marker above the plan's base version means the
    # job's final commit landed, and the run succeeded whatever became of the job.
    if await lane.committed_version(plan) is not None:
        await resolve(store, plan, OutcomeReport(status="succeeded"), lane, source="sweep")
        return "resolved"
    # ONLY BEHIND AN ENGINE THAT CAN LOSE A RECORD. A resubmit answers a lost record; an engine that keeps a durable
    # one did not lose the run, so a second submission under its key would be a second copy of work nothing lost.
    durable = Capability.DURABLE_RECORD in executor.capabilities
    if not durable and plan.resubmits < MAX_RESUBMITS:
        resubmitted = await asyncio.to_thread(
            store.update, plan.action_id, lambda p: p.model_copy(update={"resubmits": p.resubmits + 1, "seen": False, "unseen_ticks": 0})
        )
        log.warning("medallion_stage_resubmitting", extra={"action_id": plan.action_id, "attempt": resubmitted.resubmits, "vanished": vanished})
        if resubmitted.order is not None:
            await submit_stage_order(resubmitted.order, _registration(resubmitted), executor=executor)
        return "resubmitted"
    lost = "vanished" if vanished else "never registered"
    if durable:
        error = f"the stage job {plan.action_id} {lost} on an engine that keeps a durable record, and its destination carries no commit marker"
    else:
        error = f"the Ray stage job {plan.action_id} {lost} after {plan.resubmits} resubmit(s), and its destination carries no commit marker"
    await resolve(store, plan, OutcomeReport(status="failed", error=error), lane, source="sweep")
    return "resolved"


async def sweep(settings: MedallionSettings, dapr: object, *, executor: Executor | None = None) -> SweepReport:
    """One tick over this stage runner's open plans, then the retention prune. One plan's failure never stops the rest."""
    store = plan_store(settings)
    engine = executor or ray_executor()
    lane = StageOutcomeLane(settings, dapr)

    async def visit(plan: PlanDocument) -> SweepVisit:
        return await _tick(store, await publish_plan(settings, dapr, store, plan), engine, lane)

    return await sweep_plans(store, kind=PlanKind.STAGE, visit=visit)


class StageRunView(BaseModel):
    """A DECLARED field list for an operator's status question: the plan's identity and state, never its order."""

    model_config = ConfigDict(frozen=True)

    instance_id: str
    #: The engine's state while the run is open, or the plan's outcome once it closed.
    status: str
    #: Echoed so an operator can cross-check the engine's own dashboard.
    submission_id: str
    attempt: int
    #: The tenant the run is for, read off its plan: ``""`` for a single-tenant run, ``None`` when the plan document
    #: does not parse, which the producer refuses rather than read as single-tenant.
    project: str | None
    outcome: RunOutcome | None = None


async def show(settings: MedallionSettings, action_id: str, *, executor: Executor | None = None) -> StageRunView | None:
    """The plan under ``action_id`` with ONE engine read while it is open; ``None`` when no stage run is planned under it."""
    try:
        plan = await asyncio.to_thread(plan_store(settings).read, action_id)
    except ValueError:  # a document that does not parse as JSON, or as a plan (pydantic's ValidationError is a ValueError)
        log.warning("medallion_stage_plan_unreadable", extra={"action_id": action_id})
        return StageRunView(instance_id=action_id, status="UNREADABLE", submission_id=action_id, attempt=0, project=None)
    if plan is None or plan.kind is not PlanKind.STAGE:
        return None
    if plan.outcome is not None:
        status = plan.outcome.status.upper()
    else:
        # The engine read is a courtesy to the operator, so an unreachable engine answers UNREADABLE rather than
        # failing the status question the plan itself can answer.
        status = "UNREADABLE"
        with best_effort("read_stage_status", action_id=plan.action_id):
            status = (await (executor or ray_executor()).status(run_handle(plan))).value.upper()
    return StageRunView(instance_id=action_id, status=status, submission_id=plan.action_id, attempt=plan.attempt, project=plan.project, outcome=plan.outcome)


async def terminate(settings: MedallionSettings, dapr: object, action_id: str, *, executor: Executor | None = None) -> PlanDocument | None:
    """Stop the run's job through the executor port, then resolve the plan on the state the engine reports after the stop.

    THE STOP ASKS, THE ENGINE'S STATE DECIDES, because no path may emit a FAIL for a job Ray says SUCCEEDED (D-4). A
    job can finish between its last report and an operator's stop (its report lost, the sweep's tick not yet come),
    and Ray answers the stop of a job that already ended with ``stopped: false``. So the state is read again after the
    stop and resolved as the sweep resolves it:

    * SUCCEEDED hands the run off and the next tier wakes; so does a job the engine no longer knows whose destination
      carries the run's commit marker.
    * FAILED closes failed with the engine's cause. STOPPED, or a lost job with no marker, closes failed as stopped by
      an operator.
    * RUNNING or PENDING, a stop the engine has not finished, leaves the plan open: the sweep closes it on the state
      the job ends in.

    ``None`` when no stage run is planned under ``action_id``; a run that already closed is answered as it stands.

    Raises:
        StageStopError: the engine could not stop the job, or could not say what state the stop left it in; the plan
            stays open and the sweep resolves it.
        StageHandOffError: the job had succeeded and its pass-2 hand-off did not land; the plan stays open.
    """
    store = plan_store(settings)
    plan = await asyncio.to_thread(store.read, action_id)
    if plan is None or plan.kind is not PlanKind.STAGE:
        return None
    if plan.outcome is not None:
        return plan
    engine = executor or ray_executor()
    try:
        await engine.cancel(run_handle(plan))
    except Exception as exc:
        raise StageStopError(f"the compute engine could not stop the job of {plan.action_id}: {exc}") from exc
    try:
        state = await engine.status(run_handle(plan))
    except Exception as exc:
        raise StageStopError(f"the compute engine was asked to stop the job of {plan.action_id} and could not say how it ended: {exc}") from exc
    if state in (RunState.RUNNING, RunState.PENDING):
        return plan
    lane = StageOutcomeLane(settings, dapr)
    if state is RunState.SUCCEEDED or (state is RunState.UNKNOWN and await lane.committed_version(plan) is not None):
        report = OutcomeReport(status="succeeded")
    elif state is RunState.FAILED:
        report = OutcomeReport(status="failed", error=await _failure_text(engine, plan, state))
    else:
        report = OutcomeReport(status="failed", error=f"the Ray stage job {plan.action_id} ended STOPPED by an operator")
    try:
        return await resolve(store, plan, report, lane, source="operator")
    except OutcomeConflictError as exc:  # the job's own report closed the run first; its terminal stands
        return exc.plan
