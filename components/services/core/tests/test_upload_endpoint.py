"""POST /batches/{id}/upload — multipart upload writes to the input bucket + registers.

Mirrors test_register_endpoint.py: schema via a sync engine, lifespan S3 builder
patched to a moto client. The endpoint itself does the writes (nothing pre-seeded).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy import create_engine
from sqlmodel import SQLModel

from core.models.batch import Batch  # noqa: F401 - registers table with SQLModel.metadata


if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[TestClient, S3Client]]:
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
    # Prevent load_dotenv() in create_app() from pulling a postgres DATABASE_URL
    # from a local .env file — sqlite via RASK_BATCHES_DB must win.
    monkeypatch.setenv("DATABASE_URL", "")

    with mock_aws():
        c = boto3.client("s3", region_name="us-east-1")
        c.create_bucket(Bucket="images-batch")
        monkeypatch.setattr("core.lifespan._build_s3", lambda _settings: c)

        from core.main import create_app

        with TestClient(create_app()) as tc:
            yield tc, c


def _keys(s3: S3Client, prefix: str = "") -> list[str]:
    resp = s3.list_objects_v2(Bucket="images-batch", Prefix=prefix)
    return sorted(o["Key"] for o in resp.get("Contents", []))


def test_upload_creates_and_registers(client: tuple[TestClient, S3Client]) -> None:
    tc, s3 = client
    files = [
        ("files", ("page_0001.jpg", b"img1", "image/jpeg")),
        ("files", ("page_0002.jpg", b"img2", "image/jpeg")),
        ("files", ("page_0003.png", b"img3", "image/png")),
    ]
    resp = tc.post("/api/v1/batches/myvol/upload", files=files)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["batch_id"] == "myvol"
    assert body["page_count"] == 3
    assert body["manifest_status"] == "ok"
    assert _keys(s3, "myvol/") == ["myvol/page_0001.jpg", "myvol/page_0002.jpg", "myvol/page_0003.png"]


def test_upload_skips_non_images(client: tuple[TestClient, S3Client]) -> None:
    tc, s3 = client
    files = [
        ("files", ("a.jpg", b"img", "image/jpeg")),
        ("files", ("notes.txt", b"hi", "text/plain")),
    ]
    resp = tc.post("/api/v1/batches/mixed/upload", files=files)
    assert resp.status_code == 201, resp.text
    assert resp.json()["page_count"] == 1
    assert _keys(s3, "mixed/") == ["mixed/a.jpg"]  # txt not written


def test_upload_all_non_images_422(client: tuple[TestClient, S3Client]) -> None:
    tc, _ = client
    resp = tc.post("/api/v1/batches/novol/upload", files=[("files", ("notes.txt", b"hi", "text/plain"))])
    assert resp.status_code == 422


def test_upload_bad_volume_id_422(client: tuple[TestClient, S3Client]) -> None:
    tc, _ = client
    # dot is disallowed by the volume-id charset
    resp = tc.post("/api/v1/batches/bad.id/upload", files=[("files", ("a.jpg", b"x", "image/jpeg"))])
    assert resp.status_code == 422


def test_upload_filename_is_basenamed(client: tuple[TestClient, S3Client]) -> None:
    tc, s3 = client
    resp = tc.post("/api/v1/batches/safe/upload", files=[("files", ("../evil.jpg", b"x", "image/jpeg"))])
    assert resp.status_code == 201, resp.text
    # stored as basename under the volume prefix; no traversal out of safe/
    assert _keys(s3) == ["safe/evil.jpg"]


def test_upload_reregisters_idempotently(client: tuple[TestClient, S3Client]) -> None:
    tc, s3 = client
    r1 = tc.post("/api/v1/batches/idem/upload", files=[("files", ("a.jpg", b"x", "image/jpeg"))])
    assert r1.status_code == 201, r1.text
    first = r1.json()
    assert first["page_count"] == 1
    # re-upload the same volume with an additional image
    r2 = tc.post(
        "/api/v1/batches/idem/upload",
        files=[
            ("files", ("a.jpg", b"x", "image/jpeg")),
            ("files", ("b.jpg", b"y", "image/jpeg")),
        ],
    )
    assert r2.status_code == 201, r2.text
    second = r2.json()
    assert second["page_count"] == 2  # count refreshed
    assert second["chunk_id"] == first["chunk_id"]  # chunk_id preserved across re-register
    assert _keys(s3, "idem/") == ["idem/a.jpg", "idem/b.jpg"]
