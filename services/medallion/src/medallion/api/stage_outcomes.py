"""A stage runner's two doors onto its planned runs (CP-029): the job's outcome report and the sweep's cron tick.

THE OUTCOME DOOR, ``POST /runs/{action_id}/outcome``, at the pod root. A Ray stage job reports its own terminal
state here, to the stage runner that planned it (the URL rides its order). It admits the compute head's account
alone (`service_door.require_outcome_reporter`) and only for an OPEN stage plan; the shared resolution lives in
`service_kit.lakehouse.run_outcomes`, and what a terminal does in `stage_plans.StageOutcomeLane`.

THE SWEEP, ``POST /<binding name>``, at the pod root, where Dapr delivers an input binding (`api/plan_sweep.py`).
"""

from __future__ import annotations

from fastapi import FastAPI, Request

from medallion.api.dependencies import SettingsDep
from medallion.api.plan_sweep import build_sweep_router
from medallion.api.service_door import require_outcome_reporter
from medallion.services import stage_plans
from medallion.services.stage_plans import StageOutcomeLane
from service_kit.lakehouse.run_outcomes import OutcomeLane, make_outcome_router
from service_kit.lakehouse.run_plans import PlanKind, PlanStore


def _store(settings: SettingsDep) -> PlanStore:
    return stage_plans.plan_store(settings)


def _lane(request: Request, settings: SettingsDep) -> OutcomeLane:
    return StageOutcomeLane(settings, request.app.state.dapr)


def mount_stage_outcomes(app: FastAPI, *, sweep_binding_name: str) -> None:
    """Mount the outcome door always, and the sweep when a binding name is configured."""
    app.include_router(make_outcome_router(kind=PlanKind.STAGE, authorize=require_outcome_reporter, store=_store, lane=_lane))
    if sweep_binding_name:
        app.include_router(build_sweep_router(sweep_binding_name, stage_plans.sweep))
