"""Tests for TransferTracker — SQLite-backed transfer state."""

from pathlib import Path

from rahcp_tracker import TrackerProtocol, TransferStatus, TransferTracker


def test_mark_and_done_keys(tmp_path: Path):
    tracker = TransferTracker(tmp_path / "test.db")
    tracker.mark("a.jpg", 100, TransferStatus.done)
    tracker.mark("b.jpg", 200, TransferStatus.error, "timeout")
    tracker.mark("c.jpg", 300, TransferStatus.done)
    tracker.flush()

    done = tracker.done_keys()
    assert done == {"a.jpg", "c.jpg"}
    tracker.close()


def test_error_entries(tmp_path: Path):
    tracker = TransferTracker(tmp_path / "test.db")
    tracker.mark("ok.jpg", 100, TransferStatus.done)
    tracker.mark("fail1.jpg", 200, TransferStatus.error, "timeout")
    tracker.mark("fail2.jpg", 300, TransferStatus.error, "ssl error")
    tracker.flush()

    errors = tracker.error_entries()
    assert len(errors) == 2
    keys = {k for k, _ in errors}
    assert keys == {"fail1.jpg", "fail2.jpg"}
    tracker.close()


def test_mark_updates_existing(tmp_path: Path):
    tracker = TransferTracker(tmp_path / "test.db")
    tracker.mark("a.jpg", 100, TransferStatus.error, "first failure")
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
    tracker.mark("c.jpg", 300, TransferStatus.error, "fail")

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
