"""The cascade's operator surface: observe and stop a planned stage run (DWF-MGT-002/003 on plans, CP-029 D-8).

THESE ROUTES LIVE ON THE STAGE RUNNER because the plan does: a run's plan is this stage runner's, under its own
identity in the control root, and the sweep that resolves it runs here. The stage runner has no gateway row and no
Ingress, so it is reached through the producer: `stage_runner_ops` authenticates the caller, authorizes it on the
project the run's plan RECORDS, and forwards here over the stage runner's ClusterIP with its own projected
`rask-medallion` token. This side admits the producer's account and no other (`service_door.require_producer`), so
the ClusterIP is not a way round that check.

show answers the plan's identity and state with ONE engine read while the run is open, never the order it carries
(URIs and a provenance document a status question must not disclose). terminate stops the job through the executor
port and resolves the plan on the state the engine reports after the stop (`stage_plans.terminate`): a stopped job
closes the run FAILED and the next tier is never woken, a job that had already succeeded closes it SUCCEEDED and the
next tier wakes, and a stop the engine has not finished leaves the run for the sweep.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from medallion.api.dependencies import SettingsDep
from medallion.api.service_door import ProducerService
from medallion.services import stage_plans
from medallion.services.stage_plans import StageHandOffError, StageRunView, StageStopError


router = APIRouter(tags=["stages"])


class StageTerminated(BaseModel):
    instance_id: str
    status: str
    detail: str


@router.get("/stages/{instance_id}")
async def show_stage(instance_id: str, settings: SettingsDep, _producer: ProducerService) -> StageRunView:
    """The HTTP view of a planned stage run: its plan, and the engine's state while it is open."""
    view = await stage_plans.show(settings, instance_id)
    if view is None:
        raise HTTPException(status_code=404, detail=f"no stage run {instance_id!r}")
    return view


@router.post("/stages/{instance_id}/terminate", status_code=202)
async def terminate_stage(instance_id: str, request: Request, settings: SettingsDep, _producer: ProducerService) -> StageTerminated:
    """Stop a planned stage run's job and close its plan on how the job ended; a run that already closed is answered as it stands."""
    try:
        plan = await stage_plans.terminate(settings, request.app.state.dapr, instance_id)
    except StageStopError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except StageHandOffError as exc:
        raise HTTPException(
            status_code=503, detail=f"the job had already succeeded and its next tier could not be woken yet; the sweep repeats it: {exc}"
        ) from exc
    if plan is None:
        raise HTTPException(status_code=404, detail=f"no stage run {instance_id!r}")
    outcome = plan.outcome
    if outcome is None:
        detail = "the stop was sent and the job has not ended yet; the plan sweep closes the run on the state it ends in"
    elif outcome.source == "operator" and outcome.status == "succeeded":
        detail = "the job had already succeeded before the stop; the run closed succeeded and its next tier is woken"
    elif outcome.source == "operator":
        detail = "the job was stopped through the compute engine and the run closed failed; no downstream trigger is published"
    else:
        detail = "the run had already closed; nothing was stopped"
    return StageTerminated(instance_id=instance_id, status=outcome.status if outcome is not None else "stopping", detail=detail)
