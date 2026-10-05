"""The Ray dashboard Jobs API, spoken directly — medallion's adapter for the Ray engine.

MOVED HERE FROM `ray-kit` rather than copied, and the move is what lets `services/medallion` declare
no compute engine at all. Measured 2026-09-07: this module is pure HTTPX plus OpenTelemetry
propagation and imports no Ray, while the `ray-kit` PACKAGE declares `ray[default]` for its SDK half
(`build_client`, `JobSubmissionClient`, the dashboard wrapper). Medallion consumed only this module
and paid for the whole engine to get it; `services/compute` — the estate's Ray-facing service —
consumes only the other half and is unaffected. One consumer each, so the split is clean and nothing
is duplicated.

WHY ENGINE KNOWLEDGE MAY LIVE HERE. It is part of the Ray ADAPTER (`rayjobs_api_executor`), not a consumer: the
cascade and the training lane reach it through `service_kit.lakehouse.executor`, so swapping the engine means adding a
sibling adapter rather than editing a planner. Two `.importlinter` contracts encode that —
`the-lakehouse-is-not-built-on-ray` refuses a `ray` or `ray_kit` import in any lakehouse service, and
`only-the-ray-adapter-speaks-the-jobs-api` refuses this module to every medallion module but the adapter.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Literal

import httpx

from medallion.services.ray_job_failure import RayJobFailure
from service_kit.lakehouse.executor import EngineError


log = logging.getLogger(__name__)

#: Ray's terminal job states. SUCCEEDED is the only good one; the other two are terminal-bad and are what
#: makes a deterministic id safe to reuse (a terminal job can be deleted and resubmitted).
TERMINAL_OK = "SUCCEEDED"
TERMINAL_BAD = ("FAILED", "STOPPED")


class RayJobError(EngineError):
    """The Ray dashboard could not be reached, or refused a submit or a read: the port's `EngineError`, for this engine."""


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

    A SINGLE read, deliberately: this is not the `while True: sleep()` completion poll that A13 deleted
    from `medallion/services/ray_submit.py`, and it must not grow back into one. That loop was wrong
    because it held a Dapr ack across the whole job runtime, so a job outliving the redelivery window
    exhausted it. The fix is not "poll faster" — it is to put the WAITING somewhere durable: a stage's
    plan sweep and a training run's plan sweep ask on each cron tick, and this function
    only answers the question once.

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


def is_terminal(status: str | None) -> bool:
    """Whether ``status`` is a state Ray will never move out of.

    Centralised so a caller cannot check only ``SUCCEEDED`` and hang forever on a job that FAILED —
    the asymmetry that makes "wait for completion" loops subtly wrong.
    """
    return status == TERMINAL_OK or status in TERMINAL_BAD


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
