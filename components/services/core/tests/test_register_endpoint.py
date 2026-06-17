"""POST /batches/{id}/register — real register_volume over moto S3 + sqlite.

Mirrors test_db_endpoints.py: the schema is created up front with a sync engine
(the app uses Alembic at runtime, not create_all), then the real app boots inside
mock_aws() so the lifespan's S3 client is mocked and register_volume lists the
seeded objects for real.
"""

import asyncio
from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlmodel import SQLModel


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    db = tmp_path / "b.db"
    db_url = f"sqlite+aiosqlite:///{db}"

    # Set env vars early so Settings() picks them up
    monkeypatch.setenv("RASK_VIEWER_INPUT", "s3://images-batch")
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", "s3://images-batch-alto")
    monkeypatch.setenv("RASK_CACHE_BUCKET", "images-batch")
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("RAY_DASHBOARD_URL", "http://127.0.0.1:1")

    with mock_aws():
        # Create S3 bucket and seed objects with a mocked boto3 client
        c = boto3.client("s3", region_name="us-east-1")
        c.create_bucket(Bucket="images-batch")
        c.put_object(Bucket="images-batch", Key="VOL_A/00001.jpg", Body=b"x")
        c.put_object(Bucket="images-batch", Key="VOL_A/00002.jpg", Body=b"x")

        # Patch _build_s3 to return the mocked client instead of trying to connect
        # to HCP_ENDPOINT. This keeps register_volume real while using moto's mocked S3.
        def mock_build_s3(_settings):  # type: ignore[misc]
            return c

        monkeypatch.setattr("core.lifespan._build_s3", mock_build_s3)

        from core.main import create_app

        app = create_app()
        with TestClient(app) as tc:
            # Create database tables after app startup (when settings are available)
            async def create_tables() -> None:
                from core.db import make_engine

                engine = make_engine(tc.app.state.settings)
                async with engine.begin() as conn:
                    await conn.run_sync(SQLModel.metadata.create_all)
                await engine.dispose()

            asyncio.run(create_tables())
            yield tc


def test_register_endpoint_creates_batch(client: TestClient) -> None:
    resp = client.post("/api/v1/batches/VOL_A/register")
    assert resp.status_code == 201
    body = resp.json()
    assert body["batch_id"] == "VOL_A"
    assert body["page_count"] == 2
    assert body["manifest_status"] == "ok"
    assert client.get("/api/v1/batches/VOL_A").status_code == 200
