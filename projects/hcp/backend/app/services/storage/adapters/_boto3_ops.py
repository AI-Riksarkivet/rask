"""Shared async boto3 method implementations for S3-compatible storage adapters.

Boto3Operations is a standalone class that holds an aioboto3 client and
provides the S3 methods that are identical across HcpStorage and
GenericBoto3Storage.

Boto3Forwarder is a typed forwarding base class: each concrete adapter
inherits from it and composes a Boto3Operations instance (``self._ops``).
Override methods defined directly on the adapter take precedence.

Overrides per adapter:
  - HcpStorage overrides list_buckets (catches TypeError) and
    delete_objects (individual deletes for HCP Content-MD5 bug)
  - GenericBoto3Storage overrides delete_objects (native batch) and
    4 ACL methods (raise NotSupported)
"""

from __future__ import annotations

from typing import IO, Any

from botocore.exceptions import BotoCoreError, ClientError
from opentelemetry import trace

from app.services.storage.errors import from_client_error, from_transport_error

tracer = trace.get_tracer(__name__)


class Boto3Operations:
    """Shared async S3 method implementations.

    The ``_client`` attribute is an aioboto3 S3 client, set by the owning
    adapter after calling ``connect()``.
    """

    _client: Any  # aioboto3 S3 client (set after connect)

    # -- Bucket operations --------------------------------------------------

    async def list_buckets(self) -> dict:
        with tracer.start_as_current_span("s3.list_buckets"):
            try:
                return await self._client.list_buckets()
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    async def create_bucket(self, name: str) -> dict:
        with tracer.start_as_current_span(
            "s3.create_bucket", attributes={"s3.bucket": name}
        ):
            try:
                return await self._client.create_bucket(Bucket=name)
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    async def head_bucket(self, name: str) -> dict:
        with tracer.start_as_current_span(
            "s3.head_bucket", attributes={"s3.bucket": name}
        ):
            try:
                return await self._client.head_bucket(Bucket=name)
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    async def delete_bucket(self, name: str) -> dict:
        with tracer.start_as_current_span(
            "s3.delete_bucket", attributes={"s3.bucket": name}
        ):
            try:
                return await self._client.delete_bucket(Bucket=name)
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    # -- Object operations --------------------------------------------------

    async def list_objects(
        self,
        bucket: str,
        prefix: str | None = None,
        max_keys: int = 1000,
        continuation_token: str | None = None,
        delimiter: str | None = None,
        fetch_owner: bool = True,
    ) -> dict:
        with tracer.start_as_current_span(
            "s3.list_objects",
            attributes={"s3.bucket": bucket, "s3.prefix": prefix or ""},
        ):
            try:
                kwargs: dict[str, Any] = {"Bucket": bucket, "MaxKeys": max_keys}
                if prefix:
                    kwargs["Prefix"] = prefix
                if continuation_token:
                    kwargs["ContinuationToken"] = continuation_token
                if delimiter:
                    kwargs["Delimiter"] = delimiter
                if fetch_owner:
                    kwargs["FetchOwner"] = True
                return await self._client.list_objects_v2(**kwargs)
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    async def put_object(self, bucket: str, key: str, body: IO[bytes]) -> None:
        with tracer.start_as_current_span(
            "s3.put_object",
            attributes={"s3.bucket": bucket, "s3.key": key},
        ):
            try:
                await self._client.upload_fileobj(body, bucket, key)
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    async def get_object(
        self, bucket: str, key: str, version_id: str | None = None
    ) -> dict:
        with tracer.start_as_current_span(
            "s3.get_object",
            attributes={"s3.bucket": bucket, "s3.key": key},
        ):
            try:
                kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
                if version_id:
                    kwargs["VersionId"] = version_id
                return await self._client.get_object(**kwargs)
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    async def head_object(self, bucket: str, key: str) -> dict:
        with tracer.start_as_current_span(
            "s3.head_object",
            attributes={"s3.bucket": bucket, "s3.key": key},
        ):
            try:
                return await self._client.head_object(Bucket=bucket, Key=key)
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    async def delete_object(
        self, bucket: str, key: str, version_id: str | None = None
    ) -> dict:
        with tracer.start_as_current_span(
            "s3.delete_object",
            attributes={"s3.bucket": bucket, "s3.key": key},
        ):
            try:
                kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
                if version_id:
                    kwargs["VersionId"] = version_id
                return await self._client.delete_object(**kwargs)
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    async def copy_object(
        self,
        src_bucket: str,
        src_key: str,
        dst_bucket: str,
        dst_key: str,
    ) -> dict:
        with tracer.start_as_current_span(
            "s3.copy_object",
            attributes={
                "s3.bucket": dst_bucket,
                "s3.key": dst_key,
                "s3.source_bucket": src_bucket,
                "s3.source_key": src_key,
            },
        ):
            try:
                return await self._client.copy_object(
                    CopySource={"Bucket": src_bucket, "Key": src_key},
                    Bucket=dst_bucket,
                    Key=dst_key,
                )
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    # -- Bucket versioning --------------------------------------------------

    async def get_bucket_versioning(self, bucket: str) -> dict:
        with tracer.start_as_current_span(
            "s3.get_bucket_versioning",
            attributes={"s3.bucket": bucket},
        ):
            try:
                return await self._client.get_bucket_versioning(Bucket=bucket)
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    async def put_bucket_versioning(self, bucket: str, status: str) -> dict:
        with tracer.start_as_current_span(
            "s3.put_bucket_versioning",
            attributes={"s3.bucket": bucket, "s3.versioning_status": status},
        ):
            try:
                return await self._client.put_bucket_versioning(
                    Bucket=bucket,
                    VersioningConfiguration={"Status": status},
                )
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    # -- ACLs ---------------------------------------------------------------

    async def get_bucket_acl(self, bucket: str) -> dict:
        with tracer.start_as_current_span(
            "s3.get_bucket_acl",
            attributes={"s3.bucket": bucket},
        ):
            try:
                return await self._client.get_bucket_acl(Bucket=bucket)
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    async def put_bucket_acl(self, bucket: str, acl: dict) -> dict:
        with tracer.start_as_current_span(
            "s3.put_bucket_acl",
            attributes={"s3.bucket": bucket},
        ):
            try:
                return await self._client.put_bucket_acl(
                    Bucket=bucket,
                    AccessControlPolicy=acl,
                )
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    async def get_object_acl(self, bucket: str, key: str) -> dict:
        with tracer.start_as_current_span(
            "s3.get_object_acl",
            attributes={"s3.bucket": bucket, "s3.key": key},
        ):
            try:
                return await self._client.get_object_acl(Bucket=bucket, Key=key)
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    async def put_object_acl(self, bucket: str, key: str, acl: dict) -> dict:
        with tracer.start_as_current_span(
            "s3.put_object_acl",
            attributes={"s3.bucket": bucket, "s3.key": key},
        ):
            try:
                return await self._client.put_object_acl(
                    Bucket=bucket,
                    Key=key,
                    AccessControlPolicy=acl,
                )
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    # -- Bucket CORS --------------------------------------------------------

    async def get_bucket_cors(self, bucket: str) -> dict:
        with tracer.start_as_current_span(
            "s3.get_bucket_cors",
            attributes={"s3.bucket": bucket},
        ):
            try:
                return await self._client.get_bucket_cors(Bucket=bucket)
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    async def put_bucket_cors(self, bucket: str, cors_configuration: dict) -> dict:
        with tracer.start_as_current_span(
            "s3.put_bucket_cors",
            attributes={"s3.bucket": bucket},
        ):
            try:
                return await self._client.put_bucket_cors(
                    Bucket=bucket,
                    CORSConfiguration=cors_configuration,
                )
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    async def delete_bucket_cors(self, bucket: str) -> dict:
        with tracer.start_as_current_span(
            "s3.delete_bucket_cors",
            attributes={"s3.bucket": bucket},
        ):
            try:
                return await self._client.delete_bucket_cors(Bucket=bucket)
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    # -- Object versions ----------------------------------------------------

    async def list_object_versions(
        self,
        bucket: str,
        prefix: str | None = None,
        max_keys: int = 1000,
        key_marker: str | None = None,
        version_id_marker: str | None = None,
    ) -> dict:
        with tracer.start_as_current_span(
            "s3.list_object_versions",
            attributes={"s3.bucket": bucket, "s3.prefix": prefix or ""},
        ):
            try:
                kwargs: dict[str, Any] = {"Bucket": bucket, "MaxKeys": max_keys}
                if prefix:
                    kwargs["Prefix"] = prefix
                if key_marker:
                    kwargs["KeyMarker"] = key_marker
                if version_id_marker:
                    kwargs["VersionIdMarker"] = version_id_marker
                return await self._client.list_object_versions(**kwargs)
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    # -- Presigned URLs -----------------------------------------------------

    async def generate_presigned_url(
        self,
        bucket: str,
        key: str,
        expires_in: int = 3600,
        method: str = "get_object",
        extra_params: dict[str, str | int] | None = None,
    ) -> str:
        with tracer.start_as_current_span(
            "s3.generate_presigned_url",
            attributes={"s3.bucket": bucket, "s3.key": key, "s3.method": method},
        ):
            try:
                params: dict[str, Any] = {"Bucket": bucket, "Key": key}
                if extra_params:
                    params.update(extra_params)
                return await self._client.generate_presigned_url(
                    method,
                    Params=params,
                    ExpiresIn=expires_in,
                )
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    # -- Multipart uploads --------------------------------------------------

    async def create_multipart_upload(self, bucket: str, key: str) -> dict:
        with tracer.start_as_current_span(
            "s3.create_multipart_upload",
            attributes={"s3.bucket": bucket, "s3.key": key},
        ):
            try:
                return await self._client.create_multipart_upload(
                    Bucket=bucket, Key=key
                )
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    async def upload_part(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        part_number: int,
        body: IO[bytes],
    ) -> dict:
        with tracer.start_as_current_span(
            "s3.upload_part",
            attributes={
                "s3.bucket": bucket,
                "s3.key": key,
                "s3.part_number": part_number,
            },
        ):
            try:
                return await self._client.upload_part(
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=body,
                )
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    async def complete_multipart_upload(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        parts: list[dict],
    ) -> dict:
        with tracer.start_as_current_span(
            "s3.complete_multipart_upload",
            attributes={"s3.bucket": bucket, "s3.key": key},
        ):
            try:
                return await self._client.complete_multipart_upload(
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": parts},
                )
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    async def abort_multipart_upload(
        self, bucket: str, key: str, upload_id: str
    ) -> dict:
        with tracer.start_as_current_span(
            "s3.abort_multipart_upload",
            attributes={"s3.bucket": bucket, "s3.key": key},
        ):
            try:
                return await self._client.abort_multipart_upload(
                    Bucket=bucket, Key=key, UploadId=upload_id
                )
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    async def list_parts(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        max_parts: int = 1000,
    ) -> dict:
        with tracer.start_as_current_span(
            "s3.list_parts",
            attributes={"s3.bucket": bucket, "s3.key": key},
        ):
            try:
                return await self._client.list_parts(
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload_id,
                    MaxParts=max_parts,
                )
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    async def list_multipart_uploads(
        self,
        bucket: str,
        prefix: str | None = None,
        max_uploads: int = 1000,
    ) -> dict:
        with tracer.start_as_current_span(
            "s3.list_multipart_uploads",
            attributes={"s3.bucket": bucket, "s3.prefix": prefix or ""},
        ):
            try:
                kwargs: dict[str, Any] = {"Bucket": bucket, "MaxUploads": max_uploads}
                if prefix:
                    kwargs["Prefix"] = prefix
                return await self._client.list_multipart_uploads(**kwargs)
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc


class Boto3Forwarder:
    """Typed forwarding base for composition with Boto3Operations.

    Subclasses set ``self._ops`` to a ``Boto3Operations`` instance.
    Every public method here is a thin, typed forwarder — the real work
    lives in ``Boto3Operations``.  Override any method in a concrete
    adapter to replace the default behaviour.
    """

    _ops: Boto3Operations

    # -- Bucket operations --------------------------------------------------

    async def list_buckets(self) -> dict:
        return await self._ops.list_buckets()

    async def create_bucket(self, name: str) -> dict:
        return await self._ops.create_bucket(name)

    async def head_bucket(self, name: str) -> dict:
        return await self._ops.head_bucket(name)

    async def delete_bucket(self, name: str) -> dict:
        return await self._ops.delete_bucket(name)

    # -- Object operations --------------------------------------------------

    async def list_objects(
        self,
        bucket: str,
        prefix: str | None = None,
        max_keys: int = 1000,
        continuation_token: str | None = None,
        delimiter: str | None = None,
        fetch_owner: bool = True,
    ) -> dict:
        return await self._ops.list_objects(
            bucket, prefix, max_keys, continuation_token, delimiter, fetch_owner
        )

    async def put_object(self, bucket: str, key: str, body: IO[bytes]) -> None:
        await self._ops.put_object(bucket, key, body)

    async def get_object(
        self, bucket: str, key: str, version_id: str | None = None
    ) -> dict:
        return await self._ops.get_object(bucket, key, version_id)

    async def head_object(self, bucket: str, key: str) -> dict:
        return await self._ops.head_object(bucket, key)

    async def delete_object(
        self, bucket: str, key: str, version_id: str | None = None
    ) -> dict:
        return await self._ops.delete_object(bucket, key, version_id)

    async def copy_object(
        self, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str
    ) -> dict:
        return await self._ops.copy_object(src_bucket, src_key, dst_bucket, dst_key)

    # -- Bucket versioning --------------------------------------------------

    async def get_bucket_versioning(self, bucket: str) -> dict:
        return await self._ops.get_bucket_versioning(bucket)

    async def put_bucket_versioning(self, bucket: str, status: str) -> dict:
        return await self._ops.put_bucket_versioning(bucket, status)

    # -- ACLs ---------------------------------------------------------------

    async def get_bucket_acl(self, bucket: str) -> dict:
        return await self._ops.get_bucket_acl(bucket)

    async def put_bucket_acl(self, bucket: str, acl: dict) -> dict:
        return await self._ops.put_bucket_acl(bucket, acl)

    async def get_object_acl(self, bucket: str, key: str) -> dict:
        return await self._ops.get_object_acl(bucket, key)

    async def put_object_acl(self, bucket: str, key: str, acl: dict) -> dict:
        return await self._ops.put_object_acl(bucket, key, acl)

    # -- Bucket CORS --------------------------------------------------------

    async def get_bucket_cors(self, bucket: str) -> dict:
        return await self._ops.get_bucket_cors(bucket)

    async def put_bucket_cors(self, bucket: str, cors_configuration: dict) -> dict:
        return await self._ops.put_bucket_cors(bucket, cors_configuration)

    async def delete_bucket_cors(self, bucket: str) -> dict:
        return await self._ops.delete_bucket_cors(bucket)

    # -- Object versions ----------------------------------------------------

    async def list_object_versions(
        self,
        bucket: str,
        prefix: str | None = None,
        max_keys: int = 1000,
        key_marker: str | None = None,
        version_id_marker: str | None = None,
    ) -> dict:
        return await self._ops.list_object_versions(
            bucket, prefix, max_keys, key_marker, version_id_marker
        )

    # -- Presigned URLs -----------------------------------------------------

    async def generate_presigned_url(
        self,
        bucket: str,
        key: str,
        expires_in: int = 3600,
        method: str = "get_object",
        extra_params: dict[str, str | int] | None = None,
    ) -> str:
        return await self._ops.generate_presigned_url(
            bucket, key, expires_in, method, extra_params
        )

    # -- Multipart uploads --------------------------------------------------

    async def create_multipart_upload(self, bucket: str, key: str) -> dict:
        return await self._ops.create_multipart_upload(bucket, key)

    async def upload_part(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        part_number: int,
        body: IO[bytes],
    ) -> dict:
        return await self._ops.upload_part(bucket, key, upload_id, part_number, body)

    async def complete_multipart_upload(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        parts: list[dict],
    ) -> dict:
        return await self._ops.complete_multipart_upload(bucket, key, upload_id, parts)

    async def abort_multipart_upload(
        self, bucket: str, key: str, upload_id: str
    ) -> dict:
        return await self._ops.abort_multipart_upload(bucket, key, upload_id)

    async def list_parts(
        self,
        bucket: str,
        key: str,
        upload_id: str,
        max_parts: int = 1000,
    ) -> dict:
        return await self._ops.list_parts(bucket, key, upload_id, max_parts)

    async def list_multipart_uploads(
        self,
        bucket: str,
        prefix: str | None = None,
        max_uploads: int = 1000,
    ) -> dict:
        return await self._ops.list_multipart_uploads(bucket, prefix, max_uploads)
