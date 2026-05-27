"""Data access for the batches table — async via SQLModel + AsyncSession.

Repositories return ORM rows (or scalar tuples for aggregates). Services map
ORM → API schema (`BatchPublic`) so the API contract stays decoupled from the
DB layout.
"""

from sqlalchemy import case, func
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from viewer.models.batch import Batch
from viewer.models.enums import BrowseTier, HtrStatus, ManifestStatus


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


async def status_counts(session: AsyncSession) -> tuple[dict[str, int], dict[str, int]]:
    """Returns (by_manifest_status, by_htr_status) counts."""
    manifest = await session.exec(select(Batch.manifest_status, func.count()).group_by(Batch.manifest_status))
    htr = await session.exec(select(Batch.htr_status, func.count()).group_by(Batch.htr_status))
    return (
        {str(status) if status else "unknown": n for status, n in manifest.all()},
        {str(status) if status else "unknown": n for status, n in htr.all()},
    )


async def accessible_summary(session: AsyncSession) -> tuple[int, int, int, int]:
    """Returns (batches, expected_pages, cached_pages, transcribed_pages) for manifest_status='ok'."""
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
    return int(row[0]), int(row[1]), int(row[2]), int(row[3])


async def latest_sync_timestamp(session: AsyncSession) -> str | None:
    result = await session.exec(select(func.max(Batch.last_synced_at)))
    return result.first()


async def chunks_summary(
    session: AsyncSession,
) -> list[tuple[int, int, int, int, int, int, int]]:
    """Returns per-chunk: (chunk_id, chunk_total, batches, expected, cached, transcribed, done_batches)."""
    rows = await session.exec(
        select(
            Batch.chunk_id,
            func.max(Batch.chunk_total),
            func.count(),
            func.coalesce(func.sum(Batch.page_count), 0),
            func.coalesce(func.sum(Batch.cached_pages), 0),
            func.coalesce(func.sum(Batch.transcribed_pages), 0),
            func.sum(case((Batch.htr_status == HtrStatus.DONE, 1), else_=0)),
        )
        .where(col(Batch.chunk_id).is_not(None))
        .group_by(Batch.chunk_id)
        .order_by(Batch.chunk_id)
    )
    return [
        (int(cid or 0), int(ctotal or 0), int(n), int(expected or 0), int(cached or 0), int(transcribed or 0), int(done or 0))
        for cid, ctotal, n, expected, cached, transcribed, done in rows.all()
    ]


async def prefetch_pending_chunk_ids(session: AsyncSession) -> list[int]:
    """Chunks where any batch still has cached_pages < page_count."""
    rows = await session.exec(
        select(Batch.chunk_id)
        .where(
            col(Batch.chunk_id).is_not(None),
            Batch.manifest_status == ManifestStatus.OK,
            func.coalesce(Batch.cached_pages, 0) < func.coalesce(Batch.page_count, 0),
        )
        .group_by(Batch.chunk_id)
        .order_by(Batch.chunk_id)
    )
    return [r for r in rows.all() if r is not None]


async def chunks_with_progress(session: AsyncSession) -> list[tuple[int, int, int, int]]:
    """Per-chunk totals (chunk_id, expected, cached, transcribed) — used by orchestrator."""
    rows = await session.exec(
        select(
            Batch.chunk_id,
            func.coalesce(func.sum(Batch.page_count), 0),
            func.coalesce(func.sum(Batch.cached_pages), 0),
            func.coalesce(func.sum(Batch.transcribed_pages), 0),
        )
        .where(col(Batch.chunk_id).is_not(None), Batch.manifest_status == ManifestStatus.OK)
        .group_by(Batch.chunk_id)
        .order_by(Batch.chunk_id)
    )
    return [(int(cid or 0), int(e or 0), int(c or 0), int(t or 0)) for cid, e, c, t in rows.all()]


def _tier_predicate(tier: BrowseTier):  # noqa: ANN202 — SQLAlchemy ColumnElement type is internal
    if tier == BrowseTier.TRANSCRIBED:
        return func.coalesce(Batch.transcribed_pages, 0) > 0
    if tier == BrowseTier.CACHED:
        return func.coalesce(Batch.cached_pages, 0) > 0
    return True  # LISTED


async def count_at_tier(session: AsyncSession, tier: BrowseTier) -> int:
    row = await session.exec(select(func.count()).where(_tier_predicate(tier)))
    return int(row.one() or 0)


async def browse_at_tier(session: AsyncSession, tier: BrowseTier, limit: int, offset: int) -> list[tuple[str, int, int]]:
    """Page of (batch_id, cached_pages, transcribed_pages) at the given tier."""
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
    return [(bid, int(c), int(t)) for bid, c, t in rows.all()]
