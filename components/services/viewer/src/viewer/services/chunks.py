"""Chunk aggregations derived from the batches table."""

from sqlmodel.ext.asyncio.session import AsyncSession

from viewer.repositories import batch as batch_repo
from viewer.schemas.chunk import ChunkListResponse


async def list_chunks(session: AsyncSession) -> ChunkListResponse:
    return ChunkListResponse(chunks=await batch_repo.chunks_summary(session))
