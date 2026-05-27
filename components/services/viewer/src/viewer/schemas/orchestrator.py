from pydantic import BaseModel

from viewer.models.batch import Pipeline


class StageStat(BaseModel):
    stage: str
    finished: int
    running: int
    scheduled: int
    pending: int
    failed: int
    total: int


class SlimJob(BaseModel):
    submission_id: str
    status: str | None = None
    start_time: float | None = None
    chunk_id: int | None = None


class SlotState(BaseModel):
    running: SlimJob | None = None
    next: int | None = None
    queue_len: int
    stages: list[StageStat] = []


class Cooldown(BaseModel):
    submission_id: str
    chunk_id: int
    pipeline: Pipeline
    expires_in_secs: int


class OrchestratorState(BaseModel):
    ok: bool
    error: str | None = None
    prefetch: SlotState | None = None
    htr: SlotState | None = None
    cooldowns: list[Cooldown] = []
    ready_threshold: float | None = None
    cooldown_secs: int | None = None
