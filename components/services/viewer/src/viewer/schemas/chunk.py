from pydantic import BaseModel


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
