"""S3 → batches table reconciliation.

Lists each bucket once, groups keys by `<batch_id>/...` prefix, then updates
row state (cached_pages, transcribed_pages, htr_status, timestamps). Async
on top of sync boto3 via `anyio.to_thread`.

Uses raw SQL (no SQLModel class import) so this module is independent of the
viewer's schema layer. The column set matches what `scripts/build_batches_db.py`
creates today; schema ownership will move into this package when Alembic lands.
"""

import logging
from collections import defaultdict
from datetime import UTC, datetime

import anyio
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from storage import iter_keys, s3_client


log = logging.getLogger(__name__)


_HTR_STATUS_PENDING = "pending"
_HTR_STATUS_CACHED = "cached"
_HTR_STATUS_PARTIAL = "partial"
_HTR_STATUS_DONE = "done"


class SyncResult(BaseModel):
    cached_total: int
    transcribed_total: int
    rows_updated: int
    by_status: dict[str, int]


def _classify(expected: int | None, cached: int, transcribed: int) -> str:
    if expected is None:
        if transcribed > 0:
            return _HTR_STATUS_PARTIAL
        if cached > 0:
            return _HTR_STATUS_CACHED
        return _HTR_STATUS_PENDING
    if transcribed >= expected:
        return _HTR_STATUS_DONE
    if transcribed > 0:
        return _HTR_STATUS_PARTIAL
    if cached >= expected:
        return _HTR_STATUS_CACHED
    if cached > 0:
        return _HTR_STATUS_PARTIAL
    return _HTR_STATUS_PENDING


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

    cached = await anyio.to_thread.run_sync(_count_per_batch, client, cache_bucket, ".jpg")
    transcribed = await anyio.to_thread.run_sync(_count_per_batch, client, output_bucket, ".xml")

    now = datetime.now(UTC).isoformat(timespec="seconds")
    rows = (await session.execute(text("SELECT batch_id, page_count, started_at, finished_at FROM batches"))).all()

    updates: list[dict[str, object]] = []
    by_status: dict[str, int] = defaultdict(int)
    for batch_id, expected, started_at, finished_at in rows:
        c = cached.get(batch_id, 0)
        t = transcribed.get(batch_id, 0)
        status = _classify(expected, c, t)
        updates.append(
            {
                "cached": c,
                "transcribed": t,
                "status": status,
                "started": started_at or (now if c > 0 else None),
                "finished": finished_at or (now if status == _HTR_STATUS_DONE else None),
                "now": now,
                "bid": batch_id,
            }
        )
        by_status[status] += 1

    if updates:
        await session.execute(
            text(
                """UPDATE batches
                      SET cached_pages = :cached,
                          transcribed_pages = :transcribed,
                          htr_status = :status,
                          started_at = :started,
                          finished_at = :finished,
                          last_synced_at = :now
                    WHERE batch_id = :bid"""
            ),
            updates,
        )
        await session.commit()

    return SyncResult(
        cached_total=sum(cached.values()),
        transcribed_total=sum(transcribed.values()),
        rows_updated=len(updates),
        by_status=dict(by_status),
    )
