"""``POST /train`` head + the ``/train-trigger`` subscription (#115a, docs/RAY-TRAIN.md D1/D2).

Training gets its OWN topic and consumer: the trigger is fire-and-track (submit-and-ack), never a
stage hop — see the design doc for why a workload-type field on the medallion trigger was rejected.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from dapr.ext.fastapi import DaprApp
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from lance_namespace import ErrorCode, ServiceUnavailableError
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel, Field

from medallion.api.dependencies import DaprClientDep, FgaClientDep, SettingsDep
from medallion.api.produce_auth import AdmittedCaller, ProducerCaller, authorize_train, require_project_admin
from medallion.core.config import MedallionSettings, get_settings
from medallion.services import train_plans
from medallion.services.train import (
    DATASET_PATTERN,
    MAX_FEATURES,
    MODEL_PATTERN,
    TOKEN_PATTERN,
    handle_train_trigger,
    submit_train_request,
    train_head_enabled,
)
from medallion.services.train_plans import TrainRunState, TrainStopError
from service_kit.draining import refuse_when_draining, retry_when_draining
from service_kit.governed.dapr_auth import require_dapr_token
from service_kit.governed.signing_key import retry_until_signed
from service_kit.lakehouse.ns_errors import problem_body
from service_kit.lakehouse.run_plans import PlanDocument
from service_kit.lakehouse.warehouse_registry import is_safe_project


log = logging.getLogger(__name__)
router = APIRouter(tags=["train"])


class FeatureRef(BaseModel):
    """One training input: a ``stage$name`` feature dataset, optionally pinned to an exact version."""

    dataset: str = Field(pattern=DATASET_PATTERN)
    version: int | None = None


class TrainRequest(BaseModel):
    """The ``POST /train`` body — pointers only (claim-check): names, pins, and a SMALL config.

    Name shapes and the feature cap mirror the CONSUMER's validation exactly (review 2026-07-11):
    a request the consumer would DROP is refused HERE with a 422, never 202'd into a silent no-op.
    """

    model: str = Field(pattern=MODEL_PATTERN)
    features: list[FeatureRef] = Field(min_length=1, max_length=MAX_FEATURES)
    config: dict[str, Any] = Field(default_factory=dict)


#: This door's statuses mapped to the spec's numeric error codes. A literal per site would drift; the
#: map is small because the door answers exactly three ways.
_CODES: dict[int, ErrorCode] = {
    409: ErrorCode.INVALID_TABLE_STATE,
    422: ErrorCode.INVALID_INPUT,
    503: ErrorCode.SERVICE_UNAVAILABLE,
}


def _problem(status: int, title: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        headers={"Retry-After": "5"} if status == 503 else None,
        # SHARED builder — see `ns_errors.problem_body`. This door is non-spec, so the missing `code`
        # broke no generated client; it made the estate answer the same class of failure in two
        # different shapes, which is the thing the comment below already claimed it did not.
        content=problem_body(_CODES[status], status=status, title=title, detail=detail),
    )


@router.post(
    "/train",
    status_code=202,
    response_model=None,
    # B6: a draining pod must not START work it cannot finish. 503 + Retry-After rather than a
    # 4xx — the caller's request is fine, this replica is simply leaving.
    dependencies=[Depends(refuse_when_draining)],
)
async def train(
    body: TrainRequest,
    dapr: DaprClientDep,
    settings: SettingsDep,
    # #64: a service or a person administering the CONFIGURED project — pinned, a stray ?project= is ignored
    # (single-tenant write; see authorize_train). It hands back the verified sub on the human path,
    # which is the ONLY moment this run is attributable: everything after here is a bus trigger and a
    # detached Ray job, and the job's own events author as `service-trainer` by design.
    originator: Annotated[str | None, Depends(authorize_train)],
    # The consumer's token shape, not the sibling doors' key shape: this key becomes the training
    # token, and a key the consumer would DROP must be a 422 here (see `TOKEN_PATTERN`).
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=64, pattern=TOKEN_PATTERN)],
) -> dict[str, Any] | JSONResponse:
    """Request a training run: pin feature versions (omitted → LATEST, resolved HERE) and publish the
    training trigger — 202 with the correlation ``token``. Token-guarded like ``/produce``; a disabled
    head (no Ray path / S3 / bronze URI) is an explicit 409, a lost trigger an explicit 503 — never a 202
    that silently trains nothing."""
    if not train_head_enabled(settings):
        return _problem(409, "Conflict", "train head not configured (needs ray_enabled + S3 + bronze URI)")
    result = await submit_train_request(
        dapr,
        settings,
        model=body.model,
        features=[f.model_dump() for f in body.features],
        config=body.config,
        token=idempotency_key,
        originator=originator or "",
    )
    if result.get("status") == "resolve_failed":
        return _problem(422, "ValidationError", f"cannot resolve feature dataset {result['dataset']!r}")
    if result.get("status") == "publish_failed":
        return _problem(503, "ServiceUnavailable", "training trigger publish failed; retry")
    return result


def register_train_trigger_route(app: FastAPI, dapr_app: DaprApp | None = None) -> DaprApp:
    """Register the training-trigger subscription on ``app`` (reusing the producer's ``DaprApp``)."""
    settings = get_settings()
    dapr_app = dapr_app or DaprApp(app)

    @dapr_app.subscribe(
        pubsub=settings.pubsub,
        topic=settings.train_topic,
        route="/train-trigger",
        dead_letter_topic=settings.dlq_topic or None,
    )
    async def on_train_trigger(
        event: dict[str, Any],
        request: Request,
        config: SettingsDep,
        _: Annotated[None, Depends(require_dapr_token)],
        drain: Annotated[dict[str, str] | None, Depends(retry_when_draining)] = None,
        signing: Annotated[dict[str, str] | None, Depends(retry_until_signed)] = None,
    ) -> dict[str, str]:
        """Thin wrapper over the testable :func:`handle_train_trigger` (submit-and-ack, D2).
        Authenticated by the Dapr app-api-token so a forged trigger can't spend training compute; the
        FGA client is the host app's (``app.state.fga`` — built by the producer lifespan when
        RASK_FGA_ENABLED, ``None`` otherwise → gate off, symmetric with the stage runners)."""
        if drain is not None:
            return drain
        if signing is not None:
            return signing
        fga_client = getattr(request.app.state, "fga", None)
        return await handle_train_trigger(config, event, dapr=request.app.state.dapr, fga_client=fga_client)

    return dapr_app


class TrainTerminateAccepted(BaseModel):
    instance_id: str
    detail: str


def _run_project(settings: MedallionSettings, recorded: str) -> str | None:
    """The project a training run RECORDS on its plan, or ``None`` when that value names no project.

    By construction that is the configured project: the trigger consumer stamps `produce_admin_project` on every
    plan, whatever the trigger claims, so a plan recording none is the configured one, the project `POST /train`
    authorized. The gate reads the record rather than the setting, so it stays on the resource's own tenant.
    """
    if recorded == "":
        return settings.produce_admin_project
    return recorded if is_safe_project(recorded) else None


def _no_run(instance_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"no training run {instance_id!r}")


async def _authorized_run(settings: MedallionSettings, fga_client: OpenFgaClient | None, caller: ProducerCaller, instance_id: str) -> PlanDocument:
    """Load the TRAINING plan (404 for anything else), then authorize the caller on the project it records.

    A plan document that does not parse names no project, so the caller is refused it (503) rather than checked
    against the configured project, which would hand an unknown run to that project's admins.
    """
    resource = f"train_run:{instance_id}"
    try:
        plan = await train_plans.read(settings, instance_id)
    except ValueError:  # a document that does not parse as JSON, or as a plan (pydantic's ValidationError is a ValueError)
        log.warning("medallion_train_plan_unreadable", extra={"instance_id": instance_id})
        await require_project_admin(fga_client, caller, project=None, resource=resource)
        raise ServiceUnavailableError(f"the plan of training run {instance_id!r} cannot be read") from None
    if plan is None:
        raise _no_run(instance_id)
    await require_project_admin(fga_client, caller, project=_run_project(settings, plan.project), resource=resource)
    return plan


@router.get("/trains/{instance_id}")
async def show_train(instance_id: str, settings: SettingsDep, fga_client: FgaClientDep, caller: AdmittedCaller) -> TrainRunState:
    """DWF-MGT-002 on plans: the HTTP view of a planned training run. `POST /train` answers 202, and this is how a
    caller learns whether the run is still training, landed, or failed; an id no training plan has is 404.

    ``instance_id`` is the plan's action id, `ray-train-<token>`. Gated on `can_administer` over the project the plan
    records: reading the status of compute you may not spend is not public, and the estate argues this exact point
    on `flows.get_run` and `ingest.get_ingest`.
    """
    plan = await _authorized_run(settings, fga_client, caller, instance_id)
    return await train_plans.state(plan)


@router.post("/trains/{instance_id}/terminate", status_code=202)
async def terminate_train(
    instance_id: str, request: Request, settings: SettingsDep, fga_client: FgaClientDep, caller: AdmittedCaller
) -> TrainTerminateAccepted:
    """DWF-MGT-003 on plans: stop a training run's job through the executor port, then close its plan on the state the stop left.

    Refused before anything is stopped unless the caller administers the project the plan records. The stop frees the
    run's GPUs; the engine's state after it decides the terminal (`train_plans.terminate`), so a job that had already
    published its model closes succeeded, never FAILED. A run that already closed is answered as it stands.
    """
    plan = await _authorized_run(settings, fga_client, caller, instance_id)
    try:
        closed = await train_plans.terminate(settings, request.app.state.dapr, plan)
    except TrainStopError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    log.info("medallion_train_run_termination_requested", extra={"instance_id": instance_id, "subject": caller.subject})
    if closed.outcome is None:
        detail = "the stop was asked and the job has not ended yet; the sweep closes the run on the state it ends in"
    elif closed.outcome.source == "operator":
        detail = f"the training job was stopped through the compute engine and the run closed {closed.outcome.status}"
    else:
        detail = f"the run had already closed {closed.outcome.status}; nothing was stopped"
    return TrainTerminateAccepted(instance_id=instance_id, detail=detail)
