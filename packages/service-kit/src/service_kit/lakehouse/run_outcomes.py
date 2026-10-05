"""How a planned run reaches exactly one terminal (CP-029 D-3, D-4): the outcome door, the one resolution function, and the job's client.

ONE RESOLUTION FUNCTION, called by the door a job reports through and by the sweep that resolves what no report
arrived for, so a run's terminal is decided in one place whichever of them gets there first:

* ``succeeded`` hands the run to its lane (the stage lane re-wakes the planner's second pass), THEN closes the plan.
  A failed hand-off raises and leaves the plan open for the next report or sweep tick, so nothing ever records a
  failure for a job that succeeded; a crash between the hand-off and the close only repeats a replay-safe hand-off.
* ``failed`` reads the destination's history for the run's commit marker and has the lane record the failure,
  naming the version the marker found (a job can commit and then fail), then closes the plan.

The plan's close is CAS and the first terminal wins (`run_plans.PlanStore.close`), so two callers racing one run
leave one outcome; a different outcome after it is refused and counted by the lane.

THE DOOR'S TRUST MODEL, named as the residual it is. A job authenticates with its pod's projected token, and on a
shared compute head every job is that pod's one account (LH-220 D1), so any job on the head can report any OPEN run
of the door's kind. That is no wider than the head-wide object-store key every job already holds; the door narrows it
by admitting only a key that names an open plan of its kind, and by taking the committed version from the
destination's own Lance history rather than from the report.
"""

# No `from __future__ import annotations`: the door's route annotations name the factory's own arguments (closure
# variables), and FastAPI resolves a postponed annotation against the module's globals, where those do not exist.
import asyncio
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from service_kit.lakehouse.run_plans import (
    ERROR_MAX_CHARS,
    OutcomeConflictError,
    OutcomeSource,
    OutcomeStatus,
    PlanDocument,
    PlanKind,
    PlanStore,
    RunOutcome,
)


log = logging.getLogger(__name__)

#: Where the chart projects a compute pod's `rask-medallion` ServiceAccount token, the job's credential at a door.
DEFAULT_OUTCOME_TOKEN_FILE: Final = Path("/var/run/secrets/rask/identity/rask-medallion/token")


class OutcomeReport(BaseModel):
    """What a job (or the sweep, or an operator) says about how a run ended."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: OutcomeStatus
    error: str | None = Field(default=None, max_length=ERROR_MAX_CHARS)
    #: The version the job believes it committed. Advisory: the resolver reads the destination's own history.
    committed_version: int | None = Field(default=None, ge=0)


class OutcomeNotRecordedError(RuntimeError):
    """A lane could not apply a terminal's effect (a succeeded run's hand-off); the plan stays open and a later report or
    sweep tick repeats it. The door answers 503, so a job knows its report did not land."""


class OutcomeLane(Protocol):
    """What one lane does when its run ends; the resolver decides WHEN, the lane decides WHAT."""

    async def committed_version(self, plan: PlanDocument) -> int | None:
        """The destination version the run's commit marker names, above the plan's base version, or ``None``."""
        ...

    async def hand_off(self, plan: PlanDocument, outcome: RunOutcome) -> None:
        """Continue a run that succeeded. Raising (`OutcomeNotRecordedError`) leaves the plan open."""
        ...

    async def record_failure(self, plan: PlanDocument, outcome: RunOutcome) -> None:
        """Record a run that failed. Best-effort: the plan closes whether or not the record lands."""
        ...

    def record_conflict(self, plan: PlanDocument, refused: OutcomeStatus) -> None:
        """Count an outcome refused because the plan already closed with the other one."""
        ...


async def resolve(store: PlanStore, plan: PlanDocument, report: OutcomeReport, lane: OutcomeLane, *, source: OutcomeSource) -> PlanDocument:
    """Bring ``plan`` to the terminal ``report`` names, exactly once; answer the closed plan.

    Raises:
        OutcomeConflictError: the plan already closed with the other outcome (counted through the lane).
    """
    if plan.outcome is not None:
        if plan.outcome.status == report.status:
            return plan
        lane.record_conflict(plan, report.status)
        raise OutcomeConflictError(plan, report.status)
    committed = await lane.committed_version(plan)
    outcome = RunOutcome(status=report.status, error=report.error, committed_version=committed, source=source, recorded_at=datetime.now(UTC))
    if outcome.status == "succeeded":
        await lane.hand_off(plan, outcome)
    else:
        await lane.record_failure(plan, outcome)
    try:
        return await asyncio.to_thread(store.close, plan.action_id, outcome)
    except OutcomeConflictError as exc:
        lane.record_conflict(exc.plan, outcome.status)
        raise


#: What one sweep visit did with an open plan; the sweep counts each by name.
type SweepVisit = Literal["watching", "resolved", "resubmitted"]


class SweepReport(BaseModel):
    """What one sweep tick did, returned to the cron binding that drove it."""

    model_config = ConfigDict(frozen=True)

    open: int = 0
    resolved: int = 0
    resubmitted: int = 0
    pruned: int = 0
    errors: int = 0


async def sweep_plans(store: PlanStore, *, kind: PlanKind, visit: Callable[[PlanDocument], Awaitable[SweepVisit]]) -> SweepReport:
    """One tick over ``store``'s open plans of ``kind``, then the retention prune; ``visit`` decides what each open plan needs.

    The index entries are read first: one whose plan closed, or never landed past the orphan grace, is removed rather
    than visited. One plan's failure (an outage, a refused hand-off) is counted and logged, never allowed to stop the
    rest, and the plan is visited again on the next tick.
    """
    now = datetime.now(UTC)
    counts = {"open": 0, "resolved": 0, "resubmitted": 0, "errors": 0}
    for action_id, written_at in await asyncio.to_thread(store.open_entries):
        try:
            plan = await asyncio.to_thread(store.read, action_id)
            if plan is None or not plan.is_open:
                await asyncio.to_thread(store.forget_stale_entry, action_id, written_at, now=now)
                continue
            if plan.kind is not kind:
                continue
            counts["open"] += 1
            did = await visit(plan)
            if did != "watching":
                counts[did] += 1
        except Exception:  # noqa: BLE001 — one plan's failure is retried on the next tick
            counts["errors"] += 1
            log.exception("run_plan_sweep_failed", extra={"owner": store.owner, "kind": kind.value, "action_id": action_id})
    pruned = await asyncio.to_thread(store.prune, now=now)
    return SweepReport(pruned=pruned, **counts)


def make_outcome_router(
    *,
    kind: PlanKind,
    authorize: Callable[..., Any],
    store: Callable[..., PlanStore],
    lane: Callable[..., OutcomeLane],
) -> APIRouter:
    """``POST /runs/{action_id}/outcome``: a job reports how its run ended.

    All three callables are FastAPI dependencies, so an app's own settings and overrides reach them: ``authorize``
    decides who may report, ``store`` and ``lane`` resolve the owner's plans and the lane. 404 for an id naming no plan
    of ``kind``, 409 for an outcome the plan already closed against; 200 with the recorded outcome otherwise,
    including a repeat of it.
    """
    router = APIRouter(tags=["run-outcomes"])

    @router.post("/runs/{action_id}/outcome")
    async def report_outcome(
        action_id: str,
        report: OutcomeReport,
        plans: Annotated[PlanStore, Depends(store)],
        outcomes: Annotated[OutcomeLane, Depends(lane)],
        _caller: Annotated[object, Depends(authorize)],
    ) -> RunOutcome:
        plan = await asyncio.to_thread(plans.read, action_id)
        if plan is None or plan.kind is not kind:
            raise HTTPException(status_code=404, detail=f"no {kind.value} run {action_id!r} is planned here")
        try:
            closed = await resolve(plans, plan, report, outcomes, source="job")
        except OutcomeConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except OutcomeNotRecordedError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        recorded = closed.outcome
        if recorded is None:
            raise HTTPException(status_code=503, detail=f"run {action_id!r} did not close")
        return recorded

    return router


def report_outcome(url: str, report: OutcomeReport, *, token_file: Path = DEFAULT_OUTCOME_TOKEN_FILE, timeout_seconds: float = 10.0) -> bool:
    """POST ``report`` to a run's outcome door; answer whether the door recorded it. Never raises.

    The job's side of the door, standard library only so a compute image needs nothing beyond this package. The
    bearer is the pod's projected token, read now (the kubelet rotates it); a pod with no such file presents none,
    which only a door running with authentication off admits. A refused or unreachable report is printed and
    answered ``False``: the sweep resolves a run whose report never arrived, so the job's own exit stands.
    """
    headers = {"Content-Type": "application/json"}
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
        if not token:
            print(f"run outcome not reported: the identity token at {token_file} is empty")
            return False
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=json.dumps(report.model_dump(exclude_none=True)).encode("utf-8"), headers=headers, method="POST")  # noqa: S310 — the URL is the planner's own door
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310 — see above
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError) as exc:
        print(f"run outcome not reported to {url}: {type(exc).__name__}: {exc}")
        return False
