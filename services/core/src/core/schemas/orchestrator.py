from pydantic import BaseModel, Field
from ray.dashboard.modules.job.common import JobStatus

from core.models.enums import RayStage


class StageStat(BaseModel):
    stage: RayStage
    finished: int
    running: int
    scheduled: int
    pending: int
    failed: int
    total: int


class SlimJob(BaseModel):
    submission_id: str
    status: JobStatus | None = None
    start_time: float | None = None
    chunk_id: int | None = None
    # Per-stage task counts for THIS job (from /api/v0/tasks/summarize), resolved
    # via the job's own pipeline spec — htrflow → empty, not the htr actor names.
    stages: list[StageStat] = Field(default_factory=list)


class SlotState(BaseModel):
    # All active (RUNNING/PENDING) jobs in this lane. The viewer no longer caps
    # concurrency — Ray/Kueue queue what doesn't fit the cluster's resources.
    running: list[SlimJob] = Field(default_factory=list)
    # Chunk ids ready to submit now: ready, minus in-flight, minus cooldown.
    # The loop submits ALL of these each tick.
    eligible: list[int] = Field(default_factory=list)
    # Total ready chunks in the lane (informational; len(eligible) <= queue_len).
    queue_len: int


class Cooldown(BaseModel):
    submission_id: str
    chunk_id: int
    # Pipeline NAME (registry key in core.models.pipelines.PIPELINE_SPECS),
    # e.g. "prefetch" / "htr" / "htrflow". The concurrency lane is derivable via
    # PIPELINE_SPECS[pipeline].slot.
    pipeline: str
    expires_in_secs: int
    # Why the job failed (from Ray's JobDetails) so the cooldown card is
    # self-explanatory instead of just "FAILED". driver_exit_code == 137 = OOM.
    error_type: str | None = None
    message: str | None = None
    driver_exit_code: int | None = None


class OrchestratorState(BaseModel):
    ok: bool
    running: bool = False
    error: str | None = None
    prefetch: SlotState | None = None
    htr: SlotState | None = None
    cooldowns: list[Cooldown] = Field(default_factory=list)
    ready_threshold: float | None = None
    cooldown_secs: int | None = None


class OrchestratorControlResponse(BaseModel):
    """Response shape for POST /orchestrator/start and /stop."""

    running: bool
