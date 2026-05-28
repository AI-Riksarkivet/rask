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
    ray_version: str | None = None
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
