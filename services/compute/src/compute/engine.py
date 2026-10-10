"""compute's engine door: the Ray engine as the lakehouse reaches it, by Dapr service invocation (CP-056).

A planner (a medallion stage runner, or the producer for training) invokes app-id ``compute`` on the routes
`service_kit.lakehouse.engine_door` names; this module answers them against the Ray dashboard's Jobs API
(`ray_kit.jobs_api`). The lakehouse knows no engine: it holds a `WorkOrder` and an app-id, and everything that is
Ray's — the command a task runs, the Jobs-API body, the state vocabulary, the SIGKILL classification — lives here.

THE ORDER CROSSES A TRUST BOUNDARY HERE, and Ray lays a job's env over its pod's, so the door admits only what a
caller may decide. The task resolves through THIS service's table (``RASK_COMPUTE_ENGINE_TASKS``) and an unknown one
is refused 422, so no caller string becomes a command. The env is `WorkOrder.to_env()`, whose names are the platform's
own (`RASK_*`, `RASK_PARAM_<variable name>`, `TRACEPARENT`, `TRACESTATE`, `OTEL_SERVICE_NAME`): no OTLP endpoint and
no free variable name rides an order. The outcome URL must be `<base>/runs/<the key>/outcome` for a planner base in
``RASK_COMPUTE_ENGINE_OUTCOME_BASES``, because the job posts its pod's projected token there. A run is named only by
a 40-hex key (`work_order.derive_idempotency_key`), so a caller can neither read nor stop a Ray job no planner
submitted. The job reports its own terminal on that outcome door; nothing here watches it.

THE CALLER IS A SERVICE ACCOUNT the door's ``RASK_SA_SUBJECTS`` maps: the planners, each by its projected token for
this door's audience. An account the map does not name is refused, so that map is the list of who may spend the
cluster. A caller arriving through a public front door is refused even with a valid token.
"""

import re
from http import HTTPStatus
from typing import Annotated, Any, Final

import httpx
from fastapi import APIRouter, Depends, Header, Path, Request
from lance_namespace import UnauthenticatedError
from starlette.concurrency import run_in_threadpool

from compute.dependencies import ComputeSettingsDep
from ray_kit import jobs_api
from ray_kit.jobs_api import RayJobError
from service_kit.exceptions import DomainError, ForbiddenError, ServiceUnavailableError, UnauthorizedError, UnprocessableEntityError
from service_kit.governed.audit import DENY, FAILURE, SUCCESS, audit
from service_kit.governed.dapr_auth import is_public_caller
from service_kit.governed.machine_identity import ServiceAccountVerifier
from service_kit.governed.oidc import ProviderUnavailableError
from service_kit.lakehouse.engine_door import ENGINE_RUNS_PATH, EngineRunFailure, EngineRunState, EngineSubmission, EngineSubmitted
from service_kit.lakehouse.executor import RunFailure, RunState, SubmitOutcome


#: POSIX SIGKILL: on this engine the host-RAM OOM that kills a job with no other symptom, so it is classified.
_SIGKILL_EXIT_CODE: Final = 137

#: Ray's job states in the port's vocabulary. STOPPED is a cancelled run, not a failure: a cancel is somebody's
#: decision, a failure licenses a resubmit.
_RUN_STATE: Final = {
    "PENDING": RunState.PENDING,
    "RUNNING": RunState.RUNNING,
    "SUCCEEDED": RunState.SUCCEEDED,
    "FAILED": RunState.FAILED,
    "STOPPED": RunState.CANCELLED,
}

_SUBMIT_OUTCOME: Final = {
    "submitted": SubmitOutcome.SUBMITTED,
    "reattached": SubmitOutcome.REATTACHED,
    "resubmitted": SubmitOutcome.RESUBMITTED,
    "already_failed": SubmitOutcome.ALREADY_FAILED,
}


class EngineUnreachableError(DomainError):
    """Ray could not be reached or refused the call: not evidence about the run, so the planner leaves its plan."""

    status_code = HTTPStatus.BAD_GATEWAY
    title = "Bad Gateway"


def _unreachable(exc: Exception) -> EngineUnreachableError:
    return EngineUnreachableError(f"the Ray dashboard could not answer: {exc}")


async def require_engine_caller(
    request: Request,
    settings: ComputeSettingsDep,
    authorization: Annotated[str | None, Header()] = None,
    dapr_caller_app_id: Annotated[str | None, Header()] = None,
) -> str:
    """The planner calling: the subject its projected token maps to in this door's ``RASK_SA_SUBJECTS``.

    With no service-account issuer configured the door authenticates nobody, and admits only where an operator
    acknowledged that (``RASK_INSECURE_ALLOW_UNAUTHENTICATED``).
    """
    resource = request.url.path
    verifier: ServiceAccountVerifier | None = getattr(request.app.state, "sa_oidc", None)
    if verifier is None:
        if settings.sa_issuer:
            audit("authn", FAILURE, resource=resource, reason="verifier_unavailable")
            raise ServiceUnavailableError("service-account authentication is configured but unavailable")
        if settings.insecure_allow_unauthenticated:
            return "anonymous"
        raise ForbiddenError("the engine door can authenticate nobody: set RASK_SA_ISSUER, or RASK_INSECURE_ALLOW_UNAUTHENTICATED to run it open")
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token or not verifier.issued(token):
        audit("authn", FAILURE, resource=resource, reason="missing_or_foreign_token")
        raise UnauthorizedError("the engine door admits only a service-account token")
    try:
        principal = await run_in_threadpool(verifier.verify, token)
    except UnauthenticatedError:
        audit("authn", FAILURE, resource=resource, reason="invalid_token")
        raise UnauthorizedError("invalid token") from None
    except ProviderUnavailableError as exc:
        audit("authn", FAILURE, resource=resource, reason="verifier_unavailable")
        raise ServiceUnavailableError("service-account authentication is unavailable") from exc
    if is_public_caller(dapr_caller_app_id):
        audit("authn", DENY, subject=principal.sub, resource=resource, reason="public_caller")
        raise ForbiddenError(f"{dapr_caller_app_id!r} is a public front door, and a planner never arrives through one")
    audit("authn", SUCCESS, subject=principal.sub, resource=resource)
    return principal.subject


EngineCaller = Annotated[str, Depends(require_engine_caller)]


def get_jobs_api(request: Request) -> httpx.AsyncClient:
    """The lifespan's pooled client for the Ray dashboard's Jobs API."""
    return request.app.state.jobs_api


JobsApi = Annotated[httpx.AsyncClient, Depends(get_jobs_api)]

router = APIRouter(prefix=ENGINE_RUNS_PATH, tags=["engine"])

#: A run's name: the 40-hex key `derive_idempotency_key` mints. Anything else is not a run a planner submitted.
RunKey = Annotated[str, Path(pattern=r"^[0-9a-f]{40}$")]


def _metadata(submission: EngineSubmission) -> dict[str, str]:
    """The facts read BACK off the Jobs API: who a dead job was for, and which declaration it ran. Empty is omitted."""
    order = submission.order
    facts = (
        ("rask.originator", order.identity.originator),
        ("rask.project", order.identity.project),
        ("rask.token", order.stamp.token),
        ("rask.stage", order.stamp.stage),
        ("rask.transform", order.stamp.transform),
    )
    return {key: value for key, value in facts if value}


@router.post("")
async def submit_run(submission: EngineSubmission, settings: ComputeSettingsDep, client: JobsApi, _caller: EngineCaller) -> EngineSubmitted:
    """Start the order's job under its key, or re-attach to the one already running it.

    A prior FAILED job under the key is replaced when the order asks for that (a stage) and answered
    ``already_failed`` otherwise (a training run). 422 for a task this service's table does not name, 502 when Ray
    cannot be reached or refuses the job.
    """
    order = submission.order
    command = settings.engine_tasks.get(order.task)
    if command is None:
        raise UnprocessableEntityError(f"task {order.task!r} is not one this engine runs; it runs {sorted(settings.engine_tasks)}")
    if not re.fullmatch(r"[0-9a-f]{40}", order.idempotency_key):
        raise UnprocessableEntityError("the order's key is not a run key (`derive_idempotency_key`)")
    reports_to = {f"{base.rstrip('/')}/runs/{order.idempotency_key}/outcome" for base in settings.engine_outcome_bases}
    if order.outcome_url and order.outcome_url not in reports_to:
        raise UnprocessableEntityError("the order's outcome URL is not this run's door on a planner this engine reports to")
    body: dict[str, Any] = {
        "submission_id": order.idempotency_key,
        "entrypoint": command,
        "runtime_env": {"env_vars": order.to_env()},
        "metadata": _metadata(submission),
    }
    try:
        answered = await jobs_api.submit_or_reattach(
            client, order.idempotency_key, body, on_terminal_failure="resubmit" if order.replace_failed_run else "report"
        )
    except RayJobError as exc:
        raise _unreachable(exc) from exc
    return EngineSubmitted(handle=order.idempotency_key, outcome=_SUBMIT_OUTCOME[answered])


@router.get("/{key}")
async def run_state(key: RunKey, client: JobsApi, _caller: EngineCaller) -> EngineRunState:
    """Where the run is; ``unknown`` only when Ray answers that it holds no job under the key."""
    try:
        raw = await jobs_api.job_status(client, key)
    except RayJobError as exc:
        raise _unreachable(exc) from exc
    return EngineRunState(state=_RUN_STATE.get(raw or "", RunState.UNKNOWN))


@router.get("/{key}/failure")
async def run_failure(key: RunKey, client: JobsApi, _caller: EngineCaller) -> EngineRunFailure:
    """Ray's cause, classified: exit 137 is ``oom`` under any engine, else Ray's own error type, else ``unknown``."""
    try:
        raw = await jobs_api.job_failure(client, key)
    except RayJobError as exc:
        raise _unreachable(exc) from exc
    if raw is None:
        return EngineRunFailure()
    kind = "oom" if raw.driver_exit_code == _SIGKILL_EXIT_CODE else (raw.error_type or "unknown")
    return EngineRunFailure(failure=RunFailure(kind=kind, message=raw.message or "", exit_code=raw.driver_exit_code))


@router.post("/{key}/cancel")
async def cancel_run(key: RunKey, client: JobsApi, _caller: EngineCaller) -> EngineRunState:
    """Stop the job (`POST /api/jobs/{key}/stop`; a DELETE stops nothing on a running job) and answer the state after.

    A refused or unreachable stop is 502, so nobody is told a running job stopped.
    """
    try:
        response = await client.post(f"/api/jobs/{key}/stop")
        response.raise_for_status()
        raw = await jobs_api.job_status(client, key)
    except (httpx.HTTPError, RayJobError) as exc:
        raise _unreachable(exc) from exc
    return EngineRunState(state=_RUN_STATE.get(raw or "", RunState.UNKNOWN))
