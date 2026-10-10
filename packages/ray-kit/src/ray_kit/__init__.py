"""ray-kit — the Ray SDK + Dashboard HTTP wrapper (schemas, dashboard service, shared
transient-error tuple, client constructor). Its one consumer is `services/compute`, the estate's
Ray-facing service. No FastAPI, no viewer, no DB.

`jobs_api` is the Jobs-API kernel (submit-or-reattach, status, cause) compute's engine door speaks; the lakehouse
reaches it only through that door (CP-056)."""

from ray.job_submission import JobSubmissionClient

from ray_kit import dashboard, metrics
from ray_kit.auth import auth_headers
from ray_kit.dashboard import RAY_TRANSIENT_ERRORS, build_client


__all__ = ["RAY_TRANSIENT_ERRORS", "JobSubmissionClient", "auth_headers", "build_client", "dashboard", "metrics"]
