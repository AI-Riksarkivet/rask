"""S3 bucket CRUD endpoints.

Endpoints:
    GET  /buckets              — list all buckets
    POST /buckets              — create a bucket
    HEAD /buckets/{bucket}     — check if bucket exists
    DELETE /buckets/{bucket}   — delete a bucket (force=true empties first)
    GET  /buckets/{bucket}/versioning — get versioning status
    PUT  /buckets/{bucket}/versioning — set versioning
    GET  /buckets/{bucket}/acl — get bucket ACL
    PUT  /buckets/{bucket}/acl — set bucket ACL
    GET  /buckets/{bucket}/cors — get bucket CORS
    PUT  /buckets/{bucket}/cors — set bucket CORS
    DELETE /buckets/{bucket}/cors — delete bucket CORS
    GET  /buckets/{bucket}/uploads — list in-progress multipart uploads
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception

from app.api.dependencies import get_mapi_service, get_s3_service, get_storage_settings
from app.api.errors import run_storage
from app.core.config import StorageSettings
from app.core.security import oauth2_scheme, verify_token_with_credentials
from app.schemas.s3 import (
    AclPolicy,
    AclResponse,
    BucketInfo,
    BucketMutationResponse,
    BucketVersioningResponse,
    CorsConfiguration,
    CorsResponse,
    CreateBucketRequest,
    ListBucketsResponse,
    ListMultipartUploadsResponse,
    MultipartUploadInfo,
    OwnerInfo,
    PutBucketVersioningRequest,
    VersioningMutationResponse,
)
from app.services.mapi_service import AuthenticatedMapiService
from app.services.storage import StorageProtocol

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/buckets", tags=["S3 Buckets"])


@router.get("", response_model=ListBucketsResponse)
async def list_buckets(s3: StorageProtocol = Depends(get_s3_service)):
    """List all S3 buckets accessible to the authenticated user."""
    result = await run_storage(s3.list_buckets(), "buckets")
    buckets = [BucketInfo.model_validate(b) for b in result.get("Buckets", [])]
    owner = OwnerInfo.model_validate(result["Owner"]) if "Owner" in result else None
    return ListBucketsResponse(buckets=buckets, owner=owner)


@router.post("", response_model=BucketMutationResponse, status_code=201)
async def create_bucket(
    body: CreateBucketRequest,
    s3: StorageProtocol = Depends(get_s3_service),
):
    """Create a new S3 bucket."""
    await run_storage(s3.create_bucket(body.bucket), f"bucket '{body.bucket}'")
    return {"status": "created", "bucket": body.bucket}


@router.head("/{bucket}")
async def head_bucket(bucket: str, s3: StorageProtocol = Depends(get_s3_service)):
    """Check whether a bucket exists. Returns 200 or 404."""
    await run_storage(s3.head_bucket(bucket), f"bucket '{bucket}'")
    return Response(status_code=200)


@router.delete("/{bucket}", response_model=BucketMutationResponse)
async def delete_bucket(
    bucket: str,
    force: bool | None = Query(None),
    s3: StorageProtocol = Depends(get_s3_service),
    hcp: AuthenticatedMapiService = Depends(get_mapi_service),
    token: Annotated[str, Depends(oauth2_scheme)] = "",
    storage_settings: StorageSettings = Depends(get_storage_settings),
):
    """Delete an S3 bucket.

    If ``force=true``, all objects (including versions and delete markers)
    are removed before deleting the bucket itself.  On HCP, when the S3
    delete fails with BucketNotEmpty (typically due to immutable deletion
    records), the endpoint reconfigures the namespace via MAPI to allow
    pruning, then retries.  On MinIO/generic, force-delete is just
    empty + delete (no MAPI involved).
    """
    if force:
        empty_errors = await _empty_bucket(s3, bucket)

        if storage_settings.storage_backend == "hcp":
            return await _force_delete_hcp(s3, hcp, bucket, token, empty_errors)

        # MinIO / generic: simple empty + delete
        await run_storage(s3.delete_bucket(bucket), f"bucket '{bucket}'")
        return {"status": "deleted", "bucket": bucket}

    await run_storage(s3.delete_bucket(bucket), f"bucket '{bucket}'")
    return {"status": "deleted", "bucket": bucket}


async def _force_delete_hcp(
    s3: StorageProtocol,
    hcp: AuthenticatedMapiService,
    bucket: str,
    token: str,
    object_errors: list[str] | None = None,
) -> dict:
    """HCP-specific force-delete with MAPI namespace reconfiguration."""
    creds = verify_token_with_credentials(token)
    try:
        await run_storage(s3.delete_bucket(bucket), f"bucket '{bucket}'")
        return {"status": "deleted", "bucket": bucket}
    except HTTPException as exc:
        if exc.status_code != 409 or not creds.tenant:
            raise

    # BucketNotEmpty — likely immutable deletion records.
    logger.info(
        "force-delete: S3 delete failed for '%s' (BucketNotEmpty), "
        "reconfiguring namespace for deletion record cleanup",
        bucket,
    )
    ns_path = f"/tenants/{creds.tenant}/namespaces/{bucket}"
    await _prepare_namespace_for_delete(hcp, ns_path)

    try:
        return await _retry_delete_bucket(s3, hcp, bucket, ns_path)
    except HTTPException:
        reason = f"bucket '{bucket}': Could not delete bucket."
        if object_errors:
            reason += " Some objects failed to delete: " + "; ".join(object_errors)
        else:
            reason += (
                " HCP is still cleaning up internal deletion records."
                " Please try again in a few minutes."
            )
        raise HTTPException(status_code=409, detail=reason)


def _is_conflict(exc: BaseException) -> bool:
    """Only retry on 409 Conflict (BucketNotEmpty)."""
    return isinstance(exc, HTTPException) and exc.status_code == 409


@retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(2),
    retry=retry_if_exception(_is_conflict),
    reraise=True,
)
async def _retry_delete_bucket(
    s3: StorageProtocol,
    hcp: AuthenticatedMapiService,
    bucket: str,
    ns_path: str,
) -> dict:
    """Try S3 delete, then MAPI namespace delete. Retries on 409."""
    try:
        await run_storage(s3.delete_bucket(bucket), f"bucket '{bucket}'")
        return {"status": "deleted", "bucket": bucket}
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
    try:
        await hcp.send("DELETE", ns_path, resource=f"bucket '{bucket}'")
        return {"status": "deleted", "bucket": bucket}
    except HTTPException:
        logger.info(
            "force-delete: '%s' — waiting for HCP to prune deletion records",
            bucket,
        )
        raise


async def _prepare_namespace_for_delete(
    hcp: AuthenticatedMapiService, ns_path: str
) -> None:
    """Reconfigure an HCP namespace so deletion records can be pruned.

    HCP keeps immutable "deletion records" when ``keepDeletionRecords``
    is enabled.  These prevent both S3 bucket delete and MAPI namespace
    delete.  To allow cleanup, we disable the record-keeping, enable
    immediate pruning, and disable search indexing.
    """
    try:
        await hcp.send(
            "POST",
            f"{ns_path}/versioningSettings",
            body={"keepDeletionRecords": False, "prune": True, "pruneDays": 0},
        )
    except HTTPException:
        logger.warning("force-delete: failed to update versioning settings")
    try:
        await hcp.send("POST", ns_path, body={"searchEnabled": False})
    except HTTPException:
        logger.warning("force-delete: failed to disable search")


async def _delete_all_versions(
    s3: StorageProtocol, bucket: str
) -> tuple[int, list[str]]:
    """Delete every object version and delete marker in *bucket*.

    Returns ``(deleted_count, errors)`` where *errors* holds the first
    few human-readable failure reasons (capped at 5).
    """
    deleted = 0
    errors: list[str] = []
    key_marker: str | None = None
    version_marker: str | None = None
    while True:
        result = await s3.list_object_versions(
            bucket, None, 1000, key_marker, version_marker
        )
        items: list[tuple[str, str | None]] = []
        for v in result.get("Versions", []):
            items.append((v["Key"], v.get("VersionId")))
        for dm in result.get("DeleteMarkers", []):
            items.append((dm["Key"], dm.get("VersionId")))

        for key, vid in items:
            try:
                await s3.delete_object(bucket, key, vid)
                deleted += 1
            except Exception as exc:
                logger.warning(
                    "force-delete: failed to remove %s/%s: %s", bucket, key, exc
                )
                if len(errors) < 5:
                    errors.append(f"{key}: {exc}")

        if not result.get("IsTruncated", False):
            break
        key_marker = result.get("NextKeyMarker")
        version_marker = result.get("NextVersionIdMarker")
    return deleted, errors


async def _empty_bucket(s3: StorageProtocol, bucket: str) -> list[str]:
    """Delete all objects, versions, and delete markers in *bucket*.

    Returns a list of per-object failure reasons (capped).
    """
    deleted = 0
    errors: list[str] = []

    # 1. Delete all object versions and delete markers
    count, errs = await _delete_all_versions(s3, bucket)
    deleted += count
    errors.extend(errs)

    # 2. Clean up any remaining non-versioned objects
    token: str | None = None
    while True:
        result = await s3.list_objects(bucket, None, 1000, token)
        keys = [o["Key"] for o in result.get("Contents", [])]
        if keys:
            bulk_result = await s3.delete_objects(bucket, keys)
            bulk_errors = bulk_result.get("Errors", [])
            deleted += len(keys) - len(bulk_errors)
            for err in bulk_errors:
                if len(errors) < 5:
                    key = err.get("Key", "?")
                    code = err.get("Code", "Unknown")
                    msg = err.get("Message", "")
                    errors.append(f"{key}: {code} {msg}".strip())
        if not result.get("IsTruncated", False):
            break
        token = result.get("NextContinuationToken")

    # 3. Deleting non-versioned objects may have created new delete markers
    #    on versioned buckets — clean those up too.
    count, errs = await _delete_all_versions(s3, bucket)
    deleted += count
    errors.extend(errs)

    if deleted > 0:
        logger.info(
            "force-delete: removed %d objects from bucket '%s'", deleted, bucket
        )

    return errors


# ── Bucket versioning ────────────────────────────────────────────────


@router.get("/{bucket}/versioning", response_model=BucketVersioningResponse)
async def get_bucket_versioning(
    bucket: str,
    s3: StorageProtocol = Depends(get_s3_service),
):
    """Get the versioning configuration for a bucket."""
    result = await run_storage(s3.get_bucket_versioning(bucket), f"bucket '{bucket}'")
    return BucketVersioningResponse(
        status=result.get("Status"),
        mfa_delete=result.get("MFADelete"),
    )


@router.put("/{bucket}/versioning", response_model=VersioningMutationResponse)
async def put_bucket_versioning(
    bucket: str,
    body: PutBucketVersioningRequest,
    s3: StorageProtocol = Depends(get_s3_service),
):
    """Enable or suspend versioning on a bucket."""
    await run_storage(
        s3.put_bucket_versioning(bucket, body.status), f"bucket '{bucket}'"
    )
    return {"status": "updated", "versioning": body.status}


# ── Bucket ACL ────────────────────────────────────────────────────────


@router.get("/{bucket}/acl", response_model=AclResponse)
async def get_bucket_acl(bucket: str, s3: StorageProtocol = Depends(get_s3_service)):
    """Get the access control list (ACL) for a bucket."""
    result = await run_storage(s3.get_bucket_acl(bucket), f"bucket '{bucket}'")
    return {
        "owner": result.get("Owner", {}),
        "grants": result.get("Grants", []),
    }


@router.put("/{bucket}/acl", response_model=BucketMutationResponse)
async def put_bucket_acl(
    bucket: str,
    body: AclPolicy,
    s3: StorageProtocol = Depends(get_s3_service),
):
    """Set the access control list (ACL) for a bucket."""
    await run_storage(
        s3.put_bucket_acl(bucket, body.model_dump(exclude_none=True)),
        f"bucket '{bucket}'",
    )
    return {"status": "updated"}


# ── Bucket CORS ──────────────────────────────────────────────────────


@router.get("/{bucket}/cors", response_model=CorsResponse)
async def get_bucket_cors(bucket: str, s3: StorageProtocol = Depends(get_s3_service)):
    """Get the CORS configuration for a bucket."""
    result = await run_storage(s3.get_bucket_cors(bucket), f"bucket '{bucket}'")
    return {"cors_rules": result.get("CORSRules", [])}


@router.put("/{bucket}/cors", response_model=BucketMutationResponse)
async def put_bucket_cors(
    bucket: str,
    body: CorsConfiguration,
    s3: StorageProtocol = Depends(get_s3_service),
):
    """Set the CORS configuration for a bucket."""
    await run_storage(
        s3.put_bucket_cors(bucket, body.model_dump(exclude_none=True)),
        f"bucket '{bucket}'",
    )
    return {"status": "updated"}


@router.delete("/{bucket}/cors", response_model=BucketMutationResponse)
async def delete_bucket_cors(
    bucket: str, s3: StorageProtocol = Depends(get_s3_service)
):
    """Delete the CORS configuration for a bucket."""
    await run_storage(s3.delete_bucket_cors(bucket), f"bucket '{bucket}'")
    return {"status": "deleted"}


# ── List Multipart Uploads ───────────────────────────────────────────


@router.get("/{bucket}/uploads", response_model=ListMultipartUploadsResponse)
async def list_multipart_uploads(
    bucket: str,
    prefix: str | None = Query(None),
    max_uploads: int = Query(1000, ge=1, le=1000),
    s3: StorageProtocol = Depends(get_s3_service),
):
    """List in-progress multipart uploads for a bucket."""
    result = await run_storage(
        s3.list_multipart_uploads(bucket, prefix, max_uploads), f"bucket '{bucket}'"
    )
    uploads = [MultipartUploadInfo.model_validate(u) for u in result.get("Uploads", [])]
    return ListMultipartUploadsResponse(
        bucket=bucket,
        uploads=uploads,
        is_truncated=result.get("IsTruncated", False),
    )
