"""Bulk transfer engine — producer-consumer pipelines for large-scale S3 operations.

::

    bulk/
        protocol.py   — S3Client, BulkClient, TransferSettings (interfaces)
        config.py     — BulkUploadConfig, BulkDownloadConfig, TransferStats
        helpers.py    — Counters, pipeline runner, pooling, validation
        upload.py     — bulk_upload()
        download.py   — bulk_download()
"""

from rahcp_client.bulk.config import (
    BulkDownloadConfig,
    BulkUploadConfig,
    TransferStats,
)
from rahcp_client.bulk.download import bulk_download
from rahcp_client.bulk.upload import bulk_upload

__all__ = [
    "BulkDownloadConfig",
    "BulkUploadConfig",
    "TransferStats",
    "bulk_download",
    "bulk_upload",
]
