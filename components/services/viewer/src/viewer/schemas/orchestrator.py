from pydantic import BaseModel, Field
from ray.dashboard.modules.job.common import JobStatus

from viewer.models.enums import Pipeline, RayStage


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


class SlotState(BaseModel):
    running: SlimJob | None = None
    next: int | None = None
    queue_len: int
    stages: list[StageStat] = Field(default_factory=list)


class Cooldown(BaseModel):
    submission_id: str
    chunk_id: int
    pipeline: Pipeline
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
