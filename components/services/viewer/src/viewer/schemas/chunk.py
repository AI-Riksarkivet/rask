from pydantic import BaseModel

from control import SubmitResult


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


# Wire response == library result; viewer doesn't add any fields on top.
SubmitChunkResponse = SubmitResult
