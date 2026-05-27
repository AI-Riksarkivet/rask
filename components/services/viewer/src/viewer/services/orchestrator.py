"""Derive what the orchestrator would do next from current Ray + DB state.

Mirrors the constants used by `scripts/orchestrator.py` so the UI shows the
same decisions the cron-driven tick would make. Pure derivation; no writes.
"""

import re
import time
from enum import StrEnum

import anyio
import httpx
from ray.dashboard.modules.job.common import JobStatus
from ray.job_submission import JobSubmissionClient
from sqlmodel.ext.asyncio.session import AsyncSession

from viewer.models.batch import Pipeline
from viewer.repositories import batch as batch_repo
from viewer.schemas.orchestrator import Cooldown, OrchestratorState, SlimJob, SlotState, StageStat
from viewer.schemas.ray import RayJob
from viewer.services import ray_dashboard


HTR_READY_FRACTION = 0.95
FAIL_COOLDOWN_SECS = 600
_CHUNK_RE = re.compile(r"chunk-(\d+)-of-")
_MS_PER_SECOND = 1000.0

_ACTIVE_STATUSES: frozenset[JobStatus] = frozenset({JobStatus.RUNNING, JobStatus.PENDING})


class TaskState(StrEnum):
    """Ray Data task-scheduler state keys from /api/v0/tasks/summarize.

    Not a Ray public enum — these are external API keys. The viewer collapses
    them into four UI buckets (finished/running/scheduled/pending/failed).
    `PENDING` is the **prefix** for several substates (PENDING_NODE_ASSIGNMENT,
    PENDING_ARGS_AVAIL, …), used with `str.startswith()` rather than equality.
    """

    FINISHED = "FINISHED"
    RUNNING = "RUNNING"
    SCHEDULED = "SUBMITTED_TO_WORKER"
    FAILED = "FAILED"
    WAITING = "WAITING"
    PENDING = "PENDING"

class RayStage(StrEnum):
    """Ray actor names matching the pipeline stages in `components/apps/runner/src/runner/pipeline.py`.

    Used to query `/api/v0/tasks/summarize` (the stage name appears inside
    `MapWorker(MapBatches(<stage>)).submit`) — string-equality with Ray's task
    naming is the contract.
    """

    PAGE_LOADER = "PageLoaderActor"
    LAYOUT = "LayoutActor"
    LINE = "LineActor"
    TRANSCRIBE = "TranscribeViaServe"
    ALTO_EXPORT = "AltoExportActor"
    ALTO_WRITER = "AltoWriterActor"
    PREFETCH = "PrefetchActor"


HTR_STAGES: tuple[RayStage, ...] = (
    RayStage.PAGE_LOADER,
    RayStage.LAYOUT,
    RayStage.LINE,
    RayStage.TRANSCRIBE,
    RayStage.ALTO_EXPORT,
    RayStage.ALTO_WRITER,
)
PREFETCH_STAGES: tuple[RayStage, ...] = (RayStage.PREFETCH,)


def _ms_to_sec(v: int | None) -> float | None:
    return None if v is None else float(v) / _MS_PER_SECOND


def _pipeline_for(submission_id: str) -> Pipeline:
    """Infer pipeline from the submission_id prefix. Defaults to HTR for any
    id that doesn't carry the PREFETCH prefix."""
    if submission_id.startswith(Pipeline.PREFETCH.submission_id_prefix):
        return Pipeline.PREFETCH
    return Pipeline.HTR


def _slim_job(j: RayJob) -> SlimJob:
    sid = j.submission_id or ""
    m = _CHUNK_RE.search(sid)
    return SlimJob(
        submission_id=sid,
        status=j.status,
        start_time=_ms_to_sec(j.start_time),
        chunk_id=int(m.group(1)) if m else None,
    )


async def _task_summary_for_job(
    http: httpx.AsyncClient,
    dashboard_url: str,
    driver_job_id: str,
    stage_names: tuple[RayStage, ...],
) -> list[StageStat]:
    try:
        r = await http.get(
            f"{dashboard_url}/api/v0/tasks/summarize",
            params={"filter_keys": "job_id", "filter_predicates": "=", "filter_values": driver_job_id},
        )
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError:
        return []
    summary = ((data.get("data") or {}).get("result") or {}).get("result", {}).get("node_id_to_summary", {}).get("cluster", {}).get("summary", {})
    out: list[StageStat] = []
    for stage in stage_names:
        info = summary.get(f"MapWorker(MapBatches({stage})).submit") or {}
        sc = info.get("state_counts") or {}
        finished = int(sc.get(TaskState.FINISHED, 0))
        running = int(sc.get(TaskState.RUNNING, 0))
        scheduled = int(sc.get(TaskState.SCHEDULED, 0))
        failed = int(sc.get(TaskState.FAILED, 0))
        pending = sum(int(v) for k, v in sc.items() if k.startswith(TaskState.PENDING) or k == TaskState.WAITING)
        total = sum(int(v) for v in sc.values())
        out.append(
            StageStat(
                stage=stage,
                finished=finished,
                running=running,
                scheduled=scheduled,
                pending=pending,
                failed=failed,
                total=total,
            )
        )
    return out


async def _driver_job_id(client: JobSubmissionClient | None, submission_id: str) -> str | None:
    if client is None:
        return None
    try:
        details = await anyio.to_thread.run_sync(client.get_job_info, submission_id)
    except Exception:
        return None
    return details.job_id


async def derive_state(
    http: httpx.AsyncClient,
    client: JobSubmissionClient | None,
    dashboard_url: str,
    session: AsyncSession,
) -> OrchestratorState:
    jobs_payload = await ray_dashboard.list_jobs(client, dashboard_url)
    if not jobs_payload.ok:
        return OrchestratorState(ok=False, error=jobs_payload.error or "ray dashboard unreachable")
    jobs = jobs_payload.jobs

    def running_for(pipeline: Pipeline) -> RayJob | None:
        prefix = pipeline.submission_id_prefix
        for j in jobs:
            sid = j.submission_id or ""
            if sid.startswith(prefix) and j.status in _ACTIVE_STATUSES:
                return j
        return None

    prefetch_running = running_for(Pipeline.PREFETCH)
    htr_running = running_for(Pipeline.HTR)

    now = time.time()
    cooldowns: list[Cooldown] = []
    for j in jobs:
        if j.status != JobStatus.FAILED:
            continue
        end_sec = _ms_to_sec(j.end_time)
        if end_sec is None:
            continue
        elapsed = now - end_sec
        if elapsed >= FAIL_COOLDOWN_SECS:
            continue
        sid = j.submission_id or ""
        m = _CHUNK_RE.search(sid)
        if not m:
            continue
        cooldowns.append(
            Cooldown(
                submission_id=sid,
                chunk_id=int(m.group(1)),
                pipeline=_pipeline_for(sid),
                expires_in_secs=max(0, int(FAIL_COOLDOWN_SECS - elapsed)),
            )
        )

    prefetch_pending = await batch_repo.prefetch_pending_chunk_ids(session)
    progress = await batch_repo.chunks_with_progress(session)
    ready_for_htr = [
        cid
        for cid, expected, cached, transcribed in progress
        if expected and transcribed < expected and cached / expected >= HTR_READY_FRACTION
    ]

    cooldown_pf = {c.chunk_id for c in cooldowns if c.pipeline is Pipeline.PREFETCH}
    cooldown_htr = {c.chunk_id for c in cooldowns if c.pipeline is Pipeline.HTR}
    next_prefetch = next((cid for cid in prefetch_pending if cid not in cooldown_pf), None)
    next_htr = next((cid for cid in ready_for_htr if cid not in cooldown_htr), None)

    prefetch_slot = await _build_slot(http, client, dashboard_url, prefetch_running, next_prefetch, len(prefetch_pending), PREFETCH_STAGES)
    htr_slot = await _build_slot(http, client, dashboard_url, htr_running, next_htr, len(ready_for_htr), HTR_STAGES)

    return OrchestratorState(
        ok=True,
        prefetch=prefetch_slot,
        htr=htr_slot,
        cooldowns=cooldowns,
        ready_threshold=HTR_READY_FRACTION,
        cooldown_secs=FAIL_COOLDOWN_SECS,
    )


async def _build_slot(
    http: httpx.AsyncClient,
    client: JobSubmissionClient | None,
    dashboard_url: str,
    running: RayJob | None,
    next_chunk: int | None,
    queue_len: int,
    stages: tuple[RayStage, ...],
) -> SlotState:
    stage_stats: list[StageStat] = []
    if running:
        jid = await _driver_job_id(client, running.submission_id or "")
        if jid:
            stage_stats = await _task_summary_for_job(http, dashboard_url, jid, stages)
    return SlotState(
        running=_slim_job(running) if running else None,
        next=next_chunk,
        queue_len=queue_len,
        stages=stage_stats,
    )
