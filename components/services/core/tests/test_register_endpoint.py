"""POST /batches/{id}/register wiring — moto S3 + sqlite, via the live router."""

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from core.models.batch import Batch
from core.models.enums import HtrStatus, ManifestStatus


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    monkeypatch.setenv("RASK_VIEWER_INPUT", "s3://images-batch")
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", "s3://images-batch-alto")
    monkeypatch.setenv("RASK_CACHE_BUCKET", "images-batch")
    monkeypatch.setenv("RASK_BATCHES_DB", str(tmp_path / "b.db"))
    monkeypatch.setenv("HCP_ENDPOINT", "http://localhost:9000")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("RAY_DASHBOARD_URL", "http://127.0.0.1:1")

    mock = mock_aws()
    mock.start()
    try:
        c = boto3.client("s3", region_name="us-east-1")
        c.create_bucket(Bucket="images-batch")
        c.create_bucket(Bucket="images-batch-alto")
        c.put_object(Bucket="images-batch", Key="VOL_A/00001.jpg", Body=b"x")
        c.put_object(Bucket="images-batch", Key="VOL_A/00002.jpg", Body=b"x")
        from core.main import create_app
        from core.db import make_engine
        from sqlmodel import SQLModel

        app = create_app()

        tc = TestClient(app)
        tc.__enter__()

        # Create database tables after app startup (when settings are available)
        import asyncio
        async def create_tables():
            engine = make_engine(tc.app.state.settings)
            async with engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)
        asyncio.run(create_tables())

        yield tc
        tc.__exit__(None, None, None)
    finally:
        mock.stop()


async def mock_register_volume(session, client, *, input_bucket: str, volume_id: str):
    """Return a mock batch for testing."""
    batch = Batch(
        batch_id=volume_id,
        chunk_id=1,
        chunk_total=1,
        page_count=2,
        cached_pages=2,
        htr_status=HtrStatus.CACHED,
        manifest_status=ManifestStatus.OK,
    )
    session.add(batch)
    await session.commit()
    await session.refresh(batch)
    return batch


def test_register_endpoint_creates_batch(client: TestClient) -> None:
    with patch("core.services.registration.register_volume", side_effect=mock_register_volume):
        resp = client.post("/api/v1/batches/VOL_A/register")
    assert resp.status_code == 201
    body = resp.json()
    assert body["batch_id"] == "VOL_A"
    assert body["page_count"] == 2
    assert body["manifest_status"] == "ok"
    # now visible via the normal read path
    assert client.get("/api/v1/batches/VOL_A").status_code == 200
