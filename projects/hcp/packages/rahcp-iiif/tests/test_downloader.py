"""Tests for IIIF bulk downloader."""

from pathlib import Path

import httpx
import pytest
import respx

from rahcp_iiif.downloader import download_batch
from rahcp_tracker import SqliteTracker, TransferStatus

pytestmark = pytest.mark.asyncio

BASE = "https://test-iiif.example.com"

MANIFEST = {
    "items": [
        {"id": f"https://iiif.example.com/arkis!BATCH001_0000{i}/canvas"}
        for i in range(1, 4)
    ]
}


def _mock_manifest_and_images():
    """Set up respx mocks for manifest + 3 image downloads."""
    respx.get(f"{BASE}/arkis!BATCH001/manifest").mock(
        return_value=httpx.Response(200, json=MANIFEST)
    )
    for i in range(1, 4):
        respx.get(f"{BASE}/arkis!BATCH001_0000{i}/full/max/0/default.jpg").mock(
            return_value=httpx.Response(200, content=b"\xff\xd8fake-jpg-data\xff\xd9")
        )


@respx.mock
async def test_download_batch_basic(tmp_path: Path):
    _mock_manifest_and_images()
    tracker = SqliteTracker(tmp_path / "tracker.db")

    stats = await download_batch(
        "BATCH001",
        tmp_path / "output",
        tracker,
        base_url=BASE,
        workers=2,
    )

    assert stats.ok == 3
    assert stats.errors == 0
    assert stats.skipped == 0
    assert (tmp_path / "output" / "BATCH001" / "BATCH001_00001.jpg").exists()
    assert (tmp_path / "output" / "BATCH001" / "BATCH001_00002.jpg").exists()
    assert (tmp_path / "output" / "BATCH001" / "BATCH001_00003.jpg").exists()
    tracker.close()


@respx.mock
async def test_download_batch_skips_done(tmp_path: Path):
    _mock_manifest_and_images()
    tracker = SqliteTracker(tmp_path / "tracker.db")
    tracker.mark("BATCH001/BATCH001_00001.jpg", 100, TransferStatus.done)
    tracker.flush()

    stats = await download_batch(
        "BATCH001",
        tmp_path / "output",
        tracker,
        base_url=BASE,
        workers=1,
    )

    assert stats.ok == 2
    assert stats.skipped == 1
    tracker.close()


@respx.mock
async def test_download_batch_records_errors(tmp_path: Path):
    respx.get(f"{BASE}/arkis!BATCH001/manifest").mock(
        return_value=httpx.Response(200, json=MANIFEST)
    )
    for i in range(1, 4):
        respx.get(f"{BASE}/arkis!BATCH001_0000{i}/full/max/0/default.jpg").mock(
            return_value=httpx.Response(500)
        )

    tracker = SqliteTracker(tmp_path / "tracker.db")
    errors_seen = []

    stats = await download_batch(
        "BATCH001",
        tmp_path / "output",
        tracker,
        base_url=BASE,
        workers=1,
        on_error=lambda key, exc: errors_seen.append(key),
    )

    assert stats.errors == 3
    assert stats.ok == 0
    assert len(errors_seen) == 3
    tracker.close()


@respx.mock
async def test_download_batch_max_images(tmp_path: Path):
    _mock_manifest_and_images()
    tracker = SqliteTracker(tmp_path / "tracker.db")

    stats = await download_batch(
        "BATCH001",
        tmp_path / "output",
        tracker,
        base_url=BASE,
        workers=1,
        max_images=2,
    )

    assert stats.ok == 2
    tracker.close()


@respx.mock
async def test_download_batch_with_validation(tmp_path: Path):
    _mock_manifest_and_images()
    tracker = SqliteTracker(tmp_path / "tracker.db")

    def fake_validator(path: Path) -> None:
        if "00002" in path.name:
            raise ValueError("corrupt image")

    stats = await download_batch(
        "BATCH001",
        tmp_path / "output",
        tracker,
        base_url=BASE,
        workers=1,
        validate_file=fake_validator,
    )

    assert stats.ok == 2
    assert stats.errors == 1
    # Failed file should be cleaned up
    assert not (tmp_path / "output" / "BATCH001" / "BATCH001_00002.jpg").exists()
    tracker.close()


@respx.mock
async def test_download_batch_progress_callback(tmp_path: Path):
    _mock_manifest_and_images()
    tracker = SqliteTracker(tmp_path / "tracker.db")
    progress_calls = []

    stats = await download_batch(
        "BATCH001",
        tmp_path / "output",
        tracker,
        base_url=BASE,
        workers=1,
        progress_interval=0.0,
        on_progress=lambda s: progress_calls.append(s.done),
    )

    assert stats.ok == 3
    assert len(progress_calls) > 0
    tracker.close()
