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
    model_config = ConfigDict(extra="allow")
    submission_id: str | None = None
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


class RayJobsPayload(BaseModel):
    ok: bool
    dashboard_url: str
    jobs: list[RayJob] = Field(default_factory=list)
    error: str | None = None


class RayNode(BaseModel):
    node_id: str | None = None
    node_ip: str | None = None
    alive: bool = False
    resources_total: dict[str, float] = Field(default_factory=dict)
    resources_used: dict[str, float] = Field(default_factory=dict)


class RayClusterPayload(BaseModel):
    ok: bool
    dashboard_url: str
    node_count: int = 0
    alive_count: int = 0
    total_resources: dict[str, float] = Field(default_factory=dict)
    used_resources: dict[str, float] = Field(default_factory=dict)
    nodes: list[RayNode] = Field(default_factory=list)
    error: str | None = None


class ProxyResponse(BaseModel):
    """Forwarded response from the Ray Dashboard — fed into fastapi `Response(...)` at the call site."""

    content: bytes
    status_code: int
    headers: dict[str, str] = Field(default_factory=dict)
