"""The producer's door onto a MOVER's cascade-stage routes (DWF-MGT-002/003).

Two apps, one operation, and the split is forced by Dapr rather than chosen. `stage_run` executes in
a mover's runtime, and `terminate_workflow` resolves the instance through the CALLING app's app-id —
so the terminate has to run in the mover's process. But a mover is bus-only: no gateway row, no
Ingress, nothing a person can POST to. Hosting only the route there would be a lever nobody can pull.

So: the producer authenticates and AUTHORIZES (it has the gateway row and already runs the dual-auth
door for `/produce` and `/train`), then forwards to the mover's ClusterIP with the service token. The
mover verifies that token and does the work under its own app-id.

This mirrors the promotion review's reasoning in the opposite direction: there, the workflow was moved
to the app that owns the door; here the workflow cannot move, so the door reaches it.
"""

from __future__ import annotations

import os
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from medallion.api.dependencies import SettingsDep
from medallion.api.produce_auth import authorize_produce


def _app_token_header() -> dict[str, str]:
    """The service credential the mover's routes verify.

    Built here rather than imported: `dapr_auth` exposes the VERIFIER (`require_dapr_token`) and no
    sender-side helper, and the header name is the one Dapr itself injects. Absent in the open dev
    default, where the mover's check is a no-op too — so the two stay consistent.
    """
    token = os.environ.get("APP_API_TOKEN")
    return {"dapr-api-token": token} if token else {}


router = APIRouter(tags=["movers"])


class MoverInventory(BaseModel):
    """Which movers this producer can reach. Answering "which movers exist" is itself an operator
    need — without it a caller has to guess a name to discover the routes."""

    movers: list[str]


def _base_url(settings: Any, mover: str) -> str:
    url = (settings.mover_urls or {}).get(mover)
    if not url:
        # 404 and NOT 502: the mover is not merely unreachable, it is not configured here at all, and
        # those are different operator problems. The message names what IS configured, because the
        # common cause is a name typo against a values-driven list.
        known = sorted((settings.mover_urls or {}).keys())
        raise HTTPException(status_code=404, detail=f"no mover {mover!r} is configured; known movers: {known}")
    return url.rstrip("/")


async def _forward(request: Request, settings: Any, mover: str, path: str, *, method: str) -> Any:
    url = f"{_base_url(settings, mover)}{path}"
    client: httpx.AsyncClient | None = getattr(request.app.state, "http", None)
    if client is None:
        # Built once in the lifespan; a per-request client re-opens a connection every call, which is
        # the anti-pattern this estate has already paid for once.
        raise HTTPException(status_code=503, detail="the producer has no HTTP client")
    try:
        response = await client.request(method, url, headers=_app_token_header())
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"mover {mover!r} is unreachable: {exc}") from exc
    if response.status_code >= 400:
        # The mover's own answer, carried through rather than re-invented: a 404 for an unknown
        # instance means the same thing on both sides, and flattening it would lose which.
        raise HTTPException(status_code=response.status_code, detail=response.json().get("detail", response.text))
    return response.json()


@router.get("/movers")
async def list_movers(settings: SettingsDep, _subject: Annotated[str | None, Depends(authorize_produce)]) -> MoverInventory:
    return MoverInventory(movers=sorted((settings.mover_urls or {}).keys()))


@router.get("/movers/{mover}/stages/{instance_id}")
async def show_stage(mover: str, instance_id: str, request: Request, settings: SettingsDep, _subject: Annotated[str | None, Depends(authorize_produce)]) -> Any:
    """DWF-MGT-002 for the cascade: an in-flight stage was unobservable over HTTP entirely."""
    return await _forward(request, settings, mover, f"/stages/{instance_id}", method="GET")


@router.post("/movers/{mover}/stages/{instance_id}/terminate", status_code=202)
async def terminate_stage(
    mover: str, instance_id: str, request: Request, settings: SettingsDep, _subject: Annotated[str | None, Depends(authorize_produce)]
) -> Any:
    """DWF-MGT-003 for the cascade.

    Gated by the same door as `/produce`: whoever may start this tenant's pipeline may stop it. The
    mover's 202 body — which says the Ray job keeps running — is carried through unchanged, because
    softening it here is exactly how an operator comes to believe the GPUs are free.
    """
    return await _forward(request, settings, mover, f"/stages/{instance_id}/terminate", method="POST")
