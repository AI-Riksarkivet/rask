"""rahcp-client — Async Python client for HCP Unified API."""

from __future__ import annotations

from rahcp_client.client import HCPClient
from rahcp_client.config import HCPSettings
from rahcp_client.errors import (
    AuthenticationError,
    ConflictError,
    HCPError,
    NotFoundError,
    RetryableError,
    UpstreamError,
)
from rahcp_client.bulk import (
    BulkDownloadConfig,
    BulkUploadConfig,
    TransferStats,
    bulk_download,
    bulk_upload,
)
from rahcp_tracker import TrackerProtocol, TransferStatus, TransferTracker

__all__ = [
    "AuthenticationError",
    "BulkDownloadConfig",
    "BulkUploadConfig",
    "ConflictError",
    "HCPClient",
    "HCPError",
    "HCPSettings",
    "NotFoundError",
    "RetryableError",
    "TrackerProtocol",
    "TransferStats",
    "TransferStatus",
    "TransferTracker",
    "UpstreamError",
    "bulk_download",
    "bulk_upload",
]
