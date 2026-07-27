"""The lance-ray producer's ``POST /produce`` route — thin wrapper over the produce service."""

from __future__ import annotations

from typing import Annotated

from common.warehouse_registry import UnresolvableProjectError
from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse

from medallion.api.dependencies import DaprClientDep, SettingsDep
from medallion.api.produce_auth import ProjectParam, authorize_produce
from medallion.services.produce import produce as run_produce


router = APIRouter(tags=["produce"])


@router.get("/authorize")
async def authorize(_: Annotated[None, Depends(authorize_produce)]) -> dict[str, bool]:
    """Admin-door probe (#77): 200 iff the caller passes the SAME ``authorize_produce`` gate as ``/produce``
    (dev-open · service app-token · or a signed-in ``can_administer`` project admin) — else 401/403/503.

    Side-effect-free, so a governed admin surface (the web audit-log viewer) can reuse the one admin door the
    estate already owns without re-implementing the FGA check or gaining direct OpenFGA access: the web BFF
    bearer-forwards the signed-in user's token here and only proceeds if this returns 200. The admin concept
    lives here (``produce_admin_project``), so this is its natural home. Accepts the same optional
    ``project`` query param as ``/produce`` (#84), so the BFF can probe admin-ship per tenant."""
    return {"authorized": True}


@router.post("/produce", status_code=202, response_model=None)  # union with JSONResponse → no auto model
async def produce(
    dapr: DaprClientDep,
    settings: SettingsDep,
    _: Annotated[None, Depends(authorize_produce)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")] = None,
    project: ProjectParam = None,
) -> dict[str, str] | JSONResponse:
    """Ingest (dummy) the raw dataset and emit its write event — the event-driven cascade head.

    Seeds ``raw_events`` (with compute) and emits ONE OpenLineage event for it; lance-ray's ``/raw-arrival``
    subscription reacts to that event and publishes the ``medallion.raw`` trigger, so the cascade is driven
    by the arrival event, not this call. The raw-write emit is therefore the **cascade head** — if it is
    dropped, the entire raw→bronze→silver→gold run silently never happens. So a publish failure surfaces as
    **503** (not the 202 that would hide it), letting the caller retry; the request is otherwise 202.

    Guarded by ``require_dapr_token`` (the shared app-api-token) so an in-cluster workload can't forge the
    cascade head: /produce is a direct operator trigger (not sidecar-delivered), and without this any pod that
    could reach ``lance-ray:8000`` could drive the pipeline / fabricate medallion provenance. No-op in dev
    (unset token); enforced once APP_API_TOKEN is set. A NetworkPolicy (chart) is the network-isolation layer.

    ``Idempotency-Key`` (optional) is the retry pairing this route's own 503+Retry-After contract demands:
    a retry that REUSES the key converges onto the same cascade token (deterministic run_ids → the graph
    MERGEs the duplicate head) instead of double-firing two unrelated raw→gold runs.

    ``project`` (optional, #84 per-tenant routing) seeds THAT project's warehouse
    (``<root>/medallion/raw``, resolved off the warehouse registry) and stamps the project into the head
    event so the whole cascade routes per-tenant; ``authorize_produce`` gates ``can_administer`` on the
    requested project. Unresolvable (routing disabled, or no active warehouse) → **409** (fail closed —
    never a silent fallback to the shared root). Absent → today's single-tenant behavior, unchanged.
    """
    try:
        result = await run_produce(dapr, settings, token=idempotency_key, project=project or "")
    except UnresolvableProjectError as exc:
        return JSONResponse(
            status_code=409,
            media_type="application/problem+json",
            content={
                "type": "https://lance.org/problems/conflict",
                "title": "Conflict",
                "status": 409,
                "detail": str(exc),
            },
        )
    if result.get("status") == "publish_failed":
        # RFC 9457 problem+json + Retry-After (parity with catalog/lineage errors), not a bare FastAPI 503.
        return JSONResponse(
            status_code=503,
            media_type="application/problem+json",
            headers={"Retry-After": "5"},
            content={
                "type": "https://lance.org/problems/serviceunavailable",
                "title": "ServiceUnavailable",
                "status": 503,
                "detail": "medallion trigger publish failed; retry",
            },
        )
    return result
