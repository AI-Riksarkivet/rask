"""Data access for the batches table — async via SQLModel + AsyncSession.

Repositories return ORM rows for full-row reads and named Pydantic schemas
(`BatchAccessibleSummary`, `Chunk`, `ChunkProgress`, `BrowseRow`,
`StatusCounts`) for aggregates. Services pass these through; the ORM-vs-API
mapping lives at the row layer via `BatchPublic.model_validate(row)`.
"""

from sqlalchemy import func
from sqlalchemy.sql import ColumnElement
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from viewer.models.batch import Batch
from viewer.models.enums import BrowseTier, HtrStatus, ManifestStatus
from viewer.schemas.batch import BatchAccessibleSummary, BrowseRow, StatusCounts
from viewer.schemas.chunk import Chunk, ChunkProgress


async def list_all(session: AsyncSession) -> list[Batch]:
    result = await session.exec(select(Batch).order_by(Batch.batch_id))
    return list(result.all())


async def get(session: AsyncSession, batch_id: str) -> Batch | None:
    return await session.get(Batch, batch_id)


async def random_by_status(session: AsyncSession, status: HtrStatus) -> Batch | None:
    result = await session.exec(select(Batch).where(Batch.htr_status == status).order_by(func.random()).limit(1))
    return result.first()


async def by_ids(session: AsyncSession, batch_ids: list[str]) -> list[Batch]:
    if not batch_ids:
        return []
    result = await session.exec(select(Batch).where(col(Batch.batch_id).in_(batch_ids)))
    return list(result.all())


async def status_counts(session: AsyncSession) -> StatusCounts:
    manifest = await session.exec(select(Batch.manifest_status, func.count()).group_by(Batch.manifest_status))
    htr = await session.exec(select(Batch.htr_status, func.count()).group_by(Batch.htr_status))
    return StatusCounts(
        by_manifest_status={str(status) if status else "unknown": n for status, n in manifest.all()},
        by_htr_status={str(status) if status else "unknown": n for status, n in htr.all()},
    )


async def accessible_summary(session: AsyncSession) -> BatchAccessibleSummary:
    """Summary across batches with manifest_status='ok'."""
    row = (
        await session.exec(
            select(
                func.count(),
                func.coalesce(func.sum(Batch.page_count), 0),
                func.coalesce(func.sum(Batch.cached_pages), 0),
                func.coalesce(func.sum(Batch.transcribed_pages), 0),
            ).where(Batch.manifest_status == ManifestStatus.OK)
        )
    ).one()
    return BatchAccessibleSummary(
        batches=int(row[0] or 0),
        expected=int(row[1] or 0),
        cached=int(row[2] or 0),
        transcribed=int(row[3] or 0),
    )


async def latest_sync_timestamp(session: AsyncSession) -> str | None:
    result = await session.exec(select(func.max(Batch.last_synced_at)))
    return result.first()


async def chunks_summary(session: AsyncSession) -> list[Chunk]:
    """Per-chunk aggregates for the dashboard.

    Implemented as three small queries — sqlmodel's typed `select()` overload
    set caps at 4 columns, so one 7-column aggregate doesn't fit. Same result,
    one extra round-trip on a dashboard read.
    """
    chunk_id = col(Batch.chunk_id)
    rows = (
        await session.exec(
            select(chunk_id, func.max(Batch.chunk_total), func.count(), func.coalesce(func.sum(Batch.page_count), 0))
            .where(chunk_id.is_not(None))
            .group_by(chunk_id)
            .order_by(chunk_id)
        )
    ).all()
    cached_rows = (
        await session.exec(
            select(chunk_id, func.coalesce(func.sum(Batch.cached_pages), 0), func.coalesce(func.sum(Batch.transcribed_pages), 0))
            .where(chunk_id.is_not(None))
            .group_by(chunk_id)
        )
    ).all()
    cached_by_chunk = {cid: (c, t) for cid, c, t in cached_rows}
    done_rows = (
        await session.exec(select(chunk_id, func.count()).where(chunk_id.is_not(None), col(Batch.htr_status) == HtrStatus.DONE).group_by(chunk_id))
    ).all()
    done_by_chunk = dict(done_rows)
    return [
        Chunk(
            chunk_id=int(cid or 0),
            chunk_total=int(ctotal or 0),
            batches=int(n),
            expected_pages=int(expected or 0),
            cached_pages=int(cached_by_chunk.get(cid, (0, 0))[0] or 0),
            transcribed_pages=int(cached_by_chunk.get(cid, (0, 0))[1] or 0),
            done_batches=int(done_by_chunk.get(cid, 0)),
        )
        for cid, ctotal, n, expected in rows
    ]


async def prefetch_pending_chunk_ids(session: AsyncSession) -> list[int]:
    """Chunks where any batch still has cached_pages < page_count."""
    chunk_id = col(Batch.chunk_id)
    rows = await session.exec(
        select(Batch.chunk_id)
        .where(
            chunk_id.is_not(None),
            col(Batch.manifest_status) == ManifestStatus.OK,
            func.coalesce(Batch.cached_pages, 0) < func.coalesce(Batch.page_count, 0),
        )
        .group_by(chunk_id)
        .order_by(chunk_id)
    )
    return [r for r in rows.all() if r is not None]


async def chunks_with_progress(session: AsyncSession) -> list[ChunkProgress]:
    """Per-chunk page totals — used by orchestrator to decide HTR-readiness."""
    chunk_id = col(Batch.chunk_id)
    rows = await session.exec(
        select(
            Batch.chunk_id,
            func.coalesce(func.sum(Batch.page_count), 0),
            func.coalesce(func.sum(Batch.cached_pages), 0),
            func.coalesce(func.sum(Batch.transcribed_pages), 0),
        )
        .where(chunk_id.is_not(None), col(Batch.manifest_status) == ManifestStatus.OK)
        .group_by(chunk_id)
        .order_by(chunk_id)
    )
    return [
        ChunkProgress(
            chunk_id=int(cid or 0),
            expected_pages=int(e or 0),
            cached_pages=int(c or 0),
            transcribed_pages=int(t or 0),
        )
        for cid, e, c, t in rows.all()
    ]


def _tier_predicate(tier: BrowseTier) -> ColumnElement[bool] | bool:
    if tier == BrowseTier.TRANSCRIBED:
        return func.coalesce(Batch.transcribed_pages, 0) > 0
    if tier == BrowseTier.CACHED:
        return func.coalesce(Batch.cached_pages, 0) > 0
    return True  # LISTED


async def count_at_tier(session: AsyncSession, tier: BrowseTier) -> int:
    row = await session.exec(select(func.count()).where(_tier_predicate(tier)))
    return int(row.one() or 0)


async def browse_at_tier(session: AsyncSession, tier: BrowseTier, limit: int, offset: int) -> list[BrowseRow]:
    """Page of batches at the given tier (listed / cached / transcribed)."""
    rows = await session.exec(
        select(
            Batch.batch_id,
            func.coalesce(Batch.cached_pages, 0),
            func.coalesce(Batch.transcribed_pages, 0),
        )
        .where(_tier_predicate(tier))
        .order_by(Batch.batch_id)
        .limit(limit)
        .offset(offset)
    )
    return [BrowseRow(batch_id=bid, cached_pages=int(c), transcribed_pages=int(t)) for bid, c, t in rows.all()]
