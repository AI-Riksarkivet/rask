"""A Ray training job is PLANNED before it is submitted, and reaches exactly one terminal (CP-029).

A durable record, not a watcher, carries the run, so a job the head loses still reaches a terminal and no deadline
guesses an outcome. The training consumer writes a plan (`service_kit.lakehouse.run_plans`) under the job's `ray-train-<token>` id, announces it on the control lane,
submits, and acks. The job keeps its own START / RUNNING / COMPLETE / FAIL lineage and, AFTER its terminal emit
landed, reports through the producer's outcome door (`api/train_outcomes.py`). A run whose report never arrives is
the sweep's. Both resolve through ONE function (`run_outcomes.resolve`); :class:`TrainOutcomeLane` decides what each
terminal does:

* a terminal the JOB reported emits nothing more: the job already recorded it;
* a terminal the SWEEP or an OPERATOR decided is recorded here, a COMPLETE naming the registry version the run's
  commit marker found or a FAIL, with the job's own run id and the medallion's identity
  (`schemas.events.build_train_outcome_event`). A repeat of a terminal the job did emit is absorbed downstream.

THE SWEEP, one tick per cron firing over the producer's open training plans, through the executor port only. A RUNNING
or PENDING job is left alone at any age: a running job is not failed, however long it trains. FAILED or STOPPED is one
FAIL with Ray's own cause. SUCCEEDED without a report, or a job the engine no longer knows (seen and then gone, or
never registered within `planned_runs.MAX_UNSEEN_TICKS`), is decided by the registry's Lance history: a commit marker
above the plan's base version is a COMPLETE naming that version, its absence one FAIL. NOTHING IS RESUBMITTED (D2:
training compute is expensive, so a run that did not land stays terminal until a person asks again with a new token).

`bindings.cron` FIRES ON EVERY REPLICA, and the sweep needs no lease: every plan write is ETag CAS and the close is
first-wins, so two replicas resolving one plan leave one outcome, and a terminal both of them emitted is one run
downstream.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any, Final, Literal

from lance_namespace import ServiceUnavailableError
from pydantic import BaseModel, ConfigDict

from medallion.core.best_effort import best_effort
from medallion.core.config import MedallionSettings
from medallion.core.lineage_publish import emit_lineage
from medallion.core.metrics import record_outcome_conflict, record_train_outcome
from medallion.schemas.events import build_train_outcome_event
from medallion.services import ray_submit
from medallion.services.engine_names import RAY_ENGINE
from medallion.services.planned_runs import FAIL_MESSAGE_CAP, MAX_UNSEEN_TICKS, destination_version, outcome_url, publish_plan, ray_executor, run_handle
from service_kit.lakehouse.commit_marker import marked_version
from service_kit.lakehouse.executor import Executor, RunState
from service_kit.lakehouse.naming import CATALOG_DELIMITER
from service_kit.lakehouse.run_outcomes import OutcomeNotRecordedError, OutcomeReport, SweepReport, SweepVisit, resolve, sweep_plans
from service_kit.lakehouse.run_plans import OutcomeConflictError, OutcomeStatus, PlanDocument, PlanKind, PlanStore, RunOutcome
from service_kit.openlineage import run_id_for


log = logging.getLogger(__name__)

#: The task a training plan names, the registry key `MEDALLION_RAY_TASKS` registers the train entrypoint under.
TRAIN_TASK: Final = "train"

#: What `dispatch` did: the engine's answer to the submit, or the plan's own when the run had already ended.
type TrainDispatch = Literal["submitted", "attached", "already_failed", "already_landed"]


class TrainOutcomeNotRecordedError(OutcomeNotRecordedError):
    """A COMPLETE the sweep decided could not be emitted; the plan stays open and the next tick emits it."""


class TrainStopError(RuntimeError):
    """The compute engine could not stop a planned training job; the plan stays open."""


def plan_store(settings: MedallionSettings) -> PlanStore:
    """The producer's training plans: its control root, its own identity as the owner.

    Raises:
        ServiceUnavailableError: no control root is configured, so this producer plans no training run.
    """
    if not settings.control_root:
        raise ServiceUnavailableError("training runs are not planned here: no control root is configured")
    return PlanStore(settings.control_root, settings.storage_options(), owner=settings.fga_service_identity)


def train_run_id(token: str) -> str:
    """The training run's lineage id, byte-identical to the one `scripts/ray_train_job.py` stamps on its events."""
    return run_id_for(f"train-{token}")


def train_outcome_event(settings: MedallionSettings, plan: PlanDocument, outcome: RunOutcome) -> dict[str, Any]:
    """The terminal the medallion records for ``plan``, named for the person and the tenant the run is for."""
    return build_train_outcome_event(
        succeeded=outcome.status == "succeeded",
        run_id=plan.run_id,
        token=str(plan.trigger.get("token") or ""),
        model_table=plan.to_id,
        models_namespace=plan.stage,
        job_namespace=settings.job_namespace,
        author=settings.author,
        author_subject=settings.fga_service_identity,
        registry_uri=plan.to_uri,
        committed_version=outcome.committed_version,
        error_message=None if outcome.status == "succeeded" else outcome.error or f"the Ray training job {plan.action_id} failed",
        originator=plan.originator or None,
        project=plan.project or None,
    )


class TrainOutcomeLane:
    """What a planned training run's terminal does: nothing more when the job reported it, a recorded terminal otherwise."""

    def __init__(self, settings: MedallionSettings, dapr: object) -> None:
        self._settings = settings
        self._dapr = dapr

    async def committed_version(self, plan: PlanDocument) -> int | None:
        """The registry version the run's marker names above the plan's base version: the model version it published."""
        return await asyncio.to_thread(marked_version, plan.to_uri, self._settings.storage_options(), action_id=plan.action_id, above=plan.base_version)

    async def hand_off(self, plan: PlanDocument, outcome: RunOutcome) -> None:
        """Record a run that succeeded: the job's own COMPLETE when it reported, this COMPLETE when the sweep decided.

        Raises:
            TrainOutcomeNotRecordedError: the COMPLETE could not be emitted; the plan stays open for the next tick.
        """
        if outcome.source != "job":
            try:
                await emit_lineage(self._dapr, self._settings, train_outcome_event(self._settings, plan, outcome))
            except Exception as exc:
                raise TrainOutcomeNotRecordedError(f"the COMPLETE of training run {plan.action_id} was not emitted: {exc}") from exc
        record_train_outcome("succeeded")
        log.info("medallion_train_run_succeeded", extra={"action_id": plan.action_id, "source": outcome.source, "committed_version": outcome.committed_version})

    async def record_failure(self, plan: PlanDocument, outcome: RunOutcome) -> None:
        """Count the failure and, unless the job reported it, emit the FAIL best-effort: the plan closes either way."""
        record_train_outcome("failed")
        if outcome.source != "job":
            with best_effort("train_fail_event", token=plan.trigger.get("token"), action_id=plan.action_id):
                await emit_lineage(self._dapr, self._settings, train_outcome_event(self._settings, plan, outcome))
        log.error(
            "medallion_train_run_failed",
            extra={"action_id": plan.action_id, "source": outcome.source, "committed_version": outcome.committed_version, "error": (outcome.error or "")[:300]},
        )

    def record_conflict(self, plan: PlanDocument, refused: OutcomeStatus) -> None:
        """Count and log an outcome the closed plan refused."""
        record_outcome_conflict(PlanKind.TRAIN.value, refused)
        recorded = plan.outcome.status if plan.outcome is not None else None
        log.warning("medallion_train_outcome_conflict", extra={"action_id": plan.action_id, "recorded": recorded, "refused": refused})


async def dispatch(
    settings: MedallionSettings,
    dapr: object,
    *,
    token: str,
    model: str,
    features: list[dict[str, Any]],
    config: dict[str, Any],
    registry_uri: str,
    artifact_base: str,
    originator: str,
    project: str,
) -> TrainDispatch:
    """Plan this training run, announce it, submit it, and answer what happened. The consumer acks on return.

    The plan is written first and idempotently, so a redelivered trigger finds it and re-attaches. A plan that has
    already ENDED submits nothing (D2): one that succeeded answers ``already_landed``, one that failed
    ``already_failed``, which the consumer drops.

    Raises:
        ray_submit.RayJobError: the engine could not be reached or refused the submission; the plan stays open, so a
            redelivery re-attaches and the sweep fails a submission that never registers.
        ServiceUnavailableError: no control root is configured.
    """
    action_id = ray_submit.train_submission_id(token)
    store = plan_store(settings)
    base_version = await asyncio.to_thread(destination_version, registry_uri, settings.storage_options())
    planned = PlanDocument(
        action_id=action_id,
        run_id=train_run_id(token),
        kind=PlanKind.TRAIN,
        engine=RAY_ENGINE,
        task=TRAIN_TASK,
        stage=settings.models_namespace,
        from_uri="",
        to_uri=registry_uri,
        to_id=f"{settings.models_namespace}{CATALOG_DELIMITER}{model}",
        base_version=base_version,
        originator=originator,
        project=project,
        trigger={"token": token, "model": model, "features": features},
        submitted_at=datetime.now(UTC),
        report_url=outcome_url(settings, action_id),
        command=settings.train_entrypoint,
    )
    plan, created = await asyncio.to_thread(store.create, planned)
    if plan.outcome is not None:
        log.info("medallion_train_run_already_ended", extra={"action_id": action_id, "outcome": plan.outcome.status})
        return "already_landed" if plan.outcome.status == "succeeded" else "already_failed"
    plan = await publish_plan(settings, dapr, store, plan)
    submitted = await ray_submit.submit_train_job(
        settings,
        model=model,
        features_json=json.dumps(features),
        config_json=json.dumps(config),
        token=token,
        registry_uri=registry_uri,
        artifact_base=artifact_base,
        originator=originator,
        project=project,
        outcome_url=plan.report_url,
    )
    log.info("medallion_train_run_planned", extra={"action_id": action_id, "plan_created": created, "submitted": submitted})
    if submitted == "already_failed":
        return "already_failed"
    return "attached" if submitted == "attached" else "submitted"


async def _failure_text(executor: Executor, plan: PlanDocument, state: RunState) -> str:
    """Why the engine says the job ended, bounded: Ray's own classification and message when it can be read."""
    wire = "STOPPED" if state is RunState.CANCELLED else "FAILED"
    reason = f"the Ray training job {plan.action_id} ended {wire}"
    cause = None
    with best_effort("read_train_failure", action_id=plan.action_id):
        cause = await executor.failure(run_handle(plan))
    summary = cause.summary(FAIL_MESSAGE_CAP) if cause is not None else ""
    return f"{reason} — {summary}" if summary else reason


async def _tick(store: PlanStore, plan: PlanDocument, executor: Executor, lane: TrainOutcomeLane) -> SweepVisit:
    """One plan, one engine read: what the sweep did with it."""
    state = await executor.status(run_handle(plan))
    if state in (RunState.RUNNING, RunState.PENDING):
        if not plan.seen or plan.unseen_ticks:
            await asyncio.to_thread(store.update, plan.action_id, lambda p: p.model_copy(update={"seen": True, "unseen_ticks": 0}))
        return "watching"
    # The job reports BEFORE it exits, so a terminal state read after this plan was listed can follow the report that
    # closed it. Re-read, and leave a closed run to the terminal its job already recorded.
    current = await asyncio.to_thread(store.read, plan.action_id)
    if current is None or not current.is_open:
        return "watching"
    plan = current
    if state in (RunState.FAILED, RunState.CANCELLED):
        await resolve(store, plan, OutcomeReport(status="failed", error=await _failure_text(executor, plan, state)), lane, source="sweep")
        return "resolved"
    if state is RunState.SUCCEEDED:
        how = "ended SUCCEEDED"
    elif plan.seen:
        how = "vanished"
    elif plan.unseen_ticks + 1 >= MAX_UNSEEN_TICKS:
        how = "never registered"
    else:
        await asyncio.to_thread(store.update, plan.action_id, lambda p: p.model_copy(update={"unseen_ticks": p.unseen_ticks + 1}))
        return "watching"
    # NO REPORT ARRIVED, AND THE JOB WILL NOT RUN AGAIN. The registry's Lance history is what remains: the job stamps its
    # one registry commit with the run's marker, so a marker above the plan's base version means the model landed.
    if await lane.committed_version(plan) is not None:
        await resolve(store, plan, OutcomeReport(status="succeeded"), lane, source="sweep")
        return "resolved"
    error = f"the Ray training job {plan.action_id} {how}, and its registry carries no commit marker"
    await resolve(store, plan, OutcomeReport(status="failed", error=error), lane, source="sweep")
    return "resolved"


async def sweep(settings: MedallionSettings, dapr: object, *, executor: Executor | None = None) -> SweepReport:
    """One tick over the producer's open training plans, then the retention prune. One plan's failure never stops the rest."""
    store = plan_store(settings)
    engine = executor or ray_executor()
    lane = TrainOutcomeLane(settings, dapr)

    async def visit(plan: PlanDocument) -> SweepVisit:
        return await _tick(store, await publish_plan(settings, dapr, store, plan), engine, lane)

    return await sweep_plans(store, kind=PlanKind.TRAIN, visit=visit)


async def read(settings: MedallionSettings, action_id: str) -> PlanDocument | None:
    """The training plan under ``action_id``, ``None`` when none is planned under it.

    Raises:
        ValueError: the document does not parse as a plan.
        ServiceUnavailableError: no control root is configured.
    """
    plan = await asyncio.to_thread(plan_store(settings).read, action_id)
    return plan if plan is not None and plan.kind is PlanKind.TRAIN else None


class TrainRunState(BaseModel):
    """A DECLARED field list for an operator's status question: the run's state, never its trigger."""

    model_config = ConfigDict(frozen=True)

    instance_id: str
    #: The engine's state while the run is open, or the plan's outcome once it closed.
    status: str
    #: Echoed so an operator can cross-check the engine's own dashboard.
    submission_id: str
    outcome: RunOutcome | None = None


async def state(plan: PlanDocument, *, executor: Executor | None = None) -> TrainRunState:
    """The run's state: its outcome once closed, ONE engine read while it is open."""
    if plan.outcome is not None:
        status = plan.outcome.status.upper()
    else:
        # The engine read is a courtesy to the operator, so an unreachable engine answers UNREADABLE rather than
        # failing the status question the plan itself can answer.
        status = "UNREADABLE"
        with best_effort("read_train_status", action_id=plan.action_id):
            status = (await (executor or ray_executor()).status(run_handle(plan))).value.upper()
    return TrainRunState(instance_id=plan.action_id, status=status, submission_id=plan.action_id, outcome=plan.outcome)


async def terminate(settings: MedallionSettings, dapr: object, plan: PlanDocument, *, executor: Executor | None = None) -> PlanDocument:
    """Stop the run's job through the executor port, then resolve the plan on the state the engine reports after the stop.

    THE STOP ASKS, THE ENGINE'S STATE DECIDES, as on the stage lane (D-4: no path emits a FAIL for a job Ray says
    SUCCEEDED). A job can commit its model and emit its COMPLETE between its last report and an operator's stop, and Ray
    answers the stop of a job that already ended with ``stopped: false``. So the state is read again after the stop:

    * SUCCEEDED, or a job the engine no longer knows, is decided by the registry's commit marker: found is a COMPLETE
      naming that version, absent a FAIL stopped by an operator.
    * FAILED closes failed with the engine's cause; STOPPED closes failed as stopped by an operator.
    * RUNNING or PENDING, a stop the engine has not finished, leaves the plan open for the sweep.

    A run that already closed, including one its job's report closed during the stop, is answered as it stands.

    Raises:
        TrainStopError: the engine could not stop the job, or could not say what state the stop left it in; the plan
            stays open and the sweep resolves it.
    """
    if plan.outcome is not None:
        return plan
    engine = executor or ray_executor()
    try:
        await engine.cancel(run_handle(plan))
    except Exception as exc:
        raise TrainStopError(f"the compute engine could not stop the training job {plan.action_id}: {exc}") from exc
    try:
        state = await engine.status(run_handle(plan))
    except Exception as exc:
        raise TrainStopError(f"the compute engine was asked to stop the training job {plan.action_id} and could not say how it ended: {exc}") from exc
    if state in (RunState.RUNNING, RunState.PENDING):
        return plan
    lane = TrainOutcomeLane(settings, dapr)
    stopped = f"the Ray training job {plan.action_id} ended STOPPED by an operator"
    if state in (RunState.SUCCEEDED, RunState.UNKNOWN):
        report = OutcomeReport(status="succeeded") if await lane.committed_version(plan) is not None else OutcomeReport(status="failed", error=stopped)
    elif state is RunState.FAILED:
        report = OutcomeReport(status="failed", error=await _failure_text(engine, plan, state))
    else:
        report = OutcomeReport(status="failed", error=stopped)
    try:
        return await resolve(plan_store(settings), plan, report, lane, source="operator")
    except OutcomeConflictError as exc:  # the job's own report closed the run first; its terminal stands
        return exc.plan
