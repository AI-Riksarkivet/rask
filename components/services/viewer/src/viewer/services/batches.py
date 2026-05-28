"""Business logic for the batches resource.

Composes repository calls and maps ORM rows to the public schema. The S3
reconciliation lives in `services/sync.py`; RayJob submission in
`services/submission.py`; both are orchestrated by `services/orchestrator/loop.py`.
"""

from sqlmodel.ext.asyncio.session import AsyncSession

from viewer.core.exceptions import NotFoundError
from viewer.models.batch import BatchPublic
from viewer.models.enums import HtrStatus
from viewer.repositories import batch as batch_repo
from viewer.schemas.batch import (
    BatchAccessibleSummary,
    BatchListResponse,
    BatchSummary,
)


async def list_batches(session: AsyncSession) -> BatchListResponse:
    rows = await batch_repo.list_all(session)
    by_manifest, by_htr = await batch_repo.status_counts(session)
    n, expected, cached, transcribed = await batch_repo.accessible_summary(session)
    generated_at = await batch_repo.latest_sync_timestamp(session)
    return BatchListResponse(
        generated_at=generated_at,
        summary=BatchSummary(
            total_batches=len(rows),
            accessible=BatchAccessibleSummary(batches=n, expected=expected, cached=cached, transcribed=transcribed),
            by_manifest_status=by_manifest,
            by_htr_status=by_htr,
        ),
        batches=[BatchPublic.model_validate(r) for r in rows],
    )


async def get_batch(session: AsyncSession, batch_id: str) -> BatchPublic:
    row = await batch_repo.get(session, batch_id)
    if row is None:
        raise NotFoundError(f"unknown batch_id: {batch_id}")
    return BatchPublic.model_validate(row)


async def random_batch(session: AsyncSession, status: HtrStatus) -> str:
    row = await batch_repo.random_by_status(session, status)
    if row is None:
        raise NotFoundError(f"no batches with htr_status={status!r}")
    return row.batch_id


async def local_batch_status(session: AsyncSession, bild_ids: list[str]) -> tuple[set[str], set[str], set[str]]:
    """For a list of bild_ids, return (listed, cached, transcribed) sets.

    Three tiers, each strictly nested:
      listed       = batch_id exists in batches.db
      cached       = also has cached_pages > 0
      transcribed  = also has transcribed_pages > 0
    """
    if not bild_ids:
        return set(), set(), set()
    rows = await batch_repo.by_ids(session, bild_ids)
    listed = {r.batch_id for r in rows}
    cached = {r.batch_id for r in rows if r.cached_pages > 0}
    transcribed = {r.batch_id for r in rows if r.transcribed_pages > 0}
    return listed, cached, transcribed
