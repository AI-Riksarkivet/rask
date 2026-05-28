from pydantic import BaseModel


class Chunk(BaseModel):
    chunk_id: int
    chunk_total: int
    batches: int
    expected_pages: int
    cached_pages: int
    transcribed_pages: int
    done_batches: int


class ChunkListResponse(BaseModel):
    chunks: list[Chunk]


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
