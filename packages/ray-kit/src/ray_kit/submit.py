"""Submitting a Ray job — the generic half, with no workload's settings in it (R2).

``ray_kit`` was read-only: schemas plus a dashboard wrapper. Everything that could START a job lived in
``medallion/services/ray_submit.py``, so the medallion owned job submission for the whole estate — which
is backwards. ``compute`` is the execution plane (it holds the Ray Job SDK, submits, polls and proxies
Serve), and R2 puts ETL there; it cannot do that while the only submitter is inside the service that
merely happens to have needed one first.

**What moved and what did not.** Of the eight functions in that module, five read no settings at all —
they are the submitter. The other three (``submit_stage_job``, ``submit_iiif_ingest_job``,
``submit_train_job``) are workload wrappers that read 9-11 fields of ``MedallionSettings`` each, and they
stay with their workloads: a wrapper's job is to know which entrypoint and which env its transform needs.
Only the mechanics move here, and they take explicit arguments rather than a settings object, so no
caller has to own the medallion's config shape to submit a job.

The pieces this owns are the ones that are hard to get right twice:

- **Deterministic submission ids**, so a redelivered trigger re-attaches instead of starting a duplicate.
- **Reattach-or-retry**: an id that already exists re-attaches, UNLESS the prior job terminally failed —
  then it is deleted and resubmitted, because otherwise every redelivery re-observes the same failure
  until maxDeliver silently drops the trigger. That is the deterministic-id poison, and it is the sort of
  thing a second implementation gets wrong.
- **Trace continuity across the Ray boundary**: without it the estate's distributed trace goes dark at
  submit and every job-side span is an orphan.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Mapping

import httpx
from opentelemetry import propagate


log = logging.getLogger(__name__)

#: Ray's terminal job states. SUCCEEDED is the only good one; the other two are terminal-bad and are what
#: makes a deterministic id safe to reuse (a terminal job can be deleted and resubmitted).
TERMINAL_OK = "SUCCEEDED"
TERMINAL_BAD = ("FAILED", "STOPPED")

#: Consecutive poll failures tolerated before giving up. A poll is a dashboard round-trip, and a brief
#: dashboard blip must not fail a job that is running fine.


class RayJobError(RuntimeError):
    """A submitted Ray job failed, was stopped, or did not finish within the timeout."""


def submission_id(stage: str, token: str | None, work: str = "") -> str:
    """A deterministic id per ``(stage, token, work)`` so redelivery re-attaches to the same job.

    Idempotency lives HERE rather than in the caller: a trigger delivered twice must not start two jobs,
    and the only thing both deliveries share is what this hashes.

    ``work`` is the job's own identity — for a stage transform, its ``from→to`` URIs. Without it a
    token-less trigger collapsed EVERY submission of a stage onto one id (``ray-silver-notoken``), and
    ``submit_or_reattach`` read the collision as a successful re-attach — the second transform's work
    silently never ran. The same collapse hid WITH a token whenever one trigger fans out to two tables
    of the same stage. The token stays visible in the id (operators grep the dashboard by it); the work
    rides as a short digest so arbitrarily long URIs can't push the id past Ray's limits.
    """
    raw = f"ray-{stage}-{token or 'notoken'}"
    if work:
        raw = f"{raw}-{hashlib.sha256(work.encode()).hexdigest()[:12]}"
    return re.sub(r"[^A-Za-z0-9_-]", "-", raw)[:200]


def trace_env() -> dict[str, str]:
    """The current span's W3C trace context, in env-var shape for a job's ``runtime_env``.

    Trace continuity across the Ray boundary: the estate's distributed trace used to go dark at submit
    because no submission site propagated context, leaving every job-side span an orphan.
    ``propagate.inject`` writes nothing when no valid span is active, so this degrades to ``{}`` and the
    job runs untraced — the trace is only ever CONTINUED, never fabricated. Only the W3C keys are lifted;
    the global propagator also emits baggage, which has no reader on the job side.
    """
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return {k.upper(): v for k, v in carrier.items() if k in ("traceparent", "tracestate")}


def lineage_env() -> dict[str, str]:
    """This pod's ``RASK_LINEAGE_*`` config, forwarded into a job's ``runtime_env``.

    A job that grows lineage-kit emission inherits the same endpoint and namespace the fleet uses. Empty
    when unconfigured — the job's lineage-kit degrades to its no-op emitter.
    """
    return {k: v for k, v in os.environ.items() if k.startswith("RASK_LINEAGE_")}


async def submit_or_reattach(client: httpx.AsyncClient, sub_id: str, body: Mapping[str, object]) -> None:
    """``POST /api/jobs/``, tolerating an id that already exists.

    A 4xx for a duplicate id re-attaches to that job — UNLESS the prior one terminally FAILED or STOPPED,
    in which case it is deleted and resubmitted fresh, so the redelivery actually retries the work on a
    healthy worker. Without that branch every redelivery re-observes the same failure until maxDeliver
    silently drops the trigger, and the deterministic id that bought idempotency becomes the thing that
    guarantees the work never completes.
    """
    try:
        response = await client.post("/api/jobs/", json=dict(body))
        if response.status_code < 400:
            return
        existing = await client.get(f"/api/jobs/{sub_id}")
        if existing.status_code == 200:
            if existing.json().get("status") in TERMINAL_BAD:
                # DELETE is valid only on a terminal job; FAILED/STOPPED are.
                await client.delete(f"/api/jobs/{sub_id}")
                fresh = await client.post("/api/jobs/", json=dict(body))
                fresh.raise_for_status()
                log.info("ray_job_resubmitted_after_failure", extra={"submission_id": sub_id})
                return
            log.info("ray_job_reattach", extra={"submission_id": sub_id})
            return
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RayJobError(f"failed to submit ray job {sub_id}: {exc}") from exc
