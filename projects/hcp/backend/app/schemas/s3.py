"""S3 request/response schemas."""

from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class CreateBucketRequest(BaseModel):
    """Request body for creating a new S3 bucket."""

    bucket: str = Field(description="Name for the new bucket")


class BucketInfo(BaseModel):
    """Information about a single S3 bucket."""

    name: str = Field(alias="Name", description="Bucket name")
    creation_date: Optional[datetime] = Field(
        None, alias="CreationDate", description="ISO 8601 creation timestamp"
    )

    model_config = {"populate_by_name": True}


class OwnerInfo(BaseModel):
    """S3 bucket or object owner."""

    display_name: Optional[str] = Field(
        None, alias="DisplayName", description="Owner display name"
    )
    id: Optional[str] = Field(None, alias="ID", description="Owner canonical ID")

    model_config = {"populate_by_name": True}


class ListBucketsResponse(BaseModel):
    """Response for listing all S3 buckets."""

    buckets: List[BucketInfo] = Field(default=[], description="List of bucket objects")
    owner: Optional[OwnerInfo] = Field(None, description="Bucket owner information")


class ObjectInfo(BaseModel):
    """Information about a single S3 object."""

    key: str = Field(alias="Key", description="Object key (path)")
    size: Optional[int] = Field(None, alias="Size", description="Object size in bytes")
    last_modified: Optional[datetime] = Field(
        None, alias="LastModified", description="Last modification timestamp"
    )
    etag: Optional[str] = Field(None, alias="ETag", description="Entity tag (hash)")
    storage_class: Optional[str] = Field(
        None, alias="StorageClass", description="S3 storage class"
    )
    owner: Optional[OwnerInfo] = Field(None, alias="Owner", description="Object owner")

    model_config = {"populate_by_name": True}


class ListObjectsResponse(BaseModel):
    """Response for listing objects in a bucket."""

    objects: List[ObjectInfo] = Field(default=[], description="List of objects")
    common_prefixes: List[str] = Field(
        default=[], description="Grouped prefixes when using delimiter"
    )
    is_truncated: bool = Field(
        False, description="Whether there are more results to fetch"
    )
    next_continuation_token: Optional[str] = Field(
        None, description="Token for the next page of results"
    )
    key_count: int = Field(0, description="Number of keys returned")


class CountObjectsResponse(BaseModel):
    """Response for counting objects at a prefix."""

    files: int = Field(description="Total number of object keys")
    folders: int = Field(description="Total number of common prefixes (folders)")


class StatsTaskResponse(BaseModel):
    """Response for a stats counting task."""

    task_id: str = Field(description="Task identifier for polling")
    status: str = Field(description="counting | ready | failed")
    files: int = Field(default=0, description="Current file count")
    folders: int = Field(default=0, description="Current folder count")


class UploadObjectResponse(BaseModel):
    """Response for uploading an object."""

    bucket: str = Field(description="Bucket name")
    key: str = Field(description="Object key")
    status: str = Field("uploaded", description="Operation result")


class HeadObjectResponse(BaseModel):
    """Object metadata returned by HEAD."""

    content_length: Optional[int] = Field(None, description="File size in bytes")
    content_type: Optional[str] = Field(None, description="MIME type")
    etag: Optional[str] = Field(None, description="Entity tag (hash)")
    last_modified: Optional[str] = Field(
        None, description="Last modification timestamp"
    )


class CopyObjectRequest(BaseModel):
    """Request body for copying an object."""

    source_bucket: str = Field(description="Source bucket name")
    source_key: str = Field(description="Source object key")


class DeleteObjectsRequest(BaseModel):
    """Request body for bulk deleting objects."""

    keys: List[str] = Field(description="List of object keys to delete")


class BulkDownloadRequest(BaseModel):
    """Request body for bulk downloading objects as ZIP."""

    keys: List[str] = Field(default=[], description="Object keys to include")
    prefix: Optional[str] = Field(
        None, description="Download all objects under this prefix"
    )


class ZipTaskResponse(BaseModel):
    """Response for a ZIP download task."""

    task_id: str
    status: str = Field(description="processing, ready, or failed")
    total: int = 0
    completed: int = 0
    failed: int = 0
    failed_keys: List[str] = Field(default=[])
    error: Optional[str] = None


class BulkPresignRequest(BaseModel):
    """Request body for generating bulk presigned URLs."""

    keys: List[str] = Field(..., min_length=1, description="Object keys")
    method: str = Field("get_object", description="get_object or put_object")
    expires_in: int = Field(
        3600, ge=1, le=604800, description="URL expiration in seconds (max 7 days)"
    )


class BulkPresignItem(BaseModel):
    """A single presigned URL entry."""

    key: str = Field(description="Object key")
    url: str = Field(description="Presigned URL")


class BulkPresignResponse(BaseModel):
    """Response containing bulk presigned URLs."""

    urls: List[BulkPresignItem] = Field(description="Presigned URL entries")
    expires_in: int = Field(description="URL expiration in seconds")


class BucketVersioningResponse(BaseModel):
    """Bucket versioning configuration."""

    status: Optional[str] = Field(
        None, description="Enabled, Suspended, or empty (never enabled)"
    )
    mfa_delete: Optional[str] = Field(None, description="MFA delete status")


class PutBucketVersioningRequest(BaseModel):
    status: str = Field(..., description="Enabled or Suspended")


# ── ACL Models ────────────────────────────────────────────────────────


class AclOwner(BaseModel):
    model_config = {"extra": "allow"}

    ID: Optional[str] = None
    DisplayName: Optional[str] = None


class AclGrant(BaseModel):
    model_config = {"extra": "allow"}

    Grantee: Optional[dict] = None
    Permission: Optional[str] = None


class AclPolicy(BaseModel):
    """S3 ACL policy for buckets and objects."""

    model_config = {"extra": "allow"}

    Owner: Optional[AclOwner] = None
    Grants: Optional[List[AclGrant]] = None


class AclResponse(BaseModel):
    """Response for GET bucket/object ACL."""

    owner: Optional[dict] = None
    grants: Optional[List[dict]] = None


# ── Mutation Response Models ─────────────────────────────────────────


# ── CORS Models ──────────────────────────────────────────────────────


class CorsRule(BaseModel):
    """A single CORS rule for a bucket."""

    model_config = {"extra": "allow"}

    AllowedHeaders: list[str] | None = None
    AllowedMethods: list[str] | None = None
    AllowedOrigins: list[str] | None = None
    ExposeHeaders: list[str] | None = None
    MaxAgeSeconds: int | None = None


class CorsConfiguration(BaseModel):
    """S3 bucket CORS configuration."""

    CORSRules: list[CorsRule] = Field(default=[])


class CorsResponse(BaseModel):
    """Response for GET bucket CORS."""

    cors_rules: list[dict] = Field(default=[])


# ── Multipart Upload Listing Models ──────────────────────────────────


class MultipartUploadInfo(BaseModel):
    """Information about an in-progress multipart upload."""

    key: str = Field(alias="Key")
    upload_id: str = Field(alias="UploadId")
    initiated: Optional[datetime] = Field(None, alias="Initiated")
    storage_class: Optional[str] = Field(None, alias="StorageClass")

    model_config = {"populate_by_name": True}


class ListMultipartUploadsResponse(BaseModel):
    """Response for listing in-progress multipart uploads."""

    bucket: str
    uploads: list[MultipartUploadInfo] = Field(default=[])
    is_truncated: bool = False


# ── Folder Creation ──────────────────────────────────────────────────


class CreateFolderRequest(BaseModel):
    """Request body for creating a folder."""

    folder_name: str = Field(
        description="Folder name (trailing slash added automatically)"
    )


class CreateFolderResponse(BaseModel):
    """Response for folder creation."""

    bucket: str
    key: str
    status: str = "created"


# ── Mutation Response Models ─────────────────────────────────────────


class BucketMutationResponse(BaseModel):
    """Response for bucket create/delete."""

    status: str
    bucket: Optional[str] = None


class VersioningMutationResponse(BaseModel):
    """Response for PUT bucket versioning."""

    status: str
    versioning: Optional[str] = None


class ObjectMutationResponse(BaseModel):
    """Response for object copy/delete."""

    status: str
    bucket: Optional[str] = None
    key: Optional[str] = None


class DeleteObjectsResponse(BaseModel):
    """Response for bulk delete."""

    status: str
    deleted: int
    errors: Optional[List[dict]] = None


# ── Presigned URL Models ─────────────────────────────────────────


class PresignedUrlRequest(BaseModel):
    """Request body for generating a presigned URL."""

    bucket: str
    key: str
    expires_in: int = Field(
        3600, ge=1, le=604800, description="Expiry in seconds (max 7 days)"
    )
    method: str = Field(
        "get_object", description="get_object for download, put_object for upload"
    )


class PresignedUrlResponse(BaseModel):
    """Response containing the generated presigned URL."""

    url: str
    bucket: str
    key: str
    expires_in: int
    method: str


# ── Object Versions ────────────────────────────────────────────────


class ObjectVersionInfo(BaseModel):
    key: str = Field(alias="Key")
    version_id: Optional[str] = Field(None, alias="VersionId")
    is_latest: Optional[bool] = Field(None, alias="IsLatest")
    last_modified: Optional[datetime] = Field(None, alias="LastModified")
    etag: Optional[str] = Field(None, alias="ETag")
    size: Optional[int] = Field(None, alias="Size")
    storage_class: Optional[str] = Field(None, alias="StorageClass")
    owner: Optional[OwnerInfo] = Field(None, alias="Owner")

    model_config = {"populate_by_name": True}


class DeleteMarkerInfo(BaseModel):
    key: str = Field(alias="Key")
    version_id: Optional[str] = Field(None, alias="VersionId")
    is_latest: Optional[bool] = Field(None, alias="IsLatest")
    last_modified: Optional[datetime] = Field(None, alias="LastModified")
    owner: Optional[OwnerInfo] = Field(None, alias="Owner")

    model_config = {"populate_by_name": True}


class ListObjectVersionsResponse(BaseModel):
    versions: List[ObjectVersionInfo] = Field(default=[])
    delete_markers: List[DeleteMarkerInfo] = Field(default=[])
    is_truncated: bool = False
    next_key_marker: Optional[str] = None
    next_version_id_marker: Optional[str] = None
    key_count: int = 0


# ── Multipart Upload ──────────────────────────────────────────────


class PresignedMultipartRequest(BaseModel):
    """Request body for presigned multipart upload."""

    upload_id: str | None = Field(
        None,
        description="Existing upload ID from initiate. If omitted, a new multipart upload is created.",
    )
    file_size: int = Field(..., gt=0, description="Total file size in bytes")
    part_size: int = Field(
        25 * 1024 * 1024,
        ge=5 * 1024 * 1024,
        le=5 * 1024 * 1024 * 1024,
        description="Part size in bytes (min 5 MB, max 5 GB, default 25 MB)",
    )
    expires_in: int = Field(
        3600, ge=60, le=43200, description="URL expiration in seconds (min 60, max 12h)"
    )


class PresignedPartUrl(BaseModel):
    """A single presigned URL for uploading one part."""

    part_number: int
    url: str


class PresignedMultipartResponse(BaseModel):
    """Response containing presigned URLs for multipart upload."""

    bucket: str
    key: str
    upload_id: str
    part_size: int
    total_parts: int
    urls: List[PresignedPartUrl]
    expires_in: int


class CreateMultipartUploadResponse(BaseModel):
    bucket: str
    key: str
    upload_id: str


class UploadPartResponse(BaseModel):
    part_number: int
    etag: str


class CompletePart(BaseModel):
    PartNumber: int
    ETag: str


class CompleteMultipartUploadRequest(BaseModel):
    upload_id: str
    parts: List[CompletePart] = Field(description="List of {PartNumber, ETag} parts")


class CompleteMultipartUploadResponse(BaseModel):
    bucket: str
    key: str
    etag: Optional[str] = None


class AbortMultipartUploadRequest(BaseModel):
    upload_id: str


class PartInfo(BaseModel):
    part_number: int = Field(alias="PartNumber")
    etag: Optional[str] = Field(None, alias="ETag")
    size: Optional[int] = Field(None, alias="Size")
    last_modified: Optional[datetime] = Field(None, alias="LastModified")

    model_config = {"populate_by_name": True}


class ListPartsResponse(BaseModel):
    bucket: str
    key: str
    upload_id: str
    parts: List[PartInfo] = Field(default=[])
    is_truncated: bool = False


# ── S3 Credentials Model ────────────────────────────────────────


class S3CredentialsResponse(BaseModel):
    """S3 credentials for the authenticated user."""

    access_key_id: str
    secret_access_key: str
    username: Optional[str] = None
    tenant: Optional[str] = None
    endpoint_url: Optional[str] = None
