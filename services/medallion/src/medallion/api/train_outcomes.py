"""The producer's two doors onto its planned training runs (CP-029 S2): the job's outcome report and the sweep's cron tick.

THE OUTCOME DOOR, ``POST /runs/{action_id}/outcome``, at the pod root, the same shared door a stage runner mounts for
its stage runs (`service_kit.lakehouse.run_outcomes.make_outcome_router`). A training job reports its terminal here
once its own terminal event landed; the URL rides its submission (`RASK_OUTCOME_URL`). It admits the compute head's
account alone (`service_door.require_outcome_reporter`) and only for an OPEN training plan; every other producer door
authorizes that account on FGA like any other caller, and it holds no `can_administer`. No gateway row reaches it: the
compute head calls the producer's ClusterIP.

THE SWEEP, ``POST /<binding name>``, at the pod root (`api/plan_sweep.py`): the one plan-sweep Component is scoped to
the producer too, and here it sweeps the producer's own training plans (`train_plans.sweep`).
"""

from __future__ import annotations

from fastapi import FastAPI, Request

from medallion.api.dependencies import SettingsDep
from medallion.api.plan_sweep import build_sweep_router
from medallion.api.service_door import require_outcome_reporter
from medallion.services import train_plans
from medallion.services.train_plans import TrainOutcomeLane
from service_kit.lakehouse.run_outcomes import OutcomeLane, make_outcome_router
from service_kit.lakehouse.run_plans import PlanKind, PlanStore


def _store(settings: SettingsDep) -> PlanStore:
    return train_plans.plan_store(settings)


def _lane(request: Request, settings: SettingsDep) -> OutcomeLane:
    return TrainOutcomeLane(settings, request.app.state.dapr)


def mount_train_outcomes(app: FastAPI, *, sweep_binding_name: str) -> None:
    """Mount the outcome door always, and the sweep when a binding name is configured."""
    app.include_router(make_outcome_router(kind=PlanKind.TRAIN, authorize=require_outcome_reporter, store=_store, lane=_lane))
    if sweep_binding_name:
        app.include_router(build_sweep_router(sweep_binding_name, train_plans.sweep))
