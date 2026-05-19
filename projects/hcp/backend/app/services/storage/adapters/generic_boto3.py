"""Generic boto3 storage adapter for S3-compatible backends (MinIO, Ceph, AWS).

No HCP-specific workarounds:
  - Region redirector left intact (standard S3 behavior)
  - Native batch delete_objects (no Content-MD5 issue)
  - Configurable addressing style (path/virtual/auto)
  - ACL methods raise StorageOperationNotSupported (MinIO deprecated ACLs)
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack

import aioboto3
from aiobotocore.config import AioConfig
from botocore.exceptions import BotoCoreError, ClientError
from opentelemetry import trace

from app.core.config import StorageSettings
from app.services.storage.adapters._boto3_ops import Boto3Forwarder, Boto3Operations
from app.services.storage.errors import (
    StorageOperationNotSupported,
    from_client_error,
    from_transport_error,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class GenericBoto3Storage(Boto3Forwarder):
    """Standard async S3 adapter — no vendor-specific hacks.

    Implements StorageProtocol via composition: a Boto3Operations instance
    (``self._ops``) provides the shared S3 methods.  Boto3Forwarder supplies
    typed forwarding methods for protocol compliance.  Only GenericBoto3-specific
    overrides are defined here.
    """

    def __init__(
        self,
        settings: StorageSettings,
        *,
        access_key: str | None = None,
        secret_key: str | None = None,
        endpoint_url: str | None = None,
    ):
        self.settings = settings
        self._access_key = access_key or settings.s3_access_key
        self._secret_key = secret_key or settings.s3_secret_key.get_secret_value()
        self._endpoint_url = endpoint_url or settings.s3_endpoint_url or None
        self._ops = Boto3Operations()
        self._exit_stack = AsyncExitStack()

    @classmethod
    def with_credentials(
        cls,
        settings: StorageSettings,
        access_key: str,
        secret_key: str,
        endpoint_url: str | None = None,
    ) -> GenericBoto3Storage:
        """Create a GenericBoto3Storage with explicit credentials."""
        return cls(
            settings,
            access_key=access_key,
            secret_key=secret_key,
            endpoint_url=endpoint_url,
        )

    async def connect(self) -> None:
        """Enter the aioboto3 client context manager."""
        boto_config = AioConfig(
            signature_version="s3v4",
            s3={"addressing_style": self.settings.s3_addressing_style},
            retries={"max_attempts": 3, "mode": "adaptive"},
            connect_timeout=10,
            read_timeout=60,
        )
        session = aioboto3.Session(
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
        )
        client = await self._exit_stack.enter_async_context(
            session.client(
                "s3",
                endpoint_url=self._endpoint_url,
                region_name=self.settings.s3_region,
                verify=self.settings.s3_verify_ssl,
                config=boto_config,
            )
        )
        self._ops._client = client

    async def close(self) -> None:
        """Exit the aioboto3 client context manager."""
        await self._exit_stack.aclose()

    # -- GenericBoto3-specific overrides ------------------------------------

    async def delete_objects(self, bucket: str, keys: list[str]) -> dict:
        """Native batch delete — works on standard S3-compatible backends."""
        with tracer.start_as_current_span(
            "s3.delete_objects",
            attributes={"s3.bucket": bucket, "s3.key_count": len(keys)},
        ):
            try:
                result = await self._ops._client.delete_objects(
                    Bucket=bucket,
                    Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True},
                )
                errors = result.get("Errors", [])
                return {"Errors": errors} if errors else {}
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except BotoCoreError as exc:
                raise from_transport_error(exc) from exc

    # -- ACLs (not supported on MinIO) -------------------------------------

    async def get_bucket_acl(self, bucket: str) -> dict:
        raise StorageOperationNotSupported("get_bucket_acl", "minio")

    async def put_bucket_acl(self, bucket: str, acl: dict) -> dict:
        raise StorageOperationNotSupported("put_bucket_acl", "minio")

    async def get_object_acl(self, bucket: str, key: str) -> dict:
        raise StorageOperationNotSupported("get_object_acl", "minio")

    async def put_object_acl(self, bucket: str, key: str, acl: dict) -> dict:
        raise StorageOperationNotSupported("put_object_acl", "minio")

    # -- Bucket CORS (not supported on MinIO) ----------------------------------

    async def get_bucket_cors(self, bucket: str) -> dict:
        raise StorageOperationNotSupported("get_bucket_cors", "minio")

    async def put_bucket_cors(self, bucket: str, cors_configuration: dict) -> dict:
        raise StorageOperationNotSupported("put_bucket_cors", "minio")

    async def delete_bucket_cors(self, bucket: str) -> dict:
        raise StorageOperationNotSupported("delete_bucket_cors", "minio")
