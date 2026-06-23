"""Tests for TransferTracker — SQLite-backed transfer state."""

from pathlib import Path

from tracker import TrackerProtocol, TransferStatus, TransferTracker


def test_mark_and_done_keys(tmp_path: Path):
    tracker = TransferTracker(tmp_path / "test.db")
    tracker.mark("a.jpg", 100, TransferStatus.done)
    tracker.mark("b.jpg", 200, TransferStatus.error, error="timeout")
    tracker.mark("c.jpg", 300, TransferStatus.done)
    tracker.flush()

    done = tracker.done_keys()
    assert done == {"a.jpg", "c.jpg"}
    tracker.close()


def test_error_entries(tmp_path: Path):
    tracker = TransferTracker(tmp_path / "test.db")
    tracker.mark("ok.jpg", 100, TransferStatus.done)
    tracker.mark("fail1.jpg", 200, TransferStatus.error, error="timeout")
    tracker.mark("fail2.jpg", 300, TransferStatus.error, error="ssl error")
    tracker.flush()

    errors = tracker.error_entries()
    assert len(errors) == 2
    keys = {k for k, _ in errors}
    assert keys == {"fail1.jpg", "fail2.jpg"}
    tracker.close()


def test_error_details_surfaces_reason(tmp_path: Path):
    tracker = TransferTracker(tmp_path / "test.db")
    tracker.mark("ok.jpg", 100, TransferStatus.done)
    tracker.mark("bad.jpg", 200, TransferStatus.error, error="validate: Missing JPEG EOI marker")
    tracker.flush()

    details = tracker.error_details()
    assert details == [("bad.jpg", 200, "validate: Missing JPEG EOI marker")]
    tracker.close()


def test_delete_removes_entry(tmp_path: Path):
    tracker = TransferTracker(tmp_path / "test.db")
    tracker.mark("ok.jpg", 100, TransferStatus.done)
    tracker.mark("bad/__manifest__", 0, TransferStatus.error, error="manifest: timeout")

    tracker.delete("bad/__manifest__")

    assert tracker.error_details() == []
    assert tracker.summary() == {"pending": 0, "done": 1, "error": 0}
    tracker.close()


def test_delete_nonexistent_key_is_noop(tmp_path: Path):
    tracker = TransferTracker(tmp_path / "test.db")
    tracker.mark("a.jpg", 100, TransferStatus.done)

    tracker.delete("never-marked.jpg")

    assert tracker.summary() == {"pending": 0, "done": 1, "error": 0}
    assert "a.jpg" in tracker.done_keys()
    tracker.close()


def test_mark_updates_existing(tmp_path: Path):
    tracker = TransferTracker(tmp_path / "test.db")
    tracker.mark("a.jpg", 100, TransferStatus.error, error="first failure")
    tracker.flush()

    assert len(tracker.error_entries()) == 1

    tracker.mark("a.jpg", 100, TransferStatus.done)
    tracker.flush()

    assert len(tracker.error_entries()) == 0
    assert "a.jpg" in tracker.done_keys()
    tracker.close()


def test_auto_flush_on_buffer_full(tmp_path: Path):
    tracker = TransferTracker(tmp_path / "test.db", flush_every=5)
    for i in range(5):
        tracker.mark(f"file_{i}.jpg", 100, TransferStatus.done)

    # Buffer should have auto-flushed at 5
    done = tracker.done_keys()
    assert len(done) == 5
    tracker.close()


def test_summary(tmp_path: Path):
    tracker = TransferTracker(tmp_path / "test.db")
    tracker.mark("a.jpg", 100, TransferStatus.done)
    tracker.mark("b.jpg", 200, TransferStatus.done)
    tracker.mark("c.jpg", 300, TransferStatus.error, error="fail")

    result = tracker.summary()
    assert result == {"pending": 0, "done": 2, "error": 1}
    tracker.close()


def test_reopen_persists(tmp_path: Path):
    db_path = tmp_path / "test.db"

    tracker = TransferTracker(db_path)
    tracker.mark("a.jpg", 100, TransferStatus.done)
    tracker.close()

    tracker2 = TransferTracker(db_path)
    assert "a.jpg" in tracker2.done_keys()
    tracker2.close()


def test_transfer_tracker_satisfies_protocol():
    assert issubclass(TransferTracker, TrackerProtocol)


def test_concurrent_threaded_access_is_safe(tmp_path: Path):
    """Concurrent delete/mark/queries from worker threads (the asyncio.to_thread
    pattern used by sentinels) must serialize on the tracker's lock — unguarded
    shared-Session access segfaulted the process."""
    from concurrent.futures import Future, ThreadPoolExecutor

    tracker = TransferTracker(tmp_path / "test.db", flush_every=10)
    for i in range(64):
        tracker.mark(f"b{i}/__manifest__", 0, TransferStatus.error, error="manifest: 403")
    tracker.flush()

    def delete_sentinel(i: int) -> None:
        tracker.delete(f"b{i}/__manifest__")

    def mark_done(i: int) -> None:
        tracker.mark(f"b{i}/img.jpg", 100, TransferStatus.done)

    with ThreadPoolExecutor(max_workers=16) as pool:
        futures: list[Future[object]] = [pool.submit(delete_sentinel, i) for i in range(64)]
        futures += [pool.submit(mark_done, i) for i in range(64)]
        futures += [pool.submit(tracker.done_keys) for _ in range(16)]
        for f in futures:
            f.result()  # surfaces any exception from the workers

    assert tracker.error_details() == []
    assert tracker.summary() == {"pending": 0, "done": 64, "error": 0}
    tracker.close()
