"""S3 object version listing endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_s3_service
from app.api.errors import run_storage
from app.schemas.s3 import (
    DeleteMarkerInfo,
    ListObjectVersionsResponse,
    ObjectVersionInfo,
)
from app.services.storage import StorageProtocol

router = APIRouter(prefix="/buckets/{bucket}/versions", tags=["S3 Versions"])


@router.get("", response_model=ListObjectVersionsResponse)
async def list_object_versions(
    bucket: str,
    prefix: str | None = Query(None),
    max_keys: int = Query(1000, le=1000),
    key_marker: str | None = Query(None),
    version_id_marker: str | None = Query(None),
    s3: StorageProtocol = Depends(get_s3_service),
):
    """List all versions of objects in a bucket."""
    result = await run_storage(
        s3.list_object_versions(
            bucket, prefix, max_keys, key_marker, version_id_marker
        ),
        f"bucket '{bucket}'",
    )
    versions = [ObjectVersionInfo.model_validate(v) for v in result.get("Versions", [])]
    delete_markers = [
        DeleteMarkerInfo.model_validate(d) for d in result.get("DeleteMarkers", [])
    ]
    return ListObjectVersionsResponse(
        versions=versions,
        delete_markers=delete_markers,
        is_truncated=result.get("IsTruncated", False),
        next_key_marker=result.get("NextKeyMarker"),
        next_version_id_marker=result.get("NextVersionIdMarker"),
        key_count=len(versions) + len(delete_markers),
    )
