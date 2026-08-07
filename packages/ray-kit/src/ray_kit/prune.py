"""Ray job-history retention — the DELETE half of the #136 fix.

Ray's Jobs API keeps every submission forever and `GET /api/jobs` takes no parameters (spec
v4.0.0), so history only grows: the live cluster reached 81,155 jobs / 164.7 MB per listing, which
is what OOMKilled the compute service. `DELETE /api/jobs/{submission_id}` is the spec's one
reclamation lever — it deletes a job "already in a terminal state" (400 for non-API jobs, 500 for
non-terminal), and this module drives it.

Deliberately a plain synchronous function: the Job SDK is synchronous, and the one caller (the
compute service's cron-binding endpoint) wraps the whole pass in a threadpool — the same boundary
`dashboard.list_jobs` already uses. One responsibility: decide-and-delete; scheduling lives in the
chart's cron Component, reporting in the returned model.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, Field


log = logging.getLogger(__name__)

#: Spec `JobStatus` values a job cannot leave — the only states `DELETE /api/jobs/{id}` accepts.
#: PENDING/RUNNING are never candidates: deleting live work is not retention, it is sabotage.
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "STOPPED"})

#: How many failing ids the report samples — enough to see a pattern, small enough to log.
_FAILED_SAMPLE = 5


class _JobLike(Protocol):
    """The three fields retention decides on — structural, so tests need no Ray import."""

    @property
    def submission_id(self) -> str | None: ...
    @property
    def status(self) -> object: ...
    @property
    def start_time(self) -> int | None: ...


class JobsClient(Protocol):
    """The two SDK calls this module makes (`JobSubmissionClient` satisfies it)."""

    # `Sequence`, not `list`: list is invariant, and the SDK returns list[JobDetails].
    def list_jobs(self) -> Sequence[_JobLike]: ...
    # Positional-only: the SDK spells the parameter `job_id`, our fakes `submission_id` —
    # the name is not part of the contract, the position is.
    def delete_job(self, submission_id: str, /) -> bool: ...


class PruneResult(BaseModel):
    """What one retention pass did — enough for a log line and a metric, nothing more."""

    total: int
    kept_newest: int
    deleted: int = 0
    failed: int = 0
    skipped_active: int = 0
    #: First few failing ids, so a systematic failure is debuggable from the report alone.
    failed_ids: list[str] = Field(default_factory=list)


def prune_jobs(client: JobsClient, *, keep_newest: int) -> PruneResult:
    """Delete TERMINAL jobs beyond the ``keep_newest`` most recent submissions.

    The newest ``keep_newest`` are kept regardless of state — they are what an operator is
    looking at. Everything older is deleted only if terminal; active jobs are counted and left
    alone. A failed delete is counted, sampled into ``failed_ids`` and never raised: retention is
    idempotent and best-effort by design — the next tick retries whatever remains, and one
    undeletable job must not abort the other tens of thousands.

    The single ``list_jobs()`` materialisation is the price of Ray's parameterless listing; each
    pass shrinks the next one, so the first run is the expensive one and the steady state is cheap.
    """
    if keep_newest < 0:
        raise ValueError(f"keep_newest must be >= 0, got {keep_newest}")

    jobs = client.list_jobs()
    ordered = sorted(jobs, key=lambda j: j.start_time or 0, reverse=True)
    result = PruneResult(total=len(ordered), kept_newest=min(keep_newest, len(ordered)))

    for job in ordered[keep_newest:]:
        if str(job.status) not in TERMINAL_STATUSES:
            result.skipped_active += 1
            continue
        submission_id = job.submission_id
        if not submission_id:
            # A DRIVER-type job (spec `JobType`) has no submission id and cannot be deleted
            # through the Jobs API; counting it as "active" would misreport, so skip silently.
            continue
        try:
            client.delete_job(submission_id)
            result.deleted += 1
        except Exception:
            result.failed += 1
            if len(result.failed_ids) < _FAILED_SAMPLE:
                result.failed_ids.append(submission_id)
            log.warning("could not delete job %s", submission_id, exc_info=True)
    return result
