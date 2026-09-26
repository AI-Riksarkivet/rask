"""The producer's door onto a STAGE RUNNER's cascade-stage routes (DWF-MGT-002/003).

Two apps, one operation, and the split is forced by Dapr rather than chosen. `stage_run` executes in
a stage runner's runtime, and `terminate_workflow` resolves the instance through the CALLING app's app-id —
so the terminate has to run in the stage runner's process. But a stage runner is bus-only: no gateway row, no
Ingress, nothing a person can POST to. Hosting only the route there would be a lever nobody can pull.

So: the producer authenticates and AUTHORIZES (it has the gateway row and already runs the dual-auth
door for `/produce` and `/train`), then forwards to the stage runner's ClusterIP with the service token. The
stage runner verifies that token and does the work under its own app-id.

AUTHORIZED ON THE RUN, not on a caller-chosen project (owner ruling 2026-09-25). The producer cannot
read another app's workflow state, so it asks the hosting stage runner first; the stage runner reads
the tenant off the trigger the instance carries, and `can_administer` is checked on THAT project before
a status is returned or a terminate is forwarded. An admin of one project gets 403 on another's run —
as ingest's run doors and the promotion door answer — because these instance ids are content hashes
(`stage-<submission hash>`), so a 403 confirms only an id the caller already held.

This mirrors the promotion review's reasoning in the opposite direction: there, the workflow was moved
to the app that owns the door; here the workflow cannot move, so the door reaches it.
"""

from __future__ import annotations

from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel

from medallion.api.dependencies import FgaClientDep, SettingsDep
from medallion.api.produce_auth import AdmittedCaller, ProducerCaller, authorize_produce, require_project_admin
from medallion.api.stage_ops import no_stage_run
from medallion.core.config import MedallionSettings, outbound_app_token
from service_kit.lakehouse.warehouse_registry import is_safe_project


def _app_token_header(settings: MedallionSettings) -> dict[str, str]:
    """The service credential the stage runner's routes verify, resolved the way they verify it.

    THROUGH `outbound_app_token`, which is the sender-side helper this function once said did not
    exist. It resolves `dapr_auth.expected_app_token()` first and falls back to the typed setting, so
    the header this sends and the token `require_dapr_token` expects come from ONE accessor.

    Reading `settings.app_api_token` directly was the same defect `outbound_app_token` was written to
    fix one caller earlier, and its old justification — "absent in the open dev default, where the
    stage runner's check is a no-op too, so the two stay consistent" — is false wherever the estate
    keeps the token off the environment: measured on the deployed producer 2026-09-19,
    `expected_app_token()` returns a token while `settings.app_api_token` is `''`. The receiving check
    is NOT a no-op there, so the two were consistent only in the deployment that needed it least.
    """
    token = outbound_app_token(settings)
    return {"dapr-api-token": token} if token else {}


#: The cascade operator surface's one path segment, shared with `rerun.py`'s router and forwarded to by
#: the gateway's `/api/stage-runners` row. Declared once so the producer's routers cannot disagree about
#: it; the gateway is a separate deployable, so its half is pinned in `services/gateway/tests/test_lance_routes.py`,
#: which derives every route the producer serves as the chart deploys it and drives a real forward.
STAGE_RUNNERS_PREFIX = "/stage-runners"

router = APIRouter(prefix=STAGE_RUNNERS_PREFIX, tags=["stage-runners"])


class StageRunnerInventory(BaseModel):
    """Which stage runners this producer can reach. Answering "which stage runners exist" is itself an operator
    need — without it a caller has to guess a name to discover the routes."""

    stage_runners: list[str]


def _base_url(settings: Any, stage_runner: str, *, instance_id: str, caller: ProducerCaller) -> str:
    url = (settings.stage_runner_urls or {}).get(stage_runner)
    if not url:
        # A person is not authorized yet (no run has been read) and the runner names are
        # `GET /stage-runners`' to disclose, on `can_administer`. So a person hears what a configured
        # runner answers for a run it does not host, and a made-up name reads as a real one.
        if caller.subject is not None:
            raise no_stage_run(instance_id)
        # 404 and NOT 502: not configured and unreachable are different operator problems. The common
        # cause is a name typo against a values-driven list, so the message names what IS configured.
        known = sorted((settings.stage_runner_urls or {}).keys())
        raise HTTPException(status_code=404, detail=f"no stage runner {stage_runner!r} is configured; known stage runners: {known}")
    return url.rstrip("/")


async def _forward(request: Request, settings: Any, *, stage_runner: str, instance_id: str, action: str = "", method: str, caller: ProducerCaller) -> Any:
    url = f"{_base_url(settings, stage_runner, instance_id=instance_id, caller=caller)}/stages/{instance_id}{action}"
    client: httpx.AsyncClient | None = getattr(request.app.state, "http", None)
    if client is None:
        # Built once in the lifespan; a per-request client re-opens a connection every call, which is
        # the anti-pattern this estate has already paid for once.
        raise HTTPException(status_code=503, detail="the producer has no HTTP client")
    try:
        response = await client.request(method, url, headers=_app_token_header(settings))
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"stage runner {stage_runner!r} is unreachable: {exc}") from exc
    if response.status_code >= 400:
        # The stage runner's own answer, carried through rather than re-invented: a 404 for an unknown
        # instance means the same thing on both sides, and flattening it would lose which.
        raise HTTPException(status_code=response.status_code, detail=response.json().get("detail", response.text))
    return response.json()


@router.get("")
async def list_stage_runners(settings: SettingsDep, _subject: Annotated[str | None, Depends(authorize_produce)]) -> StageRunnerInventory:
    """The deployment's stage-runner names. The same answer for every tenant, so `?project=` only
    chooses which project's admin the caller proves; it cannot select anything to disclose."""
    return StageRunnerInventory(stage_runners=sorted((settings.stage_runner_urls or {}).keys()))


def _run_project(settings: MedallionSettings, state: Any) -> str | None:
    """The project a stage run belongs to, as its hosting stage runner read it, or ``None`` if unknowable.

    ``""`` is a single-tenant run: its trigger carried no project, which is the cascade `/produce` starts
    with none, gated there on the configured project. A missing key is a stage runner that cannot say —
    an unreadable input, or a build that predates the field — and is never read as single-tenant.
    """
    project = state.get("project") if isinstance(state, dict) else None
    if project == "":
        return settings.produce_admin_project
    return project if is_safe_project(project) else None


async def _authorized_run(
    request: Request, *, settings: MedallionSettings, fga_client: OpenFgaClient | None, caller: ProducerCaller, stage_runner: str, instance_id: str
) -> Any:
    """Read the run from the stage runner that hosts it, then authorize the caller on ITS project."""
    state = await _forward(request, settings, stage_runner=stage_runner, instance_id=instance_id, method="GET", caller=caller)
    await require_project_admin(fga_client, caller, project=_run_project(settings, state), resource=f"stage_run:{instance_id}")
    return state


@router.get("/{stage_runner}/stages/{instance_id}")
async def show_stage(stage_runner: str, instance_id: str, request: Request, settings: SettingsDep, fga_client: FgaClientDep, caller: AdmittedCaller) -> Any:
    """DWF-MGT-002 for the cascade: the HTTP view of an in-flight stage. 403 unless the caller administers
    the project the run records."""
    return await _authorized_run(request, settings=settings, fga_client=fga_client, caller=caller, stage_runner=stage_runner, instance_id=instance_id)


@router.post("/{stage_runner}/stages/{instance_id}/terminate", status_code=202)
async def terminate_stage(
    stage_runner: str, instance_id: str, request: Request, settings: SettingsDep, fga_client: FgaClientDep, caller: AdmittedCaller
) -> Any:
    """DWF-MGT-003 for the cascade.

    Whoever administers the project a run belongs to may stop it, and the refusal comes before the
    terminate is forwarded. The stage runner's 202 body — which says the Ray job keeps running — is
    carried through unchanged, because softening it here is exactly how an operator comes to believe
    the GPUs are free.
    """
    await _authorized_run(request, settings=settings, fga_client=fga_client, caller=caller, stage_runner=stage_runner, instance_id=instance_id)
    return await _forward(request, settings, stage_runner=stage_runner, instance_id=instance_id, action="/terminate", method="POST", caller=caller)
