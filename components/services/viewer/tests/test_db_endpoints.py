"""Integration tests for DB-touching endpoints.

Each test boots a fresh app against a seeded sqlite and hits the live router
via `TestClient`. Lance + Ray + S3 stay unconfigured so the lifespan skips
them; tests assert only what the DB layer produces.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

from viewer.models.batch import Batch
from viewer.models.enums import HtrStatus, ManifestStatus


# Fixtures kept small + deterministic so assertions are exact.
# Tuple order: (batch_id, htr_status, page_count, cached_pages, transcribed_pages, chunk_id)
_FIXTURES: tuple[tuple[str, HtrStatus, int, int, int, int], ...] = (
    ("DONE001", HtrStatus.DONE, 10, 10, 10, 0),
    ("DONE002", HtrStatus.DONE, 20, 20, 20, 0),
    ("CACHED001", HtrStatus.CACHED, 30, 30, 0, 1),
    ("PARTIAL001", HtrStatus.PARTIAL, 40, 20, 0, 1),
    ("PENDING001", HtrStatus.PENDING, 50, 0, 0, 1),
)


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    """Sqlite file seeded with one batch per htr_status, all manifest_status=ok."""
    db = tmp_path / "batches.db"
    engine = create_engine(f"sqlite:///{db}")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        for bid, htr, pages, cached, transcribed, chunk in _FIXTURES:
            s.add(
                Batch(
                    batch_id=bid,
                    htr_status=htr,
                    manifest_status=ManifestStatus.OK,
                    page_count=pages,
                    cached_pages=cached,
                    transcribed_pages=transcribed,
                    chunk_id=chunk,
                    chunk_total=2,
                    last_synced_at="2026-01-01T00:00:00+00:00",
                )
            )
        s.commit()
    engine.dispose()
    return db


@pytest.fixture
def client(seeded_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[TestClient]:
    """App pointed at the seeded sqlite. Ray dashboard is forced to a closed port
    so the lifespan's Ray probe fails fast instead of touching the local cluster."""
    monkeypatch.setenv("RASK_VIEWER_INPUT", str(tmp_path / "in"))
    monkeypatch.setenv("RASK_VIEWER_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("RASK_BATCHES_DB", str(seeded_db))
    monkeypatch.setenv("RAY_DASHBOARD_URL", "http://127.0.0.1:1")
    monkeypatch.delenv("HCP_ENDPOINT", raising=False)
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()

    from viewer.main import create_app

    with TestClient(create_app()) as c:
        yield c


def test_list_batches_returns_summary_and_rows(client: TestClient) -> None:
    resp = client.get("/api/v1/batches/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["total_batches"] == 5
    assert body["summary"]["by_htr_status"] == {"done": 2, "cached": 1, "partial": 1, "pending": 1}
    assert body["summary"]["by_manifest_status"] == {"ok": 5}
    assert body["summary"]["accessible"] == {
        "batches": 5,
        "expected": 150,
        "cached": 80,
        "transcribed": 30,
    }
    assert {b["batch_id"] for b in body["batches"]} == {bid for bid, *_ in _FIXTURES}


def test_get_batch_by_id_returns_full_row(client: TestClient) -> None:
    resp = client.get("/api/v1/batches/DONE001")
    assert resp.status_code == 200
    body = resp.json()
    assert body["batch_id"] == "DONE001"
    assert body["htr_status"] == "done"
    assert body["cached_pages"] == 10
    assert body["transcribed_pages"] == 10


def test_get_batch_unknown_returns_404_problem(client: TestClient) -> None:
    resp = client.get("/api/v1/batches/UNKNOWN_ID")
    assert resp.status_code == 404
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["status"] == 404
    assert body["title"] == "Not Found"
    assert "UNKNOWN_ID" in body["detail"]


def test_random_batch_returns_member_of_status(client: TestClient) -> None:
    resp = client.get("/api/v1/batches/random?status=done")
    assert resp.status_code == 200
    body = resp.json()
    assert body["batch_id"] in {"DONE001", "DONE002"}
    assert body["status"] == "done"


def test_random_batch_with_no_matches_returns_404(client: TestClient) -> None:
    """`verification_failed` is a valid HtrStatus but no fixture row has it."""
    resp = client.get("/api/v1/batches/random?status=verification_failed")
    assert resp.status_code == 404
    body = resp.json()
    assert body["status"] == 404
    assert "verification_failed" in body["detail"]


def test_list_chunks_aggregates_per_chunk(client: TestClient) -> None:
    resp = client.get("/api/v1/chunks/")
    assert resp.status_code == 200
    chunks = {c["chunk_id"]: c for c in resp.json()["chunks"]}

    assert chunks[0]["batches"] == 2
    assert chunks[0]["expected_pages"] == 30
    assert chunks[0]["cached_pages"] == 30
    assert chunks[0]["transcribed_pages"] == 30
    assert chunks[0]["done_batches"] == 2

    assert chunks[1]["batches"] == 3
    assert chunks[1]["expected_pages"] == 120
    assert chunks[1]["cached_pages"] == 50
    assert chunks[1]["transcribed_pages"] == 0
    assert chunks[1]["done_batches"] == 0


def test_catalog_browse_returns_db_total_with_lance_disabled(client: TestClient) -> None:
    """Lance is unconfigured, so hits is empty — but `total` is computed from the DB."""
    resp = client.get("/api/v1/catalog/browse?tier=cached&limit=10&offset=0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["tier"] == "cached"
    assert body["hits"] == []
    # cached tier = batches with cached_pages > 0 (DONE001/002, CACHED001, PARTIAL001)
    assert body["total"] == 4


def test_catalog_browse_transcribed_tier(client: TestClient) -> None:
    resp = client.get("/api/v1/catalog/browse?tier=transcribed&limit=10&offset=0")
    assert resp.status_code == 200
    body = resp.json()
    # transcribed tier = batches with transcribed_pages > 0 (DONE001/002)
    assert body["total"] == 2
