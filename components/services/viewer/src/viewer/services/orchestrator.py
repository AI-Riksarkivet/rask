"""Derive what the orchestrator would do next from current Ray + DB state.

Mirrors the constants used by `scripts/orchestrator.py` so the UI shows the
same decisions the cron-driven tick would make. Pure derivation; no writes.
"""

import re
import time
from typing import Any

import anyio
import httpx
from ray.job_submission import JobSubmissionClient
from sqlmodel.ext.asyncio.session import AsyncSession

from viewer.repositories import batch as batch_repo
from viewer.schemas.orchestrator import Cooldown, OrchestratorState, SlimJob, SlotState, StageStat
from viewer.services import ray_dashboard


HTR_READY_FRACTION = 0.95
FAIL_COOLDOWN_SECS = 600
_CHUNK_RE = re.compile(r"chunk-(\d+)-of-")

HTR_STAGES: tuple[str, ...] = (
    "PageLoaderActor",
    "LayoutActor",
    "LineActor",
    "TranscribeViaServe",
    "AltoExportActor",
    "AltoWriterActor",
)
PREFETCH_STAGES: tuple[str, ...] = ("PrefetchActor",)


def _slim_job(j: dict[str, Any]) -> SlimJob:
    sid = j.get("submission_id") or ""
    m = _CHUNK_RE.search(sid)
    return SlimJob(
        submission_id=sid,
        status=j.get("status"),
        start_time=j.get("start_time"),
        chunk_id=int(m.group(1)) if m else None,
    )


async def _task_summary_for_job(
    http: httpx.AsyncClient,
    dashboard_url: str,
    driver_job_id: str,
    stage_names: tuple[str, ...],
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
        finished = int(sc.get("FINISHED", 0))
        running = int(sc.get("RUNNING", 0))
        scheduled = int(sc.get("SUBMITTED_TO_WORKER", 0))
        failed = int(sc.get("FAILED", 0))
        pending = sum(int(v) for k, v in sc.items() if k.startswith("PENDING") or k == "WAITING")
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
    jobs_raw = [j.model_dump() for j in jobs_payload.jobs]

    def running_for(prefix: str) -> dict[str, Any] | None:
        for j in jobs_raw:
            sid = j.get("submission_id") or ""
            if sid.startswith(prefix) and j.get("status") in ("RUNNING", "PENDING"):
                return j
        return None

    prefetch_running = running_for("prefetch-")
    htr_running = running_for("htr-")

    now = time.time()
    cooldowns: list[Cooldown] = []
    for j in jobs_raw:
        if j.get("status") != "FAILED":
            continue
        end = j.get("end_time")
        if not end:
            continue
        elapsed = now - float(end)
        if elapsed >= FAIL_COOLDOWN_SECS:
            continue
        sid = j.get("submission_id") or ""
        m = _CHUNK_RE.search(sid)
        if not m:
            continue
        cooldowns.append(
            Cooldown(
                submission_id=sid,
                chunk_id=int(m.group(1)),
                pipeline="prefetch" if sid.startswith("prefetch-") else "htr",
                expires_in_secs=max(0, int(FAIL_COOLDOWN_SECS - elapsed)),
            )
        )

    prefetch_pending = await batch_repo.prefetch_pending_chunk_ids(session)
    progress = await batch_repo.chunks_with_progress(session)
    ready_for_htr = [cid for cid, expected, cached, transcribed in progress if expected and transcribed < expected and cached / expected >= HTR_READY_FRACTION]

    cooldown_pf = {c.chunk_id for c in cooldowns if c.pipeline == "prefetch"}
    cooldown_htr = {c.chunk_id for c in cooldowns if c.pipeline == "htr"}
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
    running: dict[str, Any] | None,
    next_chunk: int | None,
    queue_len: int,
    stages: tuple[str, ...],
) -> SlotState:
    stage_stats: list[StageStat] = []
    if running:
        jid = await _driver_job_id(client, running.get("submission_id") or "")
        if jid:
            stage_stats = await _task_summary_for_job(http, dashboard_url, jid, stages)
    return SlotState(
        running=_slim_job(running) if running else None,
        next=next_chunk,
        queue_len=queue_len,
        stages=stage_stats,
    )
