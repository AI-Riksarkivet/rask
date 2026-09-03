"""A job that is ALREADY GONE is the outcome retention wanted, not a failure to report.

`compute` runs at `replicas: 2` in production (`chart/values-prod.yaml`) and its prune cron is a
`bindings.cron` input binding, which fires on EVERY replica — Dapr's cron component is stateless and
uncoordinated ("each replica runs the schedule independently, causing duplicate triggers"). So both
pods list the same jobs, compute the same delete set from the same deterministic ordering, and both
attempt every delete.

Ray's `delete_job` raises `RuntimeError` when the job does not exist (its own docstring). So the losing
replica raises once per job the winner already deleted, and every one of those lands in `failed` with a
`could not delete job <id>` WARNING. A pass that reclaimed 500 jobs reports 500 failures — a false alarm
that scales with the amount of successful work done, on the one signal an operator would use to decide
retention is broken.

`prune_jobs`' own docstring already claims the property this pins: "retention is idempotent and
best-effort by design — the next tick retries whatever remains". Counting the idempotent case as a
failure is precisely not that. Converged is not failed.

The distinction is kept in its OWN counter rather than folded into `deleted`: the winning replica really
did delete the row and the loser really did not, so adding them would double-count reclamation across
the two pods and make the metric useless for "how much did retention actually reclaim".
"""

from __future__ import annotations

from typing import Any, cast

from ray_kit.prune import JobsClient, prune_jobs


class _Job:
    def __init__(self, submission_id: str, status: str, start_time: int) -> None:
        self.submission_id = submission_id
        self.status = status
        self.start_time = start_time


class _RaceLosingClient:
    """A Ray client standing in for the replica that lost every delete race.

    Raises Ray's own message verbatim — the discrimination is on what Ray actually says, so a test
    passing against invented wording would prove nothing.
    """

    def __init__(self, jobs: list[_Job], *, gone: set[str]) -> None:
        self._jobs = jobs
        self._gone = gone
        self.attempted: list[str] = []

    def list_jobs(self) -> list[Any]:
        return list(self._jobs)

    def delete_job(self, job_id: str) -> bool:
        self.attempted.append(job_id)
        if job_id in self._gone:
            raise RuntimeError(f"Job {job_id} does not exist")
        return True


def _terminal(n: int) -> list[_Job]:
    return [_Job(f"job-{i}", "SUCCEEDED", start_time=1000 - i) for i in range(n)]


def test_a_job_another_replica_already_deleted_is_not_counted_as_a_failure() -> None:
    jobs = _terminal(6)
    # The other replica won every race for the four jobs outside the keep window.
    client = _RaceLosingClient(jobs, gone={"job-2", "job-3", "job-4", "job-5"})

    result = prune_jobs(cast(JobsClient, client), keep_newest=2)

    assert client.attempted == ["job-2", "job-3", "job-4", "job-5"], "the delete set must be unchanged"
    assert result.failed == 0, "a converged job was reported as a retention failure"
    assert result.failed_ids == [], "an already-absent job must not be sampled as a failing id"
    assert result.already_absent == 4
    assert result.deleted == 0, "this replica deleted nothing; crediting it would double-count reclamation"


def test_a_REAL_delete_failure_is_still_reported() -> None:
    """The guard must discriminate, not blanket-swallow.

    A job that is present and undeletable — a permission error, a wedged dashboard, a non-terminal
    state — is the case the `failed` counter and its sampled ids exist for. Losing that signal would be
    worse than the false alarm it replaces, because retention would then look healthy while reclaiming
    nothing.
    """

    class _Wedged(_RaceLosingClient):
        def delete_job(self, job_id: str) -> bool:
            self.attempted.append(job_id)
            raise RuntimeError("Request to the job server failed with status 500")

    client = _Wedged(_terminal(4), gone=set())
    result = prune_jobs(cast(JobsClient, client), keep_newest=1)

    assert result.failed == 3
    assert result.failed_ids == ["job-1", "job-2", "job-3"]
    assert result.already_absent == 0


def test_an_unrecognized_error_FAILS_CLOSED_as_a_failure() -> None:
    """Wording this guard does not recognise must count as a failure, never as convergence.

    The discrimination is on Ray's message text because Ray flattens three distinct causes into one
    `RuntimeError`. A message this does not match is therefore an UNKNOWN cause, and the safe direction
    for an unknown is to report it: a false alarm is recoverable, a silently-swallowed real failure is
    retention that looks healthy while the store fills up.
    """

    class _Strange(_RaceLosingClient):
        def delete_job(self, job_id: str) -> bool:
            self.attempted.append(job_id)
            raise RuntimeError("some future ray wording nobody has seen")

    client = _Strange(_terminal(3), gone=set())
    result = prune_jobs(cast(JobsClient, client), keep_newest=1)

    assert result.failed == 2
    assert result.already_absent == 0
