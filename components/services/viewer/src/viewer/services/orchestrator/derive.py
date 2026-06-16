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

from ray_kit import RAY_TRANSIENT_ERRORS
from ray_kit import dashboard as ray_dashboard
from ray_kit.schemas import RayJob
from viewer.models.enums import RayStage, TaskState
from viewer.models.pipelines import Slot, spec_for_submission_id
from viewer.repositories import batch as batch_repo
from viewer.schemas.orchestrator import Cooldown, OrchestratorState, SlimJob, SlotState, StageStat


log = logging.getLogger(__name__)


HTR_READY_FRACTION = 0.95
FAIL_COOLDOWN_SECS = 600
_CHUNK_RE = re.compile(r"chunk-(\d+)-of-")
_MS_PER_SECOND = 1000.0

_ACTIVE_STATUSES: frozenset[JobStatus] = frozenset({JobStatus.RUNNING, JobStatus.PENDING})


def _ms_to_sec(v: int | None) -> float | None:
    return None if v is None else float(v) / _MS_PER_SECOND


def _slot_for(submission_id: str) -> Slot:
    """Infer the concurrency slot from the submission_id prefix via the registry.

    A known prefix resolves to its spec's slot; an unknown / unprefixed id
    defaults to HTR, preserving the old prefix-sniff behaviour where anything
    that wasn't `prefetch-` was treated as an HTR-lane job.
    """
    spec = spec_for_submission_id(submission_id)
    return spec.slot if spec is not None else Slot.HTR


def _stages_for(submission_id: str) -> tuple[RayStage, ...]:
    """Per-stage telemetry actor names for the running job, from its spec.

    This is the htrflow-telemetry fix: an `htrflow-…` job resolves to the
    htrflow spec (stages=()), so it is no longer queried against the htr
    actor-per-stage names (PageLoaderActor/.../AltoExportActor). Unknown ids
    yield no stages → the UI shows no per-stage bars rather than wrong ones.
    """
    spec = spec_for_submission_id(submission_id)
    return spec.stages if spec is not None else ()


def _chunk_id_of(submission_id: str) -> int | None:
    m = _CHUNK_RE.search(submission_id)
    return int(m.group(1)) if m else None


def _slim_job(j: RayJob, *, stages: list[StageStat] | None = None) -> SlimJob:
    sid = j.submission_id or ""
    return SlimJob(
        submission_id=sid,
        status=j.status,
        # Keep in milliseconds, matching RayJob.start_time everywhere else.
        # The frontend's fmtRuntime expects ms.
        start_time=float(j.start_time) if j.start_time is not None else None,
        chunk_id=_chunk_id_of(sid),
        stages=stages if stages is not None else [],
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
    except RAY_TRANSIENT_ERRORS as exc:
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

    def active_jobs_for(slot: Slot) -> list[RayJob]:
        # ALL active jobs in the lane (not just one). _slot_for classifies via the
        # registry, so "htrflow-…"/"fake-…" map to Slot.HTR alongside "htr-…".
        return [j for j in jobs if (j.submission_id or "") and _slot_for(j.submission_id or "") is slot and j.status in _ACTIVE_STATUSES]

    prefetch_jobs = active_jobs_for(Slot.PREFETCH)
    htr_jobs = active_jobs_for(Slot.HTR)
    # Per-chunk in-flight: a chunk with an active job in its lane is not
    # re-submitted. This is the ONLY guard — real concurrency is Ray/Kueue's job.
    running_pf_chunks = {_chunk_id_of(j.submission_id or "") for j in prefetch_jobs}
    running_htr_chunks = {_chunk_id_of(j.submission_id or "") for j in htr_jobs}

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
        spec = spec_for_submission_id(sid)
        cooldowns.append(
            Cooldown(
                submission_id=sid,
                chunk_id=int(m.group(1)),
                # Pipeline NAME from the registry (slot derivable via
                # PIPELINE_SPECS[name].slot). Unknown ids fall back to the slot
                # value as the name, which still groups correctly below.
                pipeline=spec.name if spec is not None else _slot_for(sid).value,
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

    # Group cooldowns by lane via the submission_id (authoritative) so that
    # htrflow/fake names — which differ from the slot value — still gate the
    # right lane's next pick.
    cooldown_pf = {c.chunk_id for c in cooldowns if _slot_for(c.submission_id) is Slot.PREFETCH}
    cooldown_htr = {c.chunk_id for c in cooldowns if _slot_for(c.submission_id) is Slot.HTR}
    # Eligible = ready, minus chunks already in flight, minus cooldown. The loop
    # submits ALL of these each tick; Ray/Kueue queue whatever doesn't fit.
    eligible_prefetch = [cid for cid in prefetch_pending if cid not in running_pf_chunks and cid not in cooldown_pf]
    eligible_htr = [cid for cid in ready_for_htr if cid not in running_htr_chunks and cid not in cooldown_htr]

    prefetch_slot = await _build_slot(
        http=http,
        client=client,
        dashboard_url=dashboard_url,
        jobs=prefetch_jobs,
        eligible=eligible_prefetch,
        queue_len=len(prefetch_pending),
    )
    htr_slot = await _build_slot(
        http=http,
        client=client,
        dashboard_url=dashboard_url,
        jobs=htr_jobs,
        eligible=eligible_htr,
        queue_len=len(ready_for_htr),
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
    jobs: list[RayJob],
    eligible: list[int],
    queue_len: int,
) -> SlotState:
    """Build a lane's state: every active job (each with its own per-stage
    telemetry, resolved from that job's pipeline spec — so an htrflow job uses
    htrflow's empty stages, not the htr actor names) plus the eligible chunk ids."""
    running: list[SlimJob] = []
    for j in jobs:
        sid = j.submission_id or ""
        stages: list[StageStat] = []
        jid = await _driver_job_id(client, sid)
        if jid:
            stages = await _task_summary_for_job(http=http, dashboard_url=dashboard_url, driver_job_id=jid, stage_names=_stages_for(sid))
        running.append(_slim_job(j, stages=stages))
    return SlotState(running=running, eligible=eligible, queue_len=queue_len)
