"""HTTP-level test for POST /api/v1/chunks/{id}/submit.

Covers the body-validation contract: an unknown pipeline name → 422
(SubmitRequest field_validator), before the endpoint body runs. Concurrency is
delegated to Ray (no slot cap / 409), so the allow-concurrent and
two-pipelines-on-one-chunk scenarios live in test_pipelines.py.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

from core.models.batch import Batch
from core.models.enums import HtrStatus, ManifestStatus


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    db = tmp_path / "batches.db"
    engine = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(
            Batch(
                batch_id="CACHED001",
                htr_status=HtrStatus.CACHED,
                manifest_status=ManifestStatus.OK,
                page_count=30,
                cached_pages=30,
                transcribed_pages=0,
                chunk_id=1,
                chunk_total=1,
                last_synced_at="2026-01-01T00:00:00+00:00",
            )
        )
        s.commit()
    engine.dispose()
    return db


@pytest.fixture
def app(seeded_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FastAPI:
    monkeypatch.setenv("RASK_VIEWER_INPUT", str(tmp_path / "in"))
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("RASK_BATCHES_DB", str(seeded_db))
    monkeypatch.setenv("RAY_DASHBOARD_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("HCP_ENDPOINT", raising=False)
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()

    from core.main import create_app

    return create_app()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def test_submit_unknown_pipeline_returns_422(client: TestClient) -> None:
    """The SubmitRequest validator rejects unregistered names before the
    endpoint body runs — so this is 422 even with Ray unreachable."""
    resp = client.post("/api/v1/chunks/1/submit", json={"pipeline": "bogus"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["status"] == 422
    assert body["title"] == "Validation Error"
    assert any(e["field"].endswith("pipeline") for e in body["errors"])
