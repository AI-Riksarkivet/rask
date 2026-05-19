"""S3 multipart upload endpoints.

Route ordering: specific suffix routes (/presign, /complete, /abort, /parts)
must come before the {key:path} catch-all routes to avoid being swallowed.
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

from app.api.dependencies import get_s3_service
from app.api.errors import run_storage
from app.schemas.common import StatusResponse
from app.schemas.s3 import (
    AbortMultipartUploadRequest,
    CompleteMultipartUploadRequest,
    CompleteMultipartUploadResponse,
    CreateMultipartUploadResponse,
    ListPartsResponse,
    PartInfo,
    PresignedMultipartRequest,
    PresignedMultipartResponse,
    PresignedPartUrl,
    UploadPartResponse,
)
from app.services.storage import StorageProtocol

router = APIRouter(
    prefix="/buckets/{bucket}/multipart",
    tags=["S3 Multipart Upload"],
)


# ── Specific suffix routes (must come before {key:path} catch-all) ───


@router.post("/{key:path}/complete", response_model=CompleteMultipartUploadResponse)
async def complete_multipart_upload(
    bucket: str,
    key: str,
    body: CompleteMultipartUploadRequest,
    s3: StorageProtocol = Depends(get_s3_service),
):
    """Complete a multipart upload by assembling uploaded parts."""
    result = await run_storage(
        s3.complete_multipart_upload(
            bucket, key, body.upload_id, [p.model_dump() for p in body.parts]
        ),
        f"object '{key}'",
    )
    return CompleteMultipartUploadResponse(
        bucket=bucket,
        key=key,
        etag=result.get("ETag"),
    )


@router.post("/{key:path}/abort", response_model=StatusResponse)
async def abort_multipart_upload(
    bucket: str,
    key: str,
    body: AbortMultipartUploadRequest,
    s3: StorageProtocol = Depends(get_s3_service),
):
    """Abort a multipart upload and discard uploaded parts."""
    await run_storage(
        s3.abort_multipart_upload(bucket, key, body.upload_id),
        f"object '{key}'",
    )
    return StatusResponse(status="aborted")


@router.get("/{key:path}/parts", response_model=ListPartsResponse)
async def list_parts(
    bucket: str,
    key: str,
    upload_id: str = Query(...),
    max_parts: int = Query(1000, le=1000),
    s3: StorageProtocol = Depends(get_s3_service),
):
    """List uploaded parts for a multipart upload."""
    result = await run_storage(
        s3.list_parts(bucket, key, upload_id, max_parts),
        f"object '{key}'",
    )
    parts = [PartInfo.model_validate(p) for p in result.get("Parts", [])]
    return ListPartsResponse(
        bucket=bucket,
        key=key,
        upload_id=upload_id,
        parts=parts,
        is_truncated=result.get("IsTruncated", False),
    )


@router.post("/{key:path}/presign", response_model=PresignedMultipartResponse)
async def presign_multipart_upload(
    bucket: str,
    key: str,
    body: PresignedMultipartRequest,
    s3: StorageProtocol = Depends(get_s3_service),
):
    """Generate presigned URLs for direct-to-storage multipart upload.

    The browser uploads parts directly to HCP/S3 using the presigned URLs,
    bypassing the backend for data transfer. The backend only handles
    metadata operations (create, presign, complete, abort).

    **CORS requirement:** The target HCP namespace must have CORS configured:
    - ``PUT`` must be in ``AllowedMethods``
    - ``ETag`` must be in ``ExposeHeaders``
    """
    total_parts = math.ceil(body.file_size / body.part_size)
    if total_parts > 10_000:
        raise HTTPException(
            status_code=400,
            detail=f"Too many parts ({total_parts}). Increase part_size or reduce file_size. S3 maximum is 10,000 parts.",
        )

    if body.upload_id:
        # Reuse existing upload from a prior initiate call
        upload_id = body.upload_id
    else:
        # Create a new multipart upload (one-step flow)
        result = await run_storage(
            s3.create_multipart_upload(bucket, key), f"object '{key}'"
        )
        upload_id = result["UploadId"]

    urls: list[PresignedPartUrl] = []
    for i in range(1, total_parts + 1):
        url = await run_storage(
            s3.generate_presigned_url(
                bucket,
                key,
                body.expires_in,
                "upload_part",
                {"UploadId": upload_id, "PartNumber": i},
            ),
            f"presign part {i}",
        )
        urls.append(PresignedPartUrl(part_number=i, url=url))

    return PresignedMultipartResponse(
        bucket=bucket,
        key=key,
        upload_id=upload_id,
        part_size=body.part_size,
        total_parts=total_parts,
        urls=urls,
        expires_in=body.expires_in,
    )


# ── Catch-all routes ({key:path} matches everything) ────────────────


@router.post(
    "/{key:path}", response_model=CreateMultipartUploadResponse, status_code=201
)
async def create_multipart_upload(
    bucket: str,
    key: str,
    s3: StorageProtocol = Depends(get_s3_service),
):
    """Initiate a multipart upload and return an upload ID."""
    result = await run_storage(
        s3.create_multipart_upload(bucket, key), f"object '{key}'"
    )
    return CreateMultipartUploadResponse(
        bucket=bucket,
        key=key,
        upload_id=result["UploadId"],
    )


@router.put("/{key:path}", response_model=UploadPartResponse)
async def upload_part(
    bucket: str,
    key: str,
    upload_id: str = Query(...),
    part_number: int = Query(..., ge=1, le=10000),
    file: UploadFile = File(...),
    s3: StorageProtocol = Depends(get_s3_service),
):
    """Upload a single part of a multipart upload."""
    result = await run_storage(
        s3.upload_part(bucket, key, upload_id, part_number, file.file),
        f"object '{key}' part {part_number}",
    )
    return UploadPartResponse(
        part_number=part_number,
        etag=result["ETag"],
    )
