"""In-memory S3 service for the mock development server."""

from __future__ import annotations

import hashlib
import io
import logging
import mimetypes
from datetime import datetime, timezone
from typing import IO, Any, Dict, List, Optional
from urllib.parse import quote, urlencode

from app.services.storage.errors import StorageError


class _StoredObject:
    """Metadata + bytes for a single stored object."""

    __slots__ = ("data", "content_type", "etag", "last_modified")

    def __init__(self, data: bytes, content_type: str = "application/octet-stream"):
        self.data = data
        self.content_type = content_type
        self.etag = f'"{hashlib.md5(data).hexdigest()}"'
        self.last_modified = datetime.now(timezone.utc).isoformat()


class MockS3Service:
    """In-memory S3 service implementing the StorageProtocol interface.

    All public methods are async and raise StorageError (not ClientError)
    so they work correctly with ``run_storage()`` in the API layer.
    """

    def __init__(self) -> None:
        self._logger = logging.getLogger("mock_server.s3")
        # bucket_name -> metadata dict
        self._buckets: Dict[str, Dict[str, Any]] = {}
        # bucket_name -> {key -> _StoredObject}
        self._objects: Dict[str, Dict[str, _StoredObject]] = {}
        # bucket_name -> versioning status str
        self._versioning: Dict[str, str] = {}
        # bucket_name -> ACL dict
        self._bucket_acls: Dict[str, Dict[str, Any]] = {}
        # (bucket, key) -> ACL dict
        self._object_acls: Dict[tuple, Dict[str, Any]] = {}
        # bucket_name -> CORS configuration dict
        self._bucket_cors: Dict[str, Dict[str, Any]] = {}
        # Multipart uploads: upload_id -> {bucket, key, parts: {part_num: bytes}}
        self._multipart_uploads: Dict[str, Dict[str, Any]] = {}
        self._upload_counter: int = 0
        # Cross-reference to MAPI state for namespace ↔ bucket sync
        self._mapi_state: Any = None
        # Default tenant for namespace sync (set during init)
        self._default_tenant: Optional[str] = None

    def _require_bucket(self, name: str) -> None:
        if name not in self._buckets:
            raise StorageError("NoSuchBucket", f"Bucket '{name}' does not exist", 404)

    def _require_object(self, bucket: str, key: str) -> _StoredObject:
        self._require_bucket(bucket)
        obj = self._objects.get(bucket, {}).get(key)
        if obj is None:
            raise StorageError("NoSuchKey", f"Object '{key}' not found", 404)
        return obj

    # ── Namespace ↔ bucket sync helpers ─────────────────────────────────

    def _sync_create_namespace(self, name: str) -> None:
        """Create a MAPI namespace when a bucket is created via S3."""
        state = self._mapi_state
        if state is None:
            return
        tenant = self._default_tenant
        if tenant is None or tenant not in state.namespaces:
            return
        ns_map = state.namespaces[tenant]
        if name not in ns_map:
            from .fixtures import default_ns_settings

            ns_map[name] = {"name": name}
            state.retention_classes.setdefault((tenant, name), {})
            state.ns_settings.setdefault((tenant, name), default_ns_settings())
            # Per HCP S3 docs: bucket creator gets full data access
            state._grant_user_ns_access(tenant, "admin", name)

    def _sync_delete_namespace(self, name: str) -> None:
        """Remove the MAPI namespace when a bucket is deleted via S3."""
        state = self._mapi_state
        if state is None:
            return
        tenant = self._default_tenant
        if tenant is None:
            return
        ns_map = state.namespaces.get(tenant)
        if ns_map and name in ns_map:
            del ns_map[name]
            state.retention_classes.pop((tenant, name), None)
            state.ns_settings.pop((tenant, name), None)

    # ── Bucket operations ─────────────────────────────────────────────

    def _accessible_buckets(self) -> set[str]:
        """Return bucket names the mock user has data access permissions for."""
        state = self._mapi_state
        tenant = self._default_tenant
        if state is None or tenant is None:
            return set(self._buckets)
        # Mock server always authenticates as "admin"
        key = (tenant, "user", "admin")
        perms = state.data_access_perms.get(key, {})
        return {
            p["namespaceName"]
            for p in perms.get("namespacePermission", [])
            if p.get("permissions", {}).get("permission")
        }

    async def list_buckets(self) -> dict:
        self._logger.info("list_buckets")
        allowed = self._accessible_buckets()
        buckets = [
            {"Name": name, "CreationDate": meta.get("CreationDate", "")}
            for name, meta in self._buckets.items()
            if name in allowed
        ]
        return {"Buckets": buckets}

    async def create_bucket(self, name: str) -> dict:
        self._logger.info("create_bucket name=%s", name)
        if name in self._buckets:
            raise StorageError("BucketAlreadyOwnedByYou", "Bucket already exists", 409)
        self._buckets[name] = {"CreationDate": datetime.now(timezone.utc).isoformat()}
        self._objects[name] = {}
        # Sync: also create as MAPI namespace
        self._sync_create_namespace(name)
        return {}

    async def head_bucket(self, name: str) -> dict:
        self._logger.info("head_bucket name=%s", name)
        self._require_bucket(name)
        return {}

    async def delete_bucket(self, name: str) -> dict:
        self._logger.info("delete_bucket name=%s", name)
        self._require_bucket(name)
        if self._objects.get(name):
            raise StorageError("BucketNotEmpty", "Bucket is not empty", 409)
        del self._buckets[name]
        self._objects.pop(name, None)
        self._versioning.pop(name, None)
        self._bucket_acls.pop(name, None)
        self._bucket_cors.pop(name, None)
        # Sync: also remove MAPI namespace
        self._sync_delete_namespace(name)
        return {}

    # ── Object operations ─────────────────────────────────────────────

    async def list_objects(
        self,
        bucket: str,
        prefix: Optional[str] = None,
        max_keys: int = 1000,
        continuation_token: Optional[str] = None,
        delimiter: Optional[str] = None,
        fetch_owner: bool = True,
    ) -> dict:
        self._logger.info("list_objects bucket=%s prefix=%s", bucket, prefix)
        self._require_bucket(bucket)
        all_keys = sorted(self._objects.get(bucket, {}).keys())
        if prefix:
            all_keys = [k for k in all_keys if k.startswith(prefix)]

        # Delimiter grouping: split keys into direct objects vs common prefixes
        common_prefixes: List[str] = []
        if delimiter:
            grouped_keys: List[str] = []
            seen_prefixes: set[str] = set()
            for key in all_keys:
                rest = key[len(prefix or "") :]
                idx = rest.find(delimiter)
                if idx >= 0:
                    # Key is nested — extract common prefix
                    cp = (prefix or "") + rest[: idx + len(delimiter)]
                    if cp not in seen_prefixes:
                        seen_prefixes.add(cp)
                        common_prefixes.append(cp)
                else:
                    grouped_keys.append(key)
            all_keys = grouped_keys

        start = 0
        if continuation_token:
            try:
                start = int(continuation_token)
            except ValueError:
                start = 0

        page = all_keys[start : start + max_keys]
        is_truncated = (start + max_keys) < len(all_keys)

        contents = []
        for key in page:
            obj = self._objects[bucket][key]
            contents.append(
                {
                    "Key": key,
                    "Size": len(obj.data),
                    "LastModified": obj.last_modified,
                    "ETag": obj.etag,
                    "StorageClass": "STANDARD",
                }
            )

        result: dict[str, Any] = {
            "Contents": contents,
            "CommonPrefixes": [{"Prefix": cp} for cp in common_prefixes],
            "IsTruncated": is_truncated,
            "KeyCount": len(contents),
        }
        if is_truncated:
            result["NextContinuationToken"] = str(start + max_keys)
        return result

    async def put_object(self, bucket: str, key: str, body: IO[bytes]) -> None:
        self._logger.info("put_object bucket=%s key=%s", bucket, key)
        self._require_bucket(bucket)
        data = body.read()
        content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
        self._objects[bucket][key] = _StoredObject(data, content_type)

    async def get_object(
        self, bucket: str, key: str, version_id: Optional[str] = None
    ) -> dict:
        self._logger.info("get_object bucket=%s key=%s", bucket, key)
        obj = self._require_object(bucket, key)
        body = io.BytesIO(obj.data)
        # Mimic boto3 streaming body with iter_chunks
        body.iter_chunks = lambda chunk_size=1024: iter([obj.data])  # type: ignore[attr-defined]
        return {
            "Body": body,
            "ContentType": obj.content_type,
            "ContentLength": len(obj.data),
            "ETag": obj.etag,
        }

    async def head_object(self, bucket: str, key: str) -> dict:
        self._logger.info("head_object bucket=%s key=%s", bucket, key)
        obj = self._require_object(bucket, key)
        return {
            "ContentLength": len(obj.data),
            "ContentType": obj.content_type,
            "ETag": obj.etag,
            "LastModified": obj.last_modified,
        }

    async def delete_object(
        self, bucket: str, key: str, version_id: Optional[str] = None
    ) -> dict:
        self._logger.info("delete_object bucket=%s key=%s", bucket, key)
        self._require_bucket(bucket)
        self._objects.get(bucket, {}).pop(key, None)
        self._object_acls.pop((bucket, key), None)
        return {}

    async def copy_object(
        self,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
    ) -> dict:
        self._logger.info(
            "copy_object %s/%s -> %s/%s", src_bucket, src_key, dst_bucket, dst_key
        )
        src_obj = self._require_object(src_bucket, src_key)
        self._require_bucket(dst_bucket)
        self._objects[dst_bucket][dst_key] = _StoredObject(
            src_obj.data,
            src_obj.content_type,
        )
        return {}

    async def delete_objects(self, bucket: str, keys: List[str]) -> dict:
        self._logger.info("delete_objects bucket=%s count=%d", bucket, len(keys))
        self._require_bucket(bucket)
        deleted = []
        for key in keys:
            if key in self._objects.get(bucket, {}):
                del self._objects[bucket][key]
                self._object_acls.pop((bucket, key), None)
                deleted.append({"Key": key})
        return {"Deleted": deleted, "Errors": []}

    # ── Versioning ────────────────────────────────────────────────────

    async def get_bucket_versioning(self, bucket: str) -> dict:
        self._logger.info("get_bucket_versioning bucket=%s", bucket)
        self._require_bucket(bucket)
        status = self._versioning.get(bucket)
        return {"Status": status} if status else {}

    async def put_bucket_versioning(self, bucket: str, status: str) -> dict:
        self._logger.info("put_bucket_versioning bucket=%s status=%s", bucket, status)
        self._require_bucket(bucket)
        self._versioning[bucket] = status
        return {}

    async def list_object_versions(
        self,
        bucket: str,
        prefix: Optional[str] = None,
        max_keys: int = 1000,
        key_marker: Optional[str] = None,
        version_id_marker: Optional[str] = None,
    ) -> dict:
        self._logger.info("list_object_versions bucket=%s prefix=%s", bucket, prefix)
        self._require_bucket(bucket)
        # Mock: no real versioning — return current objects as single versions
        all_keys = sorted(self._objects.get(bucket, {}).keys())
        if prefix:
            all_keys = [k for k in all_keys if k.startswith(prefix)]
        if key_marker:
            all_keys = [k for k in all_keys if k > key_marker]
        page = all_keys[:max_keys]
        versions = []
        for key in page:
            obj = self._objects[bucket][key]
            versions.append(
                {
                    "Key": key,
                    "VersionId": "null",
                    "IsLatest": True,
                    "Size": len(obj.data),
                    "LastModified": obj.last_modified,
                    "ETag": obj.etag,
                }
            )
        return {
            "Versions": versions,
            "DeleteMarkers": [],
            "IsTruncated": len(all_keys) > max_keys,
        }

    # ── ACLs ──────────────────────────────────────────────────────────

    _DEFAULT_ACL = {
        "Owner": {"DisplayName": "admin", "ID": "admin"},
        "Grants": [],
    }

    async def get_bucket_acl(self, bucket: str) -> dict:
        self._logger.info("get_bucket_acl bucket=%s", bucket)
        self._require_bucket(bucket)
        return self._bucket_acls.get(bucket, self._DEFAULT_ACL.copy())

    async def put_bucket_acl(self, bucket: str, acl: dict) -> dict:
        self._logger.info("put_bucket_acl bucket=%s", bucket)
        self._require_bucket(bucket)
        self._bucket_acls[bucket] = acl
        return {}

    async def get_object_acl(self, bucket: str, key: str) -> dict:
        self._logger.info("get_object_acl bucket=%s key=%s", bucket, key)
        self._require_object(bucket, key)
        return self._object_acls.get((bucket, key), self._DEFAULT_ACL.copy())

    async def put_object_acl(self, bucket: str, key: str, acl: dict) -> dict:
        self._logger.info("put_object_acl bucket=%s key=%s", bucket, key)
        self._require_object(bucket, key)
        self._object_acls[(bucket, key)] = acl
        return {}

    # ── CORS ───────────────────────────────────────────────────────────

    async def get_bucket_cors(self, bucket: str) -> dict:
        self._logger.info("get_bucket_cors bucket=%s", bucket)
        self._require_bucket(bucket)
        cors = self._bucket_cors.get(bucket)
        if cors is None:
            raise StorageError(
                "NoSuchCORSConfiguration",
                "The CORS configuration does not exist",
                404,
            )
        return cors

    async def put_bucket_cors(self, bucket: str, cors_configuration: dict) -> dict:
        self._logger.info("put_bucket_cors bucket=%s", bucket)
        self._require_bucket(bucket)
        self._bucket_cors[bucket] = cors_configuration
        return {}

    async def delete_bucket_cors(self, bucket: str) -> dict:
        self._logger.info("delete_bucket_cors bucket=%s", bucket)
        self._require_bucket(bucket)
        self._bucket_cors.pop(bucket, None)
        return {}

    # ── List multipart uploads ────────────────────────────────────────

    async def list_multipart_uploads(
        self,
        bucket: str,
        prefix: Optional[str] = None,
        max_uploads: int = 1000,
    ) -> dict:
        self._logger.info("list_multipart_uploads bucket=%s prefix=%s", bucket, prefix)
        self._require_bucket(bucket)
        uploads = []
        for upload_id, info in self._multipart_uploads.items():
            if info["bucket"] != bucket:
                continue
            if prefix and not info["key"].startswith(prefix):
                continue
            uploads.append(
                {
                    "Key": info["key"],
                    "UploadId": upload_id,
                    "Initiated": datetime.now(timezone.utc).isoformat(),
                    "StorageClass": "STANDARD",
                }
            )
            if len(uploads) >= max_uploads:
                break
        return {
            "Uploads": uploads,
            "IsTruncated": False,
        }

    # ── Presigned URLs ────────────────────────────────────────────────

    async def generate_presigned_url(
        self,
        bucket: str,
        key: str,
        expires_in: int = 3600,
        method: str = "get_object",
        extra_params: dict[str, str | int] | None = None,
    ) -> str:
        self._logger.info(
            "generate_presigned_url bucket=%s key=%s method=%s expires_in=%d",
            bucket,
            key,
            method,
            expires_in,
        )
        self._require_bucket(bucket)
        # Only require existing object for read operations
        if method == "get_object":
            self._require_object(bucket, key)
        encoded_key = quote(key, safe="")
        qs_params: dict[str, str] = {
            "method": method,
            "expires_in": str(expires_in),
        }
        if extra_params:
            for k, v in extra_params.items():
                qs_params[k] = str(v)
        return f"http://localhost:8000/presigned/{quote(bucket, safe='')}/{encoded_key}?{urlencode(qs_params)}"

    # ── Multipart uploads ────────────────────────────────────────────

    def _require_upload(
        self, upload_id: str, bucket: str | None = None, key: str | None = None
    ) -> dict[str, Any]:
        upload = self._multipart_uploads.get(upload_id)
        if upload is None or (
            bucket is not None
            and key is not None
            and (upload["bucket"] != bucket or upload["key"] != key)
        ):
            raise StorageError(
                "NoSuchUpload",
                f"Upload ID '{upload_id}' does not exist",
                404,
            )
        return upload

    async def create_multipart_upload(self, bucket: str, key: str) -> dict:
        self._logger.info("create_multipart_upload bucket=%s key=%s", bucket, key)
        self._require_bucket(bucket)
        self._upload_counter += 1
        upload_id = f"mock-upload-{self._upload_counter}"
        self._multipart_uploads[upload_id] = {
            "bucket": bucket,
            "key": key,
            "parts": {},
        }
        return {"Bucket": bucket, "Key": key, "UploadId": upload_id}

    async def upload_part(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        part_number: int,
        body: IO[bytes],
    ) -> dict:
        self._logger.info(
            "upload_part bucket=%s key=%s upload_id=%s part=%d",
            bucket,
            key,
            upload_id,
            part_number,
        )
        self._require_bucket(bucket)
        upload = self._require_upload(upload_id, bucket, key)
        data = body.read()
        etag = f'"{hashlib.md5(data).hexdigest()}"'
        upload["parts"][part_number] = {"data": data, "etag": etag}
        return {"ETag": etag}

    async def complete_multipart_upload(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        parts: List[dict],
    ) -> dict:
        self._logger.info(
            "complete_multipart_upload bucket=%s key=%s upload_id=%s",
            bucket,
            key,
            upload_id,
        )
        self._require_bucket(bucket)
        upload = self._require_upload(upload_id, bucket, key)
        # Assemble parts in order
        assembled = b""
        for part in sorted(parts, key=lambda p: p["PartNumber"]):
            pn = part["PartNumber"]
            if pn not in upload["parts"]:
                raise StorageError("InvalidPart", f"Part {pn} not found", 400)
            assembled += upload["parts"][pn]["data"]

        self._objects[bucket][key] = _StoredObject(assembled)
        del self._multipart_uploads[upload_id]
        return {
            "Bucket": bucket,
            "Key": key,
            "ETag": self._objects[bucket][key].etag,
        }

    async def abort_multipart_upload(
        self, bucket: str, key: str, upload_id: str
    ) -> dict:
        self._logger.info(
            "abort_multipart_upload bucket=%s key=%s upload_id=%s",
            bucket,
            key,
            upload_id,
        )
        self._require_bucket(bucket)
        self._require_upload(upload_id, bucket, key)
        del self._multipart_uploads[upload_id]
        return {}

    async def list_parts(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        max_parts: int = 1000,
    ) -> dict:
        self._logger.info(
            "list_parts bucket=%s key=%s upload_id=%s", bucket, key, upload_id
        )
        self._require_bucket(bucket)
        upload = self._require_upload(upload_id, bucket, key)
        parts_list = []
        for pn in sorted(upload["parts"].keys())[:max_parts]:
            part_data = upload["parts"][pn]
            parts_list.append(
                {
                    "PartNumber": pn,
                    "ETag": part_data["etag"],
                    "Size": len(part_data["data"]),
                    "LastModified": datetime.now(timezone.utc).isoformat(),
                }
            )
        return {
            "Parts": parts_list,
            "IsTruncated": len(upload["parts"]) > max_parts,
        }


# ── Seed data ────────────────────────────────────────────────────────


def seed_s3(s3: MockS3Service) -> None:
    """Pre-populate the mock S3 with sample buckets and objects."""
    now = datetime.now(timezone.utc).isoformat()

    for name in ("documents", "archives", "logs"):
        s3._buckets[name] = {"CreationDate": now}
        s3._objects[name] = {}

    # Sample objects
    s3._objects["documents"]["readme.txt"] = _StoredObject(
        b"Welcome to the HCP mock server.\nThis is sample data for development.",
        "text/plain",
    )
    s3._objects["documents"]["reports/q1.csv"] = _StoredObject(
        b"month,revenue\nJan,1000\nFeb,1200\nMar,1500\n",
        "text/csv",
    )
    s3._objects["documents"]["reports/q2.csv"] = _StoredObject(
        b"month,revenue\nApr,1100\nMay,1300\nJun,1600\n",
        "text/csv",
    )
    s3._objects["archives"]["2024-backup.tar.gz"] = _StoredObject(
        b"\x1f\x8b" + b"\x00" * 50,  # fake gzip header
        "application/gzip",
    )
    s3._objects["logs"]["app.log"] = _StoredObject(
        b"2024-01-01 INFO  Server started\n2024-01-01 INFO  Ready\n",
        "text/plain",
    )

    # Sync: ensure every S3 bucket also exists as a MAPI namespace
    for bucket_name in s3._buckets:
        s3._sync_create_namespace(bucket_name)
