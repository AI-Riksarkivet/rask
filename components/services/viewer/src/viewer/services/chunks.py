"""Chunk aggregations derived from the batches table."""

from sqlmodel.ext.asyncio.session import AsyncSession

from viewer.repositories import batch as batch_repo
from viewer.schemas.chunk import Chunk, ChunkListResponse


async def list_chunks(session: AsyncSession) -> ChunkListResponse:
    rows = await batch_repo.chunks_summary(session)
    chunks = [
        Chunk(
            chunk_id=cid,
            chunk_total=ctotal,
            batches=n,
            expected_pages=expected,
            cached_pages=cached,
            transcribed_pages=transcribed,
            done_batches=done,
        )
        for cid, ctotal, n, expected, cached, transcribed, done in rows
    ]
    return ChunkListResponse(chunks=chunks)
