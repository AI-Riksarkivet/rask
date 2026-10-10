"""The Ray dashboard Jobs API, spoken directly: submit-or-reattach, a job's status, and Ray's stated cause.

Pure HTTPX and pydantic. Its consumer is `compute.engine`, the estate's engine app for Ray (CP-056): the lakehouse
names no engine and reaches this through service invocation of compute's engine door, so the Jobs API is spoken by
the one service that already holds the dashboard client and its auth.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict


log = logging.getLogger(__name__)

#: Ray's terminal-bad job states: what makes a deterministic id safe to reuse (a terminal job can be deleted and
#: resubmitted).
TERMINAL_BAD = ("FAILED", "STOPPED")


class RayJobError(RuntimeError):
    """The Ray dashboard could not be reached, or refused a submit or a read."""


class RayJobFailure(BaseModel):
    """Ray's own account of WHY a job ended badly: the three fields of its job details a post-mortem reads.

    `extra="ignore"` because Ray's `JobDetails` carries `runtime_env` (the job's env vars and working-dir refs) and an
    arbitrary user `metadata` dict, and retaining either would put a job's whole environment into whatever this is
    rendered into.
    """

    model_config = ConfigDict(extra="ignore")
    error_type: str | None = None
    #: Ray's driver message, UNBOUNDED: it can be an entire traceback.
    message: str | None = None
    #: The driver's process exit code. 137 is SIGKILL, the host-RAM OOM that kills a stage with no other symptom.
    driver_exit_code: int | None = None


async def job_failure(client: httpx.AsyncClient, sub_id: str) -> RayJobFailure | None:
    """Ray's stated CAUSE for ``sub_id`` — ``None`` when the dashboard does not know the job.

    Same endpoint `job_status` already reads, and deliberately a SEPARATE call rather than a widened
    return from that one. `job_status` is the hot poll: its answer is recorded in Dapr workflow
    history on every tick, so changing its shape would break replay for in-flight instances, and
    every poll would carry a traceback it does not need. This runs once, on the terminal-bad path.

    Mirrors `job_status`'s error contract exactly, so the two cannot be reasoned about differently:
    404 is ``None`` (the job is gone — pruning is by recency, so a failure CAN outlive its record),
    and a transport or 5xx failure raises, because an unreachable dashboard is not evidence about the
    job. The caller decides whether a missing cause is fatal; for the stage watcher it is not.
    """
    try:
        response = await client.get(f"/api/jobs/{sub_id}")
    except httpx.HTTPError as exc:
        raise RayJobError(f"failed to read failure detail of ray job {sub_id}: {exc}") from exc
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise RayJobError(f"failed to read failure detail of ray job {sub_id}: HTTP {response.status_code}")
    return RayJobFailure.model_validate(response.json())


async def job_status(client: httpx.AsyncClient, sub_id: str) -> str | None:
    """One status read for ``sub_id`` — ``None`` when the job is not (yet) known to the dashboard.

    A SINGLE read, deliberately, and it must not grow into a completion poll: a poll holds whoever waits on it
    across the job's runtime. The WAITING is durable elsewhere: the planner's plan sweep asks on each cron tick, and
    this function only answers the question once.

    ``None`` rather than a raise for an unknown id, because the two callers mean different things by it:
    a caller asking immediately after submit may legitimately see the id before the dashboard does,
    and treating that as failure would abort a healthy job on a race. A TRANSPORT failure is still a
    raise — an unreachable dashboard is not evidence about the job.
    """
    try:
        response = await client.get(f"/api/jobs/{sub_id}")
    except httpx.HTTPError as exc:
        raise RayJobError(f"failed to read status of ray job {sub_id}: {exc}") from exc
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise RayJobError(f"failed to read status of ray job {sub_id}: HTTP {response.status_code}")
    status = response.json().get("status")
    return str(status) if status is not None else None


async def submit_or_reattach(
    client: httpx.AsyncClient,
    sub_id: str,
    body: Mapping[str, object],
    *,
    on_terminal_failure: Literal["resubmit", "report"] = "resubmit",
) -> str:
    """``POST /api/jobs/``, tolerating an id that already exists. Returns what actually happened:
    ``"submitted"`` | ``"reattached"`` | ``"resubmitted"`` | ``"already_failed"``.

    A 4xx for a duplicate id re-attaches to that job. What happens when the prior one terminally
    FAILED or STOPPED is the ONE deliberate policy divergence between this kernel's callers, so it is
    a parameter rather than a fork:

    - ``"resubmit"`` (the default — the STAGE contract): delete it and resubmit fresh, so the
      redelivery actually retries the work on a healthy worker. Without that branch every redelivery
      re-observes the same failure until maxDeliver silently drops the trigger, and the deterministic
      id that bought idempotency becomes the thing that guarantees the work never completes.
    - ``"report"`` (the TRAIN contract, docs/RAY-TRAIN.md D2): answer ``"already_failed"`` and touch
      nothing — training compute is expensive, so a failed run is terminal until a human resubmits
      with a fresh token, and the caller needs the outcome so it can DROP attributably.

    This parameter exists because the alternative was measured: train carried its own inline copy of
    this dance to get the D2 branch, and a mirrored submission seam is where one-sided fixes land
    (the credential echo; the work-axis id fix).
    """
    try:
        response = await client.post("/api/jobs/", json=dict(body))
        if response.status_code < 400:
            return "submitted"
        existing = await client.get(f"/api/jobs/{sub_id}")
        if existing.status_code == 200:
            if existing.json().get("status") in TERMINAL_BAD:
                if on_terminal_failure == "report":
                    log.warning("ray_job_previously_failed", extra={"submission_id": sub_id})
                    return "already_failed"
                # DELETE is valid only on a terminal job; FAILED/STOPPED are.
                await client.delete(f"/api/jobs/{sub_id}")
                fresh = await client.post("/api/jobs/", json=dict(body))
                fresh.raise_for_status()
                log.info("ray_job_resubmitted_after_failure", extra={"submission_id": sub_id})
                return "resubmitted"
            log.info("ray_job_reattach", extra={"submission_id": sub_id})
            return "reattached"
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RayJobError(f"failed to submit ray job {sub_id}: {exc}") from exc
    return "submitted"
