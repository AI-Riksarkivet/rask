"""register_volume — index an S3 prefix into a one-chunk batches row (moto-backed)."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import boto3
import pytest
import pytest_asyncio
from moto import mock_aws
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from core.models.batch import Batch
from core.models.enums import ManifestStatus
from core.services.registration import register_volume
from service_kit.exceptions import ValidationError


@pytest.fixture
def s3_client() -> Iterator[boto3.client]:  # type: ignore[type-arg]
    with mock_aws():
        c = boto3.client("s3", region_name="us-east-1")
        c.create_bucket(Bucket="images-batch")
        for key in ("VOL_A/00001.jpg", "VOL_A/00002.jpg", "VOL_A/notes.txt"):
            c.put_object(Bucket="images-batch", Key=key, Body=b"x")
        yield c


@pytest_asyncio.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'b.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_register_counts_images_only(session: AsyncSession, s3_client: boto3.client) -> None:  # type: ignore[type-arg]
    batch = await register_volume(session, s3_client, input_bucket="images-batch", volume_id="VOL_A")
    assert batch.batch_id == "VOL_A"
    assert batch.page_count == 2  # notes.txt is not an image
    assert batch.cached_pages == 2
    assert batch.manifest_status == ManifestStatus.OK
    assert batch.chunk_total == 1
    assert batch.chunk_id == 1


@pytest.mark.asyncio
async def test_register_empty_prefix_raises(session: AsyncSession, s3_client: boto3.client) -> None:  # type: ignore[type-arg]
    with pytest.raises(ValidationError):
        await register_volume(session, s3_client, input_bucket="images-batch", volume_id="MISSING")


@pytest.mark.asyncio
async def test_register_is_idempotent_keeps_chunk_id(session: AsyncSession, s3_client: boto3.client) -> None:  # type: ignore[type-arg]
    first = await register_volume(session, s3_client, input_bucket="images-batch", volume_id="VOL_A")
    s3_client.put_object(Bucket="images-batch", Key="VOL_A/00003.jpg", Body=b"x")
    again = await register_volume(session, s3_client, input_bucket="images-batch", volume_id="VOL_A")
    assert again.chunk_id == first.chunk_id  # preserved
    assert again.page_count == 3  # refreshed
