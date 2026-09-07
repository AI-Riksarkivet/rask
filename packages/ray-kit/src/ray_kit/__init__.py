"""ray-kit — the Ray SDK + Dashboard HTTP wrapper (schemas, dashboard service, shared
transient-error tuple, client constructor). Its one consumer is `services/compute`, the estate's
Ray-facing service. No FastAPI, no viewer, no DB.

THE SUBMIT KERNEL IS NOT HERE. `submit.py` was pure HTTPX and its only consumer was the medallion,
which paid for this package's `ray[default]` dependency to reach it — so it moved to
`medallion.services.ray_jobs_api`, that service's Ray adapter, and medallion now declares no compute
engine at all. What stays is the half that genuinely needs the SDK."""

from ray.job_submission import JobSubmissionClient

from ray_kit import dashboard, metrics
from ray_kit.auth import auth_headers
from ray_kit.dashboard import RAY_TRANSIENT_ERRORS, build_client


__all__ = ["RAY_TRANSIENT_ERRORS", "JobSubmissionClient", "auth_headers", "build_client", "dashboard", "metrics"]
