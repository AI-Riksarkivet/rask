"""ray-kit — Ray Job SDK + Dashboard HTTP wrapper (schemas, dashboard service,
shared transient-error tuple, client constructor). Used by the ray service (the viewer
orchestrator, its other consumer, died at P7a). No FastAPI, no viewer, no DB."""

from ray.job_submission import JobSubmissionClient

from ray_kit import dashboard, metrics, submit
from ray_kit.auth import auth_headers
from ray_kit.dashboard import RAY_TRANSIENT_ERRORS, build_client


__all__ = ["RAY_TRANSIENT_ERRORS", "JobSubmissionClient", "auth_headers", "build_client", "dashboard", "metrics", "submit"]
