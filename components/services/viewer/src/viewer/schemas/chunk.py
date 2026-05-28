from pydantic import BaseModel

from viewer.services.submission import SubmitResult


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


SubmitChunkResponse = SubmitResult
