"""IIIF manifest and image URL endpoints.

Provides cached access to Riksarkivet IIIF manifests, replacing direct
HTTP calls from the CLI/SDK with a backend service that caches manifests
and reuses connection pools.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import get_iiif_service
from app.schemas.iiif import (
    IiifBatchParams,
    IiifImageUrl,
    IiifImageUrlsResponse,
    IiifManifestResponse,
)
from app.services.cached_iiif import CachedIiifService
from app.services.iiif_service import IiifError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/iiif", tags=["IIIF"])


def _handle_iiif_error(exc: Exception, context: str) -> HTTPException:
    """Map IiifError to appropriate HTTP status codes."""
    if isinstance(exc, IiifError):
        logger.warning("IIIF error (%s): %s", context, exc)
        return HTTPException(status_code=502, detail=str(exc))
    logger.error("Unexpected error (%s): %s", context, exc)
    return HTTPException(status_code=500, detail=f"Internal error: {context}")


@router.get("/manifest/{batch_id}", response_model=IiifManifestResponse)
async def get_manifest(
    batch_id: str,
    iiif: CachedIiifService = Depends(get_iiif_service),
):
    """Fetch and cache a IIIF manifest, returning image IDs."""
    try:
        image_ids = await iiif.get_image_ids(batch_id)
    except Exception as exc:
        raise _handle_iiif_error(exc, f"manifest for {batch_id}")
    return IiifManifestResponse(
        batch_id=batch_id,
        image_count=len(image_ids),
        image_ids=image_ids,
    )


@router.get("/images/{batch_id}", response_model=IiifImageUrlsResponse)
async def get_image_urls(
    batch_id: str,
    params: IiifBatchParams = Depends(),
    iiif: CachedIiifService = Depends(get_iiif_service),
):
    """Resolve image URLs for a batch (manifest + URL building)."""
    try:
        image_ids = await iiif.get_image_ids(batch_id)
    except Exception as exc:
        raise _handle_iiif_error(exc, f"images for {batch_id}")

    query_params = params.query_params
    urls = iiif.build_image_urls(image_ids, query_params)
    extension = iiif.file_extension(query_params)

    return IiifImageUrlsResponse(
        batch_id=batch_id,
        query_params=query_params,
        extension=extension,
        image_count=len(image_ids),
        images=[IiifImageUrl(image_id=img_id, url=url) for img_id, url in urls.items()],
    )
