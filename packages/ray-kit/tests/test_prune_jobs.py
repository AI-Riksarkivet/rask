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


def test_a_burst_of_SUCCESSES_does_not_evict_the_FAILURES() -> None:
    """Retention by recency alone destroys the only post-mortem a failed Ray job leaves behind.

    Ray writes job-driver output to a file inside the container that nothing ships, so the ONLY
    durable record of why a stage died is the job row itself — readable through the dashboard until
    this pruner removes it. Sorting purely by `start_time` means a busy afternoon of successful jobs
    pushes every failure past the keep-window and deletes it, and post-mortem becomes bounded by
    submission VOLUME rather than by time. That is precisely backwards: the failures are the rows
    worth keeping.

    `_KEEP` is 500 in the deployed cron, and the medallion lane submits one job per stage per
    trigger, so this is an ordinary Tuesday rather than a pathological case.
    """
    failures = [_job(i, status="FAILED") for i in range(3)]  # oldest
    successes = [_job(i) for i in range(10, 20)]  # newest
    client = _Client(failures + successes)

    result = prune_jobs(client, keep_newest=5, keep_newest_failed=5)

    assert not any(d.startswith("job-0") or d in {"job-1", "job-2"} for d in client.deleted), (
        f"a failed job was deleted by a burst of successes — deleted {client.deleted}"
    )
    assert result.kept_failed == 3, f"expected the 3 failures retained, got kept_failed={result.kept_failed}"


def test_keeping_failures_does_not_stop_the_pruner_bounding_growth() -> None:
    """The retention window still has to bound the listing that OOM-killed the pod. Keeping failures
    is a floor under the post-mortem, not an exemption from retention."""
    client = _Client([_job(i, status="FAILED") for i in range(20)])

    result = prune_jobs(client, keep_newest=2, keep_newest_failed=3)

    # 20 failures, keep the newest 3 of them -> 17 deleted, not 0.
    assert result.deleted == 17, f"failures are being kept without bound — deleted {result.deleted}"


def test_STOPPED_counts_as_a_failure_worth_keeping() -> None:
    """A STOPPED job is terminal-bad: someone or something killed it, and why is a question asked
    days later. It shares the failure floor rather than the ordinary window."""
    client = _Client([_job(0, status="STOPPED"), *[_job(i) for i in range(10, 20)]])

    prune_jobs(client, keep_newest=2, keep_newest_failed=5)

    assert "job-0" not in client.deleted, "a STOPPED job was pruned as though it were a success"
