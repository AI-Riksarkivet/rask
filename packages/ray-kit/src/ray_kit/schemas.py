"""Schemas for `/api/ray/*` responses.

`RayJob` mirrors Ray's `JobDetails` payload (transported as a dict) plus two
viewer-specific augmentations — `batches` and `logs_url`. We can't subclass
`JobDetails` directly because Ray ships it as a Pydantic V1 model, and the
rest of the viewer's schemas are V2; mixing V1 models inside V2 containers
breaks validation. `extra='allow'` keeps every upstream field accessible;
the explicit declarations below cover the subset the viewer actually reads.
"""

from pydantic import BaseModel, ConfigDict, Field
from ray.dashboard.modules.job.common import JobStatus


class RayHealth(BaseModel):
    ok: bool
    dashboard_url: str
    # The viewer's own Ray client version, NOT the cluster's — the SDK's
    # get_version() returns the Jobs-API version, and the server Ray version
    # isn't worth an extra round-trip here. Named honestly to avoid implying
    # it's the cluster version (relevant under client/server skew on KubeRay).
    client_ray_version: str | None = None
    error: str | None = None


class RayJob(BaseModel):
    # `extra="ignore"`, NOT "allow". Ray's `JobDetails` carries `runtime_env` (the job's full pip/uv
    # list, env vars and working_dir refs), `metadata` (an arbitrary user dict) and `driver_info`;
    # `list_jobs` feeds the whole `d.dict()` in, so "allow" RETAINED all of it on every row while the
    # declared eleven scalars are the only fields any surface reads. Verified against the SPA before
    # narrowing: `frontend/microfrontends/compute` + `@rask/api` reference exactly the declared set.
    model_config = ConfigDict(extra="ignore")
    submission_id: str | None = None
    # Ray's driver job id, distinct from submission_id; carried via extra="allow"
    # but declared so the SPA can fall back to it as a row key.
    job_id: str | None = None
    status: JobStatus | None = None
    entrypoint: str | None = None
    start_time: int | None = None
    end_time: int | None = None
    batches: list[str] = Field(default_factory=list)
    logs_url: str | None = None
    # Failure cause — already on Ray's JobDetails and carried through `d.dict()`
    # + extra="allow", just declared here so it's typed and surfaced. exit 137 =
    # SIGKILL (host-RAM OOM), the dominant silent HTR failure.
    error_type: str | None = None
    message: str | None = None
    driver_exit_code: int | None = None


class RayJobFailure(BaseModel):
    """Ray's own account of WHY a job ended badly — the three fields `RayJob` has always declared.

    Split from `RayJob` because the two answer different questions and are read on different paths.
    `RayJob` is a LIST row (the jobs board, capped and sorted); this is a single job's post-mortem,
    read once on a terminal-bad outcome. A caller that only needs the cause should not have to
    construct, validate and carry the other eight fields to get at it.

    `extra="ignore"` for the same reason `RayJob` sets it: Ray's `JobDetails` carries `runtime_env`
    (the job's full pip list, env vars and working-dir refs) and an arbitrary user `metadata` dict, and
    retaining either here would put a job's whole environment into whatever this is rendered into.
    """

    model_config = ConfigDict(extra="ignore")
    error_type: str | None = None
    #: Ray's driver message. UNBOUNDED — this can be an entire traceback, so every consumer passes a
    #: limit to `summary()` rather than interpolating it directly.
    message: str | None = None
    #: The driver's process exit code. 137 is SIGKILL, i.e. the host-RAM OOM that kills a stage with no
    #: other symptom; 1 is an ordinary Python exception. Distinguishing them is the whole point.
    driver_exit_code: int | None = None

    def summary(self, message_limit: int) -> str:
        """A one-line cause, with the message bounded by the CALLER's limit.

        The limit is an argument rather than a constant here because the ceiling belongs to whatever
        this string is being put INTO — a lineage event published through a claim-check funnel has a
        different budget from a log line — and `ray-kit` has no business knowing which.

        Ordered type-first so truncation can only ever eat the free-text tail: the error TYPE and the
        exit CODE are what classify a failure, and a cap that swallowed them would leave the caller
        with a long string that says less than the short one it replaced. Empty when Ray reported
        nothing, so a caller can tell "no cause available" from "the cause is blank".
        """
        parts: list[str] = []
        if self.error_type:
            parts.append(self.error_type)
        if self.message:
            flat = " ".join(self.message.split())
            parts.append(flat if len(flat) <= message_limit else f"{flat[:message_limit]}… (truncated)")
        rendered = ": ".join(parts)
        if self.driver_exit_code is not None:
            suffix = f"driver exit {self.driver_exit_code}"
            rendered = f"{rendered} ({suffix})" if rendered else suffix
        return rendered


class RayJobsPayload(BaseModel):
    ok: bool
    dashboard_url: str
    jobs: list[RayJob] = Field(default_factory=list)
    error: str | None = None
    #: How many jobs the cluster reported, before `dashboard.MAX_JOBS` capped the list.
    total: int = 0
    #: True when `jobs` is a newest-first PREFIX of `total`. Ray's `GET /api/jobs` takes no
    #: parameters, so the cap is ours and the caller has no other way to know it was applied —
    #: without this a truncated list reads as the whole cluster.
    truncated: bool = False


class RayGpu(BaseModel):
    """Real per-GPU hardware telemetry (from the node's `gpus[]`), distinct from
    Ray's logical GPU reservation."""

    index: int | None = None
    uuid: str | None = None
    name: str | None = None
    utilization_percent: float | None = None
    memory_used_mb: float | None = None
    memory_total_mb: float | None = None
    temperature_c: float | None = None


class RayNode(BaseModel):
    node_id: str | None = None
    node_ip: str | None = None
    hostname: str | None = None
    node_type: str | None = None  # worker-group / tier, e.g. gpu-ada, gpu-a5000
    is_head: bool = False
    alive: bool = False
    # Ray's LOGICAL accounting — what actors/tasks have reserved.
    resources_total: dict[str, float] = Field(default_factory=dict)
    resources_used: dict[str, float] = Field(default_factory=dict)
    # REAL host telemetry — what the hardware is actually doing.
    host_cpu_percent: float | None = None
    host_mem_total: float | None = None
    host_mem_used: float | None = None
    gpus: list[RayGpu] = Field(default_factory=list)


class RayClusterPayload(BaseModel):
    ok: bool
    dashboard_url: str
    node_count: int = 0
    alive_count: int = 0
    total_resources: dict[str, float] = Field(default_factory=dict)
    used_resources: dict[str, float] = Field(default_factory=dict)
    nodes: list[RayNode] = Field(default_factory=list)
    error: str | None = None


class RayActor(BaseModel):
    """A Ray actor, merged from the state API (`/api/v0/actors`) and the
    dashboard's `/logical/actors` (live process/GPU telemetry + task counts)."""

    actor_id: str | None = None
    class_name: str = ""
    name: str | None = None
    repr_name: str | None = None
    state: str = ""
    pid: int | None = None
    node_id: str | None = None
    job_id: str | None = None
    ray_namespace: str | None = None
    num_restarts: int = 0
    is_detached: bool = False
    placement_group_id: str | None = None
    required_resources: dict[str, float] = Field(default_factory=dict)
    # First line of the death cause / exit detail, DEAD actors only.
    death_reason: str | None = None
    # Live telemetry from /logical/actors — absent when that endpoint is unreachable.
    start_time_ms: int | None = None
    end_time_ms: int | None = None
    cpu_percent: float | None = None
    rss_bytes: float | None = None
    num_fds: int | None = None
    gpu_util: float | None = None
    gpu_mem_mb: float | None = None
    num_executed_tasks: int | None = None
    num_running_tasks: int | None = None
    num_pending_tasks: int | None = None
    task_queue_length: int | None = None
    ip_address: str | None = None
    worker_id: str | None = None


class RayActorsPayload(BaseModel):
    ok: bool
    dashboard_url: str
    actors: list[RayActor] = Field(default_factory=list)
    error: str | None = None


class RayTask(BaseModel):
    """A Ray task from the state API (`/api/v0/tasks`)."""

    task_id: str | None = None
    name: str | None = None
    func_or_class_name: str | None = None
    type: str | None = None  # NORMAL_TASK | ACTOR_TASK | ACTOR_CREATION_TASK | ...
    state: str = ""
    job_id: str | None = None
    actor_id: str | None = None
    node_id: str | None = None
    worker_pid: int | None = None
    attempt_number: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    required_resources: dict[str, float] = Field(default_factory=dict)
    creation_time_ms: int | None = None
    start_time_ms: int | None = None
    end_time_ms: int | None = None


class RayTasksPayload(BaseModel):
    ok: bool
    dashboard_url: str
    tasks: list[RayTask] = Field(default_factory=list)
    error: str | None = None


class RayEvent(BaseModel):
    event_id: str | None = None
    severity: str = "INFO"
    message: str = ""
    time: str | None = None
    source_type: str | None = None


class RayOverviewPayload(BaseModel):
    ok: bool
    dashboard_url: str
    ray_version: str | None = None
    session_name: str | None = None
    events: list[RayEvent] = Field(default_factory=list)
    error: str | None = None


class RayJobLogsPayload(BaseModel):
    ok: bool
    submission_id: str
    logs: str = ""
    error: str | None = None


class RayLogsPayload(BaseModel):
    """Log file listing (when `filename` omitted) or a file's tail."""

    ok: bool
    node_id: str | None = None
    filename: str | None = None
    files: dict[str, list[str]] = Field(default_factory=dict)
    text: str | None = None
    error: str | None = None


class ProxyResponse(BaseModel):
    """Forwarded response from the Ray Dashboard — fed into fastapi `Response(...)` at the call site."""

    content: bytes
    status_code: int
    headers: dict[str, str] = Field(default_factory=dict)
