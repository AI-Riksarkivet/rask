"""Derive what the orchestrator would do next from current Ray + DB state.

Pure derivation — no writes. Consumed by both the `GET /orchestrator/state`
endpoint (so the UI can render the current decision) and by
`viewer.services.orchestrator.loop.tick`, which acts on the same derived
state.
"""

import logging
import re
import time
from typing import cast

import httpx
from anyio import to_thread
from ray.dashboard.modules.job.common import JobStatus
from ray.job_submission import JobSubmissionClient
from sqlmodel.ext.asyncio.session import AsyncSession

from viewer.models.enums import Pipeline, RayStage, TaskState
from viewer.repositories import batch as batch_repo
from viewer.schemas.orchestrator import Cooldown, OrchestratorState, SlimJob, SlotState, StageStat
from viewer.schemas.ray import RayJob
from viewer.services import ray_dashboard


log = logging.getLogger(__name__)


HTR_READY_FRACTION = 0.95
FAIL_COOLDOWN_SECS = 600
_CHUNK_RE = re.compile(r"chunk-(\d+)-of-")
_MS_PER_SECOND = 1000.0

_ACTIVE_STATUSES: frozenset[JobStatus] = frozenset({JobStatus.RUNNING, JobStatus.PENDING})


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
        # Keep in milliseconds, matching RayJob.start_time everywhere else.
        # The frontend's fmtRuntime expects ms.
        start_time=float(j.start_time) if j.start_time is not None else None,
        chunk_id=int(m.group(1)) if m else None,
    )


def _as_dict(v: object) -> dict[str, object]:
    """Narrow `v` to `dict[str, object]` (or empty) — used at the JSON boundary.

    Cast is intentional: at runtime the value is JSON-decoded so its keys are
    strings; the type-checker can't see that, so we assert it here once for
    every reader downstream.
    """
    if not isinstance(v, dict):
        return {}
    return cast(dict[str, object], v)


def _as_int(v: object) -> int:
    """Narrow `v` to `int` (or 0) — used at the JSON boundary."""
    return v if isinstance(v, int) else 0


def _cluster_summary(data: dict[str, object]) -> dict[str, object]:
    """Unwrap Ray's deeply-nested `/api/v0/tasks/summarize` envelope.

    Ray returns `{data: {result: {result: {node_id_to_summary: {cluster: {summary: {...}}}}}}}`.
    Any missing layer yields an empty dict so the caller can keep walking.
    """
    cursor: dict[str, object] = data
    for key in ("data", "result", "result", "node_id_to_summary", "cluster", "summary"):
        cursor = _as_dict(cursor.get(key))
        if not cursor:
            return {}
    return cursor


async def _task_summary_for_job(
    *,
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
    except httpx.HTTPError as exc:
        log.warning(f"ray tasks/summarize failed for job {driver_job_id}: {exc}")
        return []

    summary = _cluster_summary(_as_dict(data))
    out: list[StageStat] = []
    for stage in stage_names:
        sc = _as_dict(_as_dict(summary.get(f"MapWorker(MapBatches({stage})).submit")).get("state_counts"))
        out.append(
            StageStat(
                stage=stage,
                finished=_as_int(sc.get(TaskState.FINISHED)),
                running=_as_int(sc.get(TaskState.RUNNING)),
                scheduled=_as_int(sc.get(TaskState.SCHEDULED)),
                failed=_as_int(sc.get(TaskState.FAILED)),
                pending=sum(_as_int(v) for k, v in sc.items() if k.startswith(TaskState.PENDING) or k == TaskState.WAITING),
                total=sum(_as_int(v) for v in sc.values()),
            )
        )
    return out


async def _driver_job_id(client: JobSubmissionClient | None, submission_id: str) -> str | None:
    if client is None:
        return None
    try:
        details = await to_thread.run_sync(client.get_job_info, submission_id)
    except ray_dashboard.RAY_TRANSIENT_ERRORS as exc:
        log.warning(f"ray get_job_info failed for {submission_id}: {exc}")
        return None
    if details is None:
        return None
    return details.job_id


async def derive_state(
    *,
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
        # Use _pipeline_for to classify: "htrflow-chunk-…" and any future
        # non-prefetch pipeline collapse to Pipeline.HTR (the slot semantics),
        # while a literal-prefix match would only catch "htr-…".
        for j in jobs:
            sid = j.submission_id or ""
            if not sid:
                continue
            if _pipeline_for(sid) is pipeline and j.status in _ACTIVE_STATUSES:
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
                error_type=j.error_type,
                message=j.message,
                driver_exit_code=j.driver_exit_code,
            )
        )

    prefetch_pending = await batch_repo.prefetch_pending_chunk_ids(session)
    progress = await batch_repo.chunks_with_progress(session)
    ready_for_htr = [
        p.chunk_id for p in progress if p.expected_pages and p.transcribed_pages < p.expected_pages and p.cached_pages / p.expected_pages >= HTR_READY_FRACTION
    ]

    cooldown_pf = {c.chunk_id for c in cooldowns if c.pipeline is Pipeline.PREFETCH}
    cooldown_htr = {c.chunk_id for c in cooldowns if c.pipeline is Pipeline.HTR}
    next_prefetch = next((cid for cid in prefetch_pending if cid not in cooldown_pf), None)
    next_htr = next((cid for cid in ready_for_htr if cid not in cooldown_htr), None)

    prefetch_slot = await _build_slot(
        http=http,
        client=client,
        dashboard_url=dashboard_url,
        running=prefetch_running,
        next_chunk=next_prefetch,
        queue_len=len(prefetch_pending),
        stages=PREFETCH_STAGES,
    )
    htr_slot = await _build_slot(
        http=http,
        client=client,
        dashboard_url=dashboard_url,
        running=htr_running,
        next_chunk=next_htr,
        queue_len=len(ready_for_htr),
        stages=HTR_STAGES,
    )

    return OrchestratorState(
        ok=True,
        prefetch=prefetch_slot,
        htr=htr_slot,
        cooldowns=cooldowns,
        ready_threshold=HTR_READY_FRACTION,
        cooldown_secs=FAIL_COOLDOWN_SECS,
    )


async def _build_slot(
    *,
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
            stage_stats = await _task_summary_for_job(http=http, dashboard_url=dashboard_url, driver_job_id=jid, stage_names=stages)
    return SlotState(
        running=_slim_job(running) if running else None,
        next=next_chunk,
        queue_len=queue_len,
        stages=stage_stats,
    )
