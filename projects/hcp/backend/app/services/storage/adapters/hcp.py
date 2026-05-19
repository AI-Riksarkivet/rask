"""HCP storage adapter — aioboto3 wrapper with HCP-specific workarounds.

HCP-specific quirks:
  - Region redirector disabled (HCP returns non-standard redirects)
  - Multi-delete uses individual deletes (HCP requires Content-MD5 but
    boto3 sends CRC32 for the batch delete API)
  - Path-style addressing (HCP doesn't support virtual-hosted buckets)
"""

from __future__ import annotations

from contextlib import AsyncExitStack

import aioboto3
from aiobotocore.config import AioConfig
from botocore.exceptions import BotoCoreError, ClientError
from opentelemetry import trace

from app.core.config import S3Settings
from app.services.storage.adapters._boto3_ops import Boto3Forwarder, Boto3Operations
from app.services.storage.errors import from_client_error, from_transport_error

tracer = trace.get_tracer(__name__)


class HcpStorage(Boto3Forwarder):
    """Async aioboto3 wrapper for HCP.

    Implements StorageProtocol via composition: a Boto3Operations instance
    (``self._ops``) provides the shared S3 methods.  Boto3Forwarder supplies
    typed forwarding methods for protocol compliance.  Only HCP-specific
    overrides are defined here.
    """

    _BOTO_CONFIG = AioConfig(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
        retries={"max_attempts": 3, "mode": "adaptive"},
        connect_timeout=10,
        read_timeout=60,
    )

    def __init__(
        self,
        settings: S3Settings,
        *,
        access_key: str | None = None,
        secret_key: str | None = None,
        endpoint_url: str | None = None,
    ):
        self.settings = settings
        self._access_key = access_key or settings.access_key
        self._secret_key = secret_key or settings.secret_key
        self._endpoint_url = endpoint_url or settings.endpoint_url
        self._ops = Boto3Operations()
        self._exit_stack = AsyncExitStack()

    @classmethod
    def with_credentials(
        cls,
        settings: S3Settings,
        access_key: str,
        secret_key: str,
        endpoint_url: str | None = None,
    ) -> HcpStorage:
        """Create an HcpStorage with explicit credentials."""
        return cls(
            settings,
            access_key=access_key,
            secret_key=secret_key,
            endpoint_url=endpoint_url,
        )

    async def connect(self) -> None:
        """Enter the aioboto3 client context manager."""
        session = aioboto3.Session(
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
        )
        client = await self._exit_stack.enter_async_context(
            session.client(
                "s3",
                endpoint_url=self._endpoint_url,
                region_name=self.settings.region,
                verify=self.settings.verify_ssl,
                config=self._BOTO_CONFIG,
            )
        )
        # HCP returns non-standard redirects that trigger boto3's
        # S3RegionRedirectorv2 → TypeError on list_buckets. Safe to
        # disable for any non-AWS S3-compatible endpoint.
        client.meta.events.unregister("needs-retry.s3")
        self._ops._client = client

    async def close(self) -> None:
        """Exit the aioboto3 client context manager."""
        await self._exit_stack.aclose()

    # -- HCP-specific overrides ---------------------------------------------

    async def list_buckets(self) -> dict:
        """Override: also catches TypeError from residual region redirector."""
        with tracer.start_as_current_span("s3.list_buckets"):
            try:
                return await self._ops._client.list_buckets()
            except ClientError as exc:
                raise from_client_error(exc) from exc
            except (BotoCoreError, TypeError) as exc:
                raise from_transport_error(exc) from exc

    async def delete_objects(self, bucket: str, keys: list[str]) -> dict:
        """Delete multiple objects individually (HCP requires Content-MD5
        for the multi-delete API but boto3 sends CRC32 instead)."""
        with tracer.start_as_current_span(
            "s3.delete_objects",
            attributes={"s3.bucket": bucket, "s3.key_count": len(keys)},
        ):
            errors: list[dict] = []
            for key in keys:
                try:
                    await self._ops._client.delete_object(Bucket=bucket, Key=key)
                except ClientError as exc:
                    errors.append(
                        {
                            "Key": key,
                            "Code": exc.response["Error"].get("Code", "Unknown"),
                            "Message": exc.response["Error"].get("Message", ""),
                        }
                    )
            return {"Errors": errors} if errors else {}
