"""Backend-agnostic storage protocol for S3-compatible services."""

from __future__ import annotations

from typing import IO, Protocol, runtime_checkable


@runtime_checkable
class StorageProtocol(Protocol):
    """Interface that every storage backend must implement.

    All methods are async — callers ``await`` them directly.
    Methods return plain dicts matching the boto3 response shapes so existing
    endpoint code works without changes.

    Response shape reference (most common keys)::

        list_buckets()      → {"Buckets": [{"Name": str, "CreationDate": datetime}],
                               "Owner": {"DisplayName": str, "ID": str}}
        head_bucket()       → {"ResponseMetadata": {...}}
        list_objects()      → {"Contents": [{"Key": str, "Size": int, ...}],
                               "IsTruncated": bool,
                               "NextContinuationToken": str | None}
        get_object()        → {"Body": StreamingBody, "ContentLength": int,
                               "ContentType": str, "ETag": str, ...}
        head_object()       → {"ContentLength": int, "ContentType": str,
                               "ETag": str, "LastModified": datetime, ...}
        delete_objects()    → {"Errors": [{"Key": str, "Code": str}]} or {}
    """

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    # ── Bucket operations ──────────────────────────────────────────────

    async def list_buckets(self) -> dict: ...

    async def create_bucket(self, name: str) -> dict: ...

    async def head_bucket(self, name: str) -> dict: ...

    async def delete_bucket(self, name: str) -> dict: ...

    # ── Object operations ──────────────────────────────────────────────

    async def list_objects(
        self,
        bucket: str,
        prefix: str | None = None,
        max_keys: int = 1000,
        continuation_token: str | None = None,
        delimiter: str | None = None,
        fetch_owner: bool = True,
    ) -> dict: ...

    async def put_object(self, bucket: str, key: str, body: IO[bytes]) -> None: ...

    async def get_object(
        self, bucket: str, key: str, version_id: str | None = None
    ) -> dict: ...

    async def head_object(self, bucket: str, key: str) -> dict: ...

    async def delete_object(
        self, bucket: str, key: str, version_id: str | None = None
    ) -> dict: ...

    async def delete_objects(self, bucket: str, keys: list[str]) -> dict: ...

    async def copy_object(
        self,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
    ) -> dict: ...

    # ── Bucket versioning ─────────────────────────────────────────────

    async def get_bucket_versioning(self, bucket: str) -> dict: ...

    async def put_bucket_versioning(self, bucket: str, status: str) -> dict: ...

    # ── ACLs ──────────────────────────────────────────────────────────

    async def get_bucket_acl(self, bucket: str) -> dict: ...

    async def put_bucket_acl(self, bucket: str, acl: dict) -> dict: ...

    async def get_object_acl(self, bucket: str, key: str) -> dict: ...

    async def put_object_acl(self, bucket: str, key: str, acl: dict) -> dict: ...

    # ── Bucket CORS ─────────────────────────────────────────────────

    async def get_bucket_cors(self, bucket: str) -> dict: ...

    async def put_bucket_cors(self, bucket: str, cors_configuration: dict) -> dict: ...

    async def delete_bucket_cors(self, bucket: str) -> dict: ...

    # ── Object versions ────────────────────────────────────────────

    async def list_object_versions(
        self,
        bucket: str,
        prefix: str | None = None,
        max_keys: int = 1000,
        key_marker: str | None = None,
        version_id_marker: str | None = None,
    ) -> dict: ...

    # ── Presigned URLs ───────────────────────────────────────────────

    async def generate_presigned_url(
        self,
        bucket: str,
        key: str,
        expires_in: int = 3600,
        method: str = "get_object",
        extra_params: dict[str, str | int] | None = None,
    ) -> str: ...

    # ── Multipart uploads ────────────────────────────────────────────

    async def create_multipart_upload(self, bucket: str, key: str) -> dict: ...

    async def upload_part(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        part_number: int,
        body: IO[bytes],
    ) -> dict: ...

    async def complete_multipart_upload(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        parts: list[dict],
    ) -> dict: ...

    async def abort_multipart_upload(
        self, bucket: str, key: str, upload_id: str
    ) -> dict: ...

    async def list_parts(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        max_parts: int = 1000,
    ) -> dict: ...

    async def list_multipart_uploads(
        self,
        bucket: str,
        prefix: str | None = None,
        max_uploads: int = 1000,
    ) -> dict: ...
