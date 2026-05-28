"""S3 → batches table reconciliation.

Lists each bucket once, groups keys by `<batch_id>/...` prefix, then updates
row state (cached_pages, transcribed_pages, htr_status, timestamps). Async
on top of sync boto3 via `anyio.to_thread`.

Idempotent — safe to call from the lifespan orchestrator loop and from
POST /batches/sync without coordination.
"""

import logging
from collections import defaultdict
from datetime import UTC, datetime

from anyio import to_thread
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from storage import iter_keys, s3_client
from viewer.models.batch import Batch
from viewer.models.enums import HtrStatus


log = logging.getLogger(__name__)


class SyncResult(BaseModel):
    cached_total: int
    transcribed_total: int
    rows_updated: int
    by_status: dict[str, int]


def _classify(expected: int | None, cached: int, transcribed: int) -> HtrStatus:
    if expected is None:
        if transcribed > 0:
            return HtrStatus.PARTIAL
        if cached > 0:
            return HtrStatus.CACHED
        return HtrStatus.PENDING
    if transcribed >= expected:
        return HtrStatus.DONE
    if transcribed > 0:
        return HtrStatus.PARTIAL
    if cached >= expected:
        return HtrStatus.CACHED
    if cached > 0:
        return HtrStatus.PARTIAL
    return HtrStatus.PENDING


def _count_per_batch(client: object, bucket: str, suffix: str) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for key in iter_keys(client, bucket, suffix=suffix):
        batch_id, _, _ = key.partition("/")
        if batch_id:
            counts[batch_id] += 1
    return dict(counts)


async def reconcile_from_s3(
    session: AsyncSession,
    *,
    hcp_endpoint: str,
    cache_bucket: str = "images-batch",
    output_bucket: str = "images-batch-alto",
) -> SyncResult:
    """Reconcile the batches table with the actual S3 buckets. Idempotent."""
    client = s3_client(endpoint=hcp_endpoint)

    cached = await to_thread.run_sync(_count_per_batch, client, cache_bucket, ".jpg")
    transcribed = await to_thread.run_sync(_count_per_batch, client, output_bucket, ".xml")

    now = datetime.now(UTC).isoformat(timespec="seconds")
    rows = list((await session.exec(select(Batch))).all())

    by_status: dict[str, int] = defaultdict(int)
    for batch in rows:
        c = cached.get(batch.batch_id, 0)
        t = transcribed.get(batch.batch_id, 0)
        status = _classify(batch.page_count, c, t)
        batch.cached_pages = c
        batch.transcribed_pages = t
        batch.htr_status = status
        if batch.started_at is None and c > 0:
            batch.started_at = now
        if batch.finished_at is None and status is HtrStatus.DONE:
            batch.finished_at = now
        batch.last_synced_at = now
        session.add(batch)
        by_status[status.value] += 1

    if rows:
        await session.commit()

    return SyncResult(
        cached_total=sum(cached.values()),
        transcribed_total=sum(transcribed.values()),
        rows_updated=len(rows),
        by_status=dict(by_status),
    )
