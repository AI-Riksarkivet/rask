"""The medallion-producer producer's ``POST /ingest-media`` route — thin wrapper over the media ingest service."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse

from medallion.api.dependencies import DaprClientDep, SettingsDep
from medallion.api.produce_auth import authorize_ingest_media
from medallion.services.media_produce import ingest_media as run_ingest_media
from service_kit.draining import refuse_when_draining


router = APIRouter(tags=["media"])

_PROBLEM_JSON = "application/problem+json"


@router.post(
    "/ingest-media",
    status_code=202,
    response_model=None,
    # B6: a draining pod must not START work it cannot finish. 503 + Retry-After rather than a
    # 4xx — the caller's request is fine, this replica is simply leaving.
    dependencies=[Depends(refuse_when_draining)],
)
async def ingest_media(
    dapr: DaprClientDep,
    settings: SettingsDep,
    originator: Annotated[str | None, Depends(authorize_ingest_media)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")] = None,
) -> dict[str, str] | JSONResponse:
    """Land external media as bronze blobs and trigger the media chain — the multimodal cascade head (§9).

    Seeds/reads the configured external source prefix through the SourceAdapter seam, writes the bronze
    blob-v2 table, emits the ``source → bronze`` lineage (blob schema facet), and publishes the
    ``medallion.media`` trigger the bronze→silver media mover consumes (which then derives the inline
    thumbnail + embedding schema-driven). 409 when the media head isn't configured (real media can't be
    dummied — unlike ``/produce`` there is no compute-off emit); 503 when a publish fails (retryable —
    the ingest is an idempotent overwrite). Dual-auth like ``/produce``: without a door any in-cluster
    pod could drive the media pipeline / fabricate provenance — and the door is also where the
    requester's identity is captured, so the runs it starts can reach the person who asked for them.
    """
    try:
        result = await run_ingest_media(dapr, settings, token=idempotency_key, originator=originator)
    except ValueError as exc:
        # Client-addressable ingest refusals (empty source prefix, ingest ceilings exceeded) — a clear
        # 400 with the actionable message, never an opaque 500.
        return JSONResponse(
            status_code=400,
            media_type=_PROBLEM_JSON,
            content={
                "type": "https://lance.org/problems/invalidinput",
                "title": "InvalidInput",
                "status": 400,
                "detail": str(exc),
            },
        )
    if result.get("status") == "media_disabled":
        return JSONResponse(
            status_code=409,
            media_type=_PROBLEM_JSON,
            content={
                "type": "https://lance.org/problems/conflict",
                "title": "Conflict",
                "status": 409,
                "detail": "media ingest head is not configured (requires MEDALLION_COMPUTE_ENABLED, "
                "MEDALLION_MEDIA_BRONZE_URI and MEDALLION_MEDIA_SOURCE_BUCKET)",
            },
        )
    if result.get("status") == "publish_failed":
        return JSONResponse(
            status_code=503,
            media_type=_PROBLEM_JSON,
            headers={"Retry-After": "5"},
            content={
                "type": "https://lance.org/problems/serviceunavailable",
                "title": "ServiceUnavailable",
                "status": 503,
                "detail": "media ingest publish failed; retry",
            },
        )
    return result
