"""POST /batches/{id}/register — real register_volume over moto S3 + sqlite.

Mirrors test_db_endpoints.py: the schema is created up front with a sync engine
(the app uses Alembic at runtime, not create_all). The lifespan's S3 builder is
patched to return the moto-backed client so the REAL register_volume lists the
seeded objects (register_volume itself is never stubbed).
"""

from collections.abc import Iterator
from pathlib import Path

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy import create_engine
from sqlmodel import SQLModel

from core.models.batch import Batch  # noqa: F401 - registers table with SQLModel.metadata


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    db = tmp_path / "b.db"
    engine = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()

    monkeypatch.setenv("RASK_VIEWER_INPUT", "s3://images-batch")
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", "s3://images-batch-alto")
    monkeypatch.setenv("RASK_CACHE_BUCKET", "images-batch")
    monkeypatch.setenv("RASK_BATCHES_DB", str(db))
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("RAY_DASHBOARD_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("HCP_ENDPOINT", "http://localhost:9000")

    with mock_aws():
        c = boto3.client("s3", region_name="us-east-1")
        c.create_bucket(Bucket="images-batch")
        c.put_object(Bucket="images-batch", Key="VOL_A/00001.jpg", Body=b"x")
        c.put_object(Bucket="images-batch", Key="VOL_A/00002.jpg", Body=b"x")

        # The app would build its S3 client against HCP_ENDPOINT; inject the moto
        # client so the real register_volume lists the seeded bucket.
        monkeypatch.setattr("core.lifespan._build_s3", lambda _settings: c)

        from core.main import create_app

        with TestClient(create_app()) as tc:
            yield tc


def test_register_endpoint_creates_batch(client: TestClient) -> None:
    resp = client.post("/api/v1/batches/VOL_A/register")
    assert resp.status_code == 201
    body = resp.json()
    assert body["batch_id"] == "VOL_A"
    assert body["page_count"] == 2
    assert body["manifest_status"] == "ok"
    assert client.get("/api/v1/batches/VOL_A").status_code == 200
