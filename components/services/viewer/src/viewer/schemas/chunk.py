from pydantic import BaseModel, field_validator

from viewer.models.pipelines import DEFAULT_PIPELINE, PIPELINE_SPECS


class SubmitRequest(BaseModel):
    """Body for POST /chunks/{id}/submit.

    `pipeline` is an open string validated against the registry (not a
    StrEnum/Literal) so adding a runner pipeline only touches PIPELINE_SPECS —
    the API surface stays open. An unknown name raises a ValidationError, which
    the RequestValidationError handler renders as 422.
    """

    pipeline: str = DEFAULT_PIPELINE

    @field_validator("pipeline")
    @classmethod
    def _known_pipeline(cls, v: str) -> str:
        if v not in PIPELINE_SPECS:
            raise ValueError(f"unknown pipeline {v!r}; choose from {sorted(PIPELINE_SPECS)}")
        return v


class Chunk(BaseModel):
    """One row in /api/v1/chunks/, also returned by `repositories.batch.chunks_summary`."""

    chunk_id: int
    chunk_total: int
    batches: int
    expected_pages: int
    cached_pages: int
    transcribed_pages: int
    done_batches: int


class ChunkListResponse(BaseModel):
    chunks: list[Chunk]


class ChunkProgress(BaseModel):
    """Returned by `repositories.batch.chunks_with_progress`. Used by the
    orchestrator's `derive_state` to decide which chunks are ready for HTR.
    """

    chunk_id: int
    expected_pages: int
    cached_pages: int
    transcribed_pages: int


class SubmitResult(BaseModel):
    chunk_id: int
    chunk_total: int
    pipeline: str
    submission_id: str
    batches: list[str]


class StopResult(BaseModel):
    chunk_id: int
    stopped_submission_id: str
    stopped: bool


class ChunkBatches(BaseModel):
    """Membership of one chunk — returned by `submission._fetch_chunk_batches`."""

    chunk_total: int
    batch_ids: list[str]
