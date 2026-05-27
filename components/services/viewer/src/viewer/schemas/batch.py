"""Wrapper response schemas — the public row schema (`BatchPublic`) lives in
`viewer.models.batch` so it shares a base with the ORM model."""

from pydantic import BaseModel

from viewer.models.batch import BatchPublic


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
    status: str


class SyncResponse(BatchListResponse):
    pass
