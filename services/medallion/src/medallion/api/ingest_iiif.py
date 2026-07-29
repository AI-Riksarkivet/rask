"""The lance-ray producer's ``POST /ingest-iiif`` route — thin wrapper over the IIIF ingest service (P7a)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from medallion.api.dependencies import DaprClientDep, SettingsDep
from medallion.services.iiif_produce import ingest_iiif as run_ingest_iiif
from medallion.services.ray_submit import RayJobError
from service_kit.governed.dapr_auth import require_dapr_token
from service_kit.lakehouse.warehouse_registry import UnresolvableProjectError


router = APIRouter(tags=["iiif"])

_PROBLEM_JSON = "application/problem+json"


class IIIFIngestRequest(BaseModel):
    """One volume to harvest: the IIIF manifest/volume id (an ``arkis!`` batch id), optionally bounded."""

    volume_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    project: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    max_pages: int | None = Field(default=None, ge=1)


def _problem(status_code: int, title: str, detail: str, *, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        media_type=_PROBLEM_JSON,
        headers=headers,
        content={
            "type": f"https://lance.org/problems/{title.lower()}",
            "title": title,
            "status": status_code,
            "detail": detail,
        },
    )


@router.post("/ingest-iiif", status_code=202, response_model=None)  # union with JSONResponse → no model
async def ingest_iiif(
    body: IIIFIngestRequest,
    dapr: DaprClientDep,
    settings: SettingsDep,
    _: Annotated[None, Depends(require_dapr_token)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")] = None,
) -> dict[str, str] | JSONResponse:
    """Harvest a IIIF volume into the BRONZE page-image dataset — the P7a cascade head for HTR.

    Fetches the volume's manifest + page images from the IIIF Image API (external raw, R23), lands them
    as the bronze blob-v2 page dataset (in-process, or the Ray harvest job with ``MEDALLION_RAY_ENABLED``),
    and emits the ONE bronze-write lineage event (input = the external ``iiif://…`` source);
    ``/bronze-arrival`` reacts to that event and fires the ``medallion.bronze`` cascade (the HTR movers).
    409 when the head isn't configured or the
    project is unresolvable; 400 on ingest refusals (empty manifest, ceilings); 503 when the emit or the
    Ray job fails (retryable — the harvest is an idempotent overwrite and ``Idempotency-Key`` converges
    the retry onto the same run). Token-guarded like ``/produce``/``/ingest-media``.
    """
    try:
        result = await run_ingest_iiif(
            dapr,
            settings,
            body.volume_id,
            token=idempotency_key,
            max_pages=body.max_pages,
            project=body.project or "",
        )
    except UnresolvableProjectError as exc:
        return _problem(409, "Conflict", str(exc))
    except ValueError as exc:
        # Client-addressable refusals (empty manifest, ingest ceilings exceeded) — a clear 400.
        return _problem(400, "InvalidInput", str(exc))
    except RayJobError as exc:
        return _problem(503, "ServiceUnavailable", f"iiif harvest job failed; retry: {exc}", headers={"Retry-After": "5"})
    if result.get("status") == "iiif_disabled":
        return _problem(
            409,
            "Conflict",
            "iiif ingest head is not configured (requires MEDALLION_COMPUTE_ENABLED and MEDALLION_IIIF_BRONZE_URI)",
        )
    if result.get("status") == "publish_failed":
        return _problem(503, "ServiceUnavailable", "iiif ingest publish failed; retry", headers={"Retry-After": "5"})
    return result
