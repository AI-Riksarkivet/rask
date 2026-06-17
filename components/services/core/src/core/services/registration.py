"""Register an already-uploaded S3 prefix as a processable volume.

A "volume" is an S3 prefix under the input bucket. Registration indexes it into
the `batches` table so the existing orchestrator -> submit -> htrflow path picks
it up with no IIIF manifest. Indexing only: getting images into the bucket is a
separate concern. One volume = one chunk (chunk_total=1).
"""

from anyio import to_thread
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from core.models.batch import Batch
from core.models.enums import HtrStatus, ManifestStatus
from service_kit.exceptions import ValidationError
from storage import S3Client, S3Source


async def register_volume(session: AsyncSession, client: S3Client, *, input_bucket: str, volume_id: str) -> Batch:
    """Index `input_bucket/<volume_id>/` into a one-chunk batches row.

    `page_count`/`cached_pages` = number of images under the prefix (the images
    are already in the bucket, so they count as cached). Idempotent: re-registering
    refreshes the counts and keeps the existing `chunk_id`.
    """
    prefix = volume_id.rstrip("/") + "/"
    src = S3Source(bucket=input_bucket, prefix=prefix, client=client)
    keys = await to_thread.run_sync(lambda: list(src.keys()))  # S3Source.keys() filters to image suffixes by default
    if not keys:
        raise ValidationError(f"no images found under {input_bucket}/{prefix}")
    page_count = len(keys)

    batch = await session.get(Batch, volume_id)
    if batch is None:
        next_chunk: int = (await session.exec(select(func.coalesce(func.max(Batch.chunk_id), 0)))).one()  # type: ignore[assignment, invalid-assignment]
        batch = Batch(batch_id=volume_id, chunk_id=int(next_chunk) + 1, chunk_total=1, htr_status=HtrStatus.CACHED)
    batch.page_count = page_count
    batch.cached_pages = page_count
    batch.manifest_status = ManifestStatus.OK
    session.add(batch)
    await session.commit()
    await session.refresh(batch)
    return batch
