"""The medallion-producer producer's ``POST /produce`` route — thin wrapper over the produce service."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from lance_namespace import ErrorCode

from medallion.api.dependencies import DaprClientDep, SettingsDep
from medallion.api.produce_auth import ProjectParam, authorize_produce
from medallion.services.produce import produce as run_produce
from service_kit.draining import refuse_when_draining
from service_kit.lakehouse.ns_errors import problem_body
from service_kit.lakehouse.warehouse_registry import UnresolvableProjectError


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


@router.post(
    "/produce",
    status_code=202,
    response_model=None,
    # B6: a draining pod must not START work it cannot finish. 503 + Retry-After rather than a
    # 4xx — the caller's request is fine, this replica is simply leaving.
    dependencies=[Depends(refuse_when_draining)],
)
async def produce(
    dapr: DaprClientDep,
    settings: SettingsDep,
    originator: Annotated[str | None, Depends(authorize_produce)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")],
    project: ProjectParam = None,
    # The bronze volume this call writes. Optional, and absent means the seeder's own default — the
    # cascade is byte-identical to before unless a caller asks for something else.
    #
    # It exists because the promotion review band compares a stage's row count against its
    # predecessor's, and a producer that always writes the same rows makes that delta permanently
    # ZERO: no legal band can breach it (`ge=0` forbids a negative one), so the band was enabled,
    # correct, and unfalsifiable. §9.1's open question — the 0.25 default is ASSUMED, and wants "a
    # measurement of real promotions" — needs the same thing.
    #
    # Bounded because this writes every row synchronously before the request returns; an unbounded
    # value is a request that never comes back rather than an error a caller can read.
    rows: Annotated[int | None, Query(ge=1, le=1_000_000)] = None,
) -> dict[str, str] | JSONResponse:
    """Ingest (dummy) the bronze dataset and emit its write event — the event-driven cascade head.

    Seeds ``bronze$events`` (with compute) and emits ONE OpenLineage event for it; medallion-producer's
    ``/bronze-arrival`` subscription reacts to that event and publishes the ``medallion.bronze`` trigger,
    so the cascade is driven by the arrival event, not this call. The bronze-write emit is therefore the
    **cascade head** — if it is dropped, the entire bronze→silver→gold run silently never happens. So a
    publish failure surfaces as **503** (not the 202 that would hide it), letting the caller retry; the
    request is otherwise 202.

    Before it seeds anything it REGISTERS ``bronze$events`` with the catalog, so the head's own tier is a
    governed ``table:`` object like every tier below it (retention/legal-hold policy, protection, FGA
    grants all key off that object). A catalog that refuses or cannot be reached is also a **503**: the
    refusal happens before the first byte, so the run did not half-happen and the same retry converges.

    Guarded by ``require_dapr_token`` (the shared app-api-token) so an in-cluster workload can't forge the
    cascade head: /produce is a direct operator trigger (not sidecar-delivered), and without this any pod that
    could reach ``medallion-producer:8000`` could drive the pipeline / fabricate medallion provenance. No-op in dev
    (unset token); enforced once APP_API_TOKEN is set. A NetworkPolicy (chart) is the network-isolation layer.

    ``Idempotency-Key`` (REQUIRED — the header param above carries no default, so a caller omitting it
    is refused 422 before any auth or cascade work) is the retry pairing this route's own 503+Retry-After contract demands:
    a retry that REUSES the key converges onto the same cascade token (deterministic run_ids → the graph
    MERGEs the duplicate head) instead of double-firing two unrelated bronze→gold runs.

    ``project`` (optional, #84 per-tenant routing) seeds THAT project's warehouse
    (``<root>/medallion/bronze``, resolved off the warehouse registry) and stamps the project into the head
    event so the whole cascade routes per-tenant; ``authorize_produce`` gates ``can_administer`` on the
    requested project. Unresolvable (routing disabled, or no active warehouse) → **409** (fail closed —
    never a silent fallback to the shared root). Absent → today's single-tenant behavior, unchanged.
    """
    try:
        result = await run_produce(dapr, settings, token=idempotency_key, project=project or "", originator=originator or "", rows=rows)
    except UnresolvableProjectError as exc:
        return JSONResponse(
            status_code=409,
            media_type="application/problem+json",
            content=problem_body(ErrorCode.INVALID_TABLE_STATE, status=409, title="Conflict", detail=str(exc)),
        )
    if result.get("status") in ("publish_failed", "register_failed"):
        # RFC 9457 problem+json + Retry-After (parity with catalog/lineage errors), not a bare FastAPI 503.
        return JSONResponse(
            status_code=503,
            media_type="application/problem+json",
            headers={"Retry-After": "5"},
            # The `code`/`error` pair is what makes the parity this comment claims actually true — the
            # body carried four keys where every catalog/lineage error carries six.
            content=problem_body(
                ErrorCode.SERVICE_UNAVAILABLE,
                status=503,
                title="ServiceUnavailable",
                # TWO WAYS TO REACH THIS, one contract. `register_failed` is the catalog refusing or
                # being unreachable BEFORE any byte is written — so, exactly like a failed publish,
                # nothing happened and the caller's retry (same Idempotency-Key) converges. The detail
                # names which, because "retry" is the same advice but the thing to look at is not.
                detail=f"medallion {'catalog registration' if result.get('status') == 'register_failed' else 'trigger publish'} failed; retry",
            ),
        )
    return result
