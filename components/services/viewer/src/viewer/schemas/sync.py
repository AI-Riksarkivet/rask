"""Schemas for the S3 → DB reconciliation operation."""

from pydantic import BaseModel

from viewer.schemas.batch import BatchListResponse


class SyncResult(BaseModel):
    """Returned by `services.sync.reconcile_from_s3` — summary of one
    S3 → DB reconciliation pass."""

    cached_total: int
    transcribed_total: int
    rows_updated: int
    by_status: dict[str, int]


class SyncResponse(BatchListResponse):
    """POST /api/v1/batches/sync — returns the post-sync batches snapshot."""
