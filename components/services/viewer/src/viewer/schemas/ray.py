"""Schemas for `/api/ray/*` responses.

We import Ray's own Pydantic types where they exist (`JobDetails`, `JobStatus`,
`JobType`, `DriverInfo`) so the API contract follows upstream. `RayJob` is a
thin subclass of `JobDetails` that adds viewer-specific augmentations:

  - `batches` — list parsed from the entrypoint's `--batch <id>` args.
  - `logs_url` — dashboard deep-link to the job's logs page.
"""

from pydantic import BaseModel, Field
from ray.dashboard.modules.job.pydantic_models import JobDetails


class RayHealth(BaseModel):
    ok: bool
    dashboard_url: str
    ray_version: str | None = None
    error: str | None = None


class RayJob(JobDetails):
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
