"""The producer's door onto a STAGE RUNNER's cascade-stage routes (DWF-MGT-002/003).

Two apps, one operation, and the split is forced by Dapr rather than chosen. `stage_run` executes in
a stage runner's runtime, and `terminate_workflow` resolves the instance through the CALLING app's app-id —
so the terminate has to run in the stage runner's process. But a stage runner is bus-only: no gateway row, no
Ingress, nothing a person can POST to. Hosting only the route there would be a lever nobody can pull.

So: the producer authenticates and AUTHORIZES (it has the gateway row and already runs the dual-auth
door for `/produce` and `/train`), then forwards to the stage runner's ClusterIP with the service token. The
stage runner verifies that token and does the work under its own app-id.

This mirrors the promotion review's reasoning in the opposite direction: there, the workflow was moved
to the app that owns the door; here the workflow cannot move, so the door reaches it.
"""

from __future__ import annotations

from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from medallion.api.dependencies import SettingsDep
from medallion.api.produce_auth import authorize_produce
from medallion.core.config import MedallionSettings, outbound_app_token


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
#: it; the gateway is a separate deployable, so its half is pinned against the producer's OpenAPI and a
#: real forward in `services/gateway/tests/test_lance_routes.py`.
STAGE_RUNNERS_PREFIX = "/stage-runners"

router = APIRouter(prefix=STAGE_RUNNERS_PREFIX, tags=["stage-runners"])


class StageRunnerInventory(BaseModel):
    """Which stage runners this producer can reach. Answering "which stage runners exist" is itself an operator
    need — without it a caller has to guess a name to discover the routes."""

    stage_runners: list[str]


def _base_url(settings: Any, stage_runner: str) -> str:
    url = (settings.stage_runner_urls or {}).get(stage_runner)
    if not url:
        # 404 and NOT 502: the stage runner is not merely unreachable, it is not configured here at all, and
        # those are different operator problems. The message names what IS configured, because the
        # common cause is a name typo against a values-driven list.
        known = sorted((settings.stage_runner_urls or {}).keys())
        raise HTTPException(status_code=404, detail=f"no stage runner {stage_runner!r} is configured; known stage runners: {known}")
    return url.rstrip("/")


async def _forward(request: Request, settings: Any, stage_runner: str, path: str, *, method: str) -> Any:
    url = f"{_base_url(settings, stage_runner)}{path}"
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
    return StageRunnerInventory(stage_runners=sorted((settings.stage_runner_urls or {}).keys()))


@router.get("/{stage_runner}/stages/{instance_id}")
async def show_stage(
    stage_runner: str, instance_id: str, request: Request, settings: SettingsDep, _subject: Annotated[str | None, Depends(authorize_produce)]
) -> Any:
    """DWF-MGT-002 for the cascade: an in-flight stage was unobservable over HTTP entirely."""
    return await _forward(request, settings, stage_runner, f"/stages/{instance_id}", method="GET")


@router.post("/{stage_runner}/stages/{instance_id}/terminate", status_code=202)
async def terminate_stage(
    stage_runner: str, instance_id: str, request: Request, settings: SettingsDep, _subject: Annotated[str | None, Depends(authorize_produce)]
) -> Any:
    """DWF-MGT-003 for the cascade.

    Gated by the same door as `/produce`: whoever may start this tenant's pipeline may stop it. The
    stage runner's 202 body — which says the Ray job keeps running — is carried through unchanged, because
    softening it here is exactly how an operator comes to believe the GPUs are free.
    """
    return await _forward(request, settings, stage_runner, f"/stages/{instance_id}/terminate", method="POST")
