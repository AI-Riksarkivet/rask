"""Retention must delete ONLY terminal jobs beyond the keep-window — and never live work.

The stakes are asymmetric: an under-deleting pruner leaves the #136 OOM in place slowly; an
over-deleting one kills RUNNING jobs. Every boundary here is mutation-checked in CI review.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from ray_kit.prune import prune_jobs


class _Client:
    def __init__(self, jobs: list[Any]) -> None:
        self._jobs = jobs
        self.deleted: list[str] = []
        self.fail_ids: set[str] = set()

    def list_jobs(self) -> list[Any]:
        return self._jobs

    def delete_job(self, submission_id: str) -> bool:
        if submission_id in self.fail_ids:
            raise RuntimeError("backend refused")
        self.deleted.append(submission_id)
        return True


def _job(i: int, status: str = "SUCCEEDED") -> Any:
    return SimpleNamespace(submission_id=f"job-{i}", status=status, start_time=i)


def test_deletes_only_beyond_the_keep_window() -> None:
    client = _Client([_job(i) for i in range(10)])
    result = prune_jobs(client, keep_newest=3)
    assert result.deleted == 7
    assert result.kept_newest == 3
    # Newest three (highest start_time) survive.
    assert set(client.deleted) == {f"job-{i}" for i in range(7)}


def test_running_jobs_are_NEVER_deleted_even_when_old() -> None:
    """The asymmetric failure: deleting live work is sabotage, not retention."""
    jobs = [_job(0, "RUNNING"), _job(1, "PENDING"), *[_job(i) for i in range(2, 6)]]
    client = _Client(jobs)
    result = prune_jobs(client, keep_newest=1)
    assert "job-0" not in client.deleted
    assert "job-1" not in client.deleted
    assert result.skipped_active == 2
    assert result.deleted == 3  # job-2..4 (job-5 is the kept newest)


def test_newest_kept_regardless_of_terminal_state() -> None:
    client = _Client([_job(0), _job(1, "FAILED"), _job(2, "STOPPED")])
    prune_jobs(client, keep_newest=2)
    assert client.deleted == ["job-0"]


def test_a_failing_delete_is_counted_not_raised() -> None:
    client = _Client([_job(i) for i in range(5)])
    client.fail_ids = {"job-1"}
    result = prune_jobs(client, keep_newest=0)
    assert result.deleted == 4
    assert result.failed == 1
    assert result.failed_ids == ["job-1"]


def test_driver_jobs_without_submission_id_are_skipped() -> None:
    jobs = [SimpleNamespace(submission_id=None, status="SUCCEEDED", start_time=0), _job(1)]
    client = _Client(jobs)
    result = prune_jobs(client, keep_newest=0)
    assert client.deleted == ["job-1"]
    assert result.deleted == 1


def test_negative_keep_is_a_caller_bug() -> None:
    with pytest.raises(ValueError, match="keep_newest"):
        prune_jobs(_Client([]), keep_newest=-1)
