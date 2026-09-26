"""``POST /train`` head + the ``/train-trigger`` subscription (#115a, docs/RAY-TRAIN.md D1/D2).

Training gets its OWN topic and consumer: the trigger is fire-and-track (submit-and-ack), never a
stage hop — see the design doc for why a workload-type field on the medallion trigger was rejected.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from typing import Annotated, Any

from dapr.ext.fastapi import DaprApp
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from lance_namespace import ErrorCode
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel, Field

from medallion.api.dependencies import DaprClientDep, FgaClientDep, SettingsDep
from medallion.api.produce_auth import AdmittedCaller, ProducerCaller, authorize_train, require_project_admin
from medallion.core.config import MedallionSettings, get_settings
from medallion.services.train import (
    DATASET_PATTERN,
    MAX_FEATURES,
    MODEL_PATTERN,
    TOKEN_PATTERN,
    TRAIN_WATCH_PREFIX,
    handle_train_trigger,
    submit_train_request,
    train_head_enabled,
)
from service_kit.draining import refuse_when_draining, retry_when_draining
from service_kit.exceptions import ServiceUnavailableError
from service_kit.governed.dapr_auth import require_dapr_token
from service_kit.lakehouse.ns_errors import problem_body
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
    # #64: service token OR an admin of the CONFIGURED project — pinned, a stray ?project= is ignored
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
    ) -> dict[str, str]:
        """Thin wrapper over the testable :func:`handle_train_trigger` (submit-and-ack, D2).
        Authenticated by the Dapr app-api-token so a forged trigger can't spend training compute; the
        FGA client is the host app's (``app.state.fga`` — built by the producer lifespan when
        RASK_FGA_ENABLED, ``None`` otherwise → gate off, symmetric with the stage runners)."""
        if drain is not None:
            return drain
        fga_client = getattr(request.app.state, "fga", None)
        return await handle_train_trigger(config, event, fga_client=fga_client)

    return dapr_app


class TrainRunState(BaseModel):
    """What an operator needs to answer "is my training run still being watched".

    A declared field list, not the SDK's state object: `WorkflowState` carries the serialized input
    and the serialized output, and this route is reachable by any admin of the watch's project — so it
    would disclose the whole spec to answer a status question.
    """

    instance_id: str
    status: str
    #: The Ray submission the watcher is polling, echoed so an operator can cross-check the dashboard.
    submission_id: str | None = None


class TrainTerminateAccepted(BaseModel):
    instance_id: str
    detail: str


def _train_client(request: Request) -> Any:
    """The lifespan's client, never a per-request one.

    `decide()` on the promotion router builds its own and its sibling `show()` reads this one; that
    asymmetry is its own audit row. New code takes the documented side: constructing a client per
    request re-opens a gRPC channel to the sidecar on every call.
    """
    client = getattr(request.app.state, "workflow_client", None)
    if client is None:
        raise ServiceUnavailableError("the workflow engine is not available")
    return client


def _watch_project(settings: MedallionSettings, serialized_input: str | None) -> str | None:
    """The project a training watch RECORDS (`TrainJobSpec.project`), or ``None`` if it cannot be read.

    By construction that is the configured project: the trigger consumer stamps `produce_admin_project`
    on every watch, whatever the trigger claims, so a watch recording none is the configured one — the
    project `POST /train` authorized. The gate reads the record rather than the setting, so it stays
    on the resource's own tenant.
    """
    try:
        spec = json.loads(serialized_input or "{}")
    except ValueError:
        return None
    project = spec.get("project", "") if isinstance(spec, dict) else None
    if project == "":
        return settings.produce_admin_project
    return project if is_safe_project(project) else None


def _no_watch(instance_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"no training watch {instance_id!r}")


async def _authorized_watch(
    request: Request, *, settings: MedallionSettings, fga_client: OpenFgaClient | None, caller: ProducerCaller, instance_id: str
) -> Any:
    """Load the TRAINING watch (404 for anything else), then authorize the caller on the project it records.

    This engine also hosts `promotion_review`, whose door is `/promotions/{id}` on `can_promote`. A
    training door that acted on one would skip that rung and the promotion's outcome event, so an id
    `schedule_train_watch` cannot mint is refused before the engine is asked, and a state whose
    workflow is not `train_run` answers exactly as an absent one.
    """
    if not instance_id.startswith(TRAIN_WATCH_PREFIX):
        raise _no_watch(instance_id)
    client = _train_client(request)
    # Resolved here, as `promotions.py` does: `medallion.workflow` is the engine adapter, an optional
    # extra, and a router the producer always mounts must not import it. After the client, because a
    # live client means the extra is installed; without one the answer is the 503 above.
    from medallion.workflow import train_run

    # The SDK client is SYNCHRONOUS. Awaiting it inline blocks the event loop for every other request
    # on this worker — the same reason ingest and flows read their state through a thread.
    state = await asyncio.to_thread(lambda: client.get_workflow_state(instance_id, fetch_payloads=True))
    # `name` is the orchestration's registered name, which `workflow.register` leaves as `__name__`.
    if state is None or state.name != train_run.__name__:
        raise _no_watch(instance_id)
    await require_project_admin(fga_client, caller, project=_watch_project(settings, state.serialized_input), resource=f"{train_run.__name__}:{instance_id}")
    return state


@router.get("/trains/{instance_id}")
async def show_train(instance_id: str, request: Request, settings: SettingsDep, fga_client: FgaClientDep, caller: AdmittedCaller) -> TrainRunState:
    """DWF-MGT-002: the HTTP view of a training watch. `POST /train` answers 202, and this is how a
    caller learns whether the watcher is alive or has abandoned the run; an id no watch has is 404.

    Gated on `can_administer` over the project the watch records: reading the status of compute you
    may not spend is not public, and the estate argues this exact point on `flows.get_run` and
    `ingest.get_ingest`.
    """
    state = await _authorized_watch(request, settings=settings, fga_client=fga_client, caller=caller, instance_id=instance_id)
    submission_id: str | None = None
    with suppress(Exception):
        # Best-effort: a state whose input this build cannot parse must still answer the STATUS
        # question, which is the one the caller asked.
        submission_id = str(json.loads(state.serialized_input or "{}").get("submission_id") or "") or None
    return TrainRunState(instance_id=instance_id, status=str(getattr(state.runtime_status, "name", state.runtime_status)), submission_id=submission_id)


@router.post("/trains/{instance_id}/terminate", status_code=202)
async def terminate_train(
    instance_id: str, request: Request, settings: SettingsDep, fga_client: FgaClientDep, caller: AdmittedCaller
) -> TrainTerminateAccepted:
    """DWF-MGT-003, for the training lane. Refused before the terminate unless the caller administers
    the project the watch records.

    A HARD terminate is honest here and the response says exactly what it does and does not do.
    `train_run` only POLLS a Ray job it did not submit — `submit_train_job` did, before the watcher
    was ever scheduled — so stopping the watch does NOT stop the training job or free its GPUs. An
    operator told "terminated" would reasonably believe otherwise, so the body refuses to imply it.
    """
    await _authorized_watch(request, settings=settings, fga_client=fga_client, caller=caller, instance_id=instance_id)
    client = _train_client(request)
    await asyncio.to_thread(lambda: client.terminate_workflow(instance_id))
    log.info("medallion_train_watch_termination_requested", extra={"instance_id": instance_id, "subject": caller.subject})
    return TrainTerminateAccepted(
        instance_id=instance_id,
        detail="the watch stops; the Ray training job it was polling keeps running and must be stopped through Ray",
    )
