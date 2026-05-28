"""Schemas for the batches resource — list/summary/random + the repository
row models used internally by services. `BatchPublic` (the row schema)
lives in `viewer.models.batch` so it shares a base with the ORM model.
"""

from pydantic import BaseModel

from viewer.models.batch import BatchPublic
from viewer.models.enums import HtrStatus


class BatchAccessibleSummary(BaseModel):
    batches: int
    expected: int
    cached: int
    transcribed: int


class BatchSummary(BaseModel):
    total_batches: int
    accessible: BatchAccessibleSummary
    by_manifest_status: dict[str, int]
    by_htr_status: dict[str, int]


class BatchListResponse(BaseModel):
    generated_at: str | None = None
    summary: BatchSummary
    batches: list[BatchPublic]


class RandomBatchResponse(BaseModel):
    batch_id: str
    status: HtrStatus


class BrowseRow(BaseModel):
    """One row of `repositories.batch.browse_at_tier`."""

    batch_id: str
    cached_pages: int
    transcribed_pages: int


class StatusCounts(BaseModel):
    """Returned by `repositories.batch.status_counts` — two named dicts so
    callers don't need to remember the tuple order."""

    by_manifest_status: dict[str, int]
    by_htr_status: dict[str, int]


class BatchStatusSets(BaseModel):
    """Per-tier set of batch_ids for a given list of bild_ids.

    Strictly nested: `transcribed ⊆ cached ⊆ listed`.
    """

    listed: set[str]
    cached: set[str]
    transcribed: set[str]
