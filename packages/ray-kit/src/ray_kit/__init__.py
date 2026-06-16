"""ray-kit — Ray Job SDK + Dashboard HTTP wrapper (schemas, dashboard service,
shared transient-error tuple, client constructor). Used by ray-api and by the
viewer orchestrator. No FastAPI, no viewer, no DB."""

from ray.job_submission import JobSubmissionClient

from ray_kit.dashboard import RAY_TRANSIENT_ERRORS, build_client


__all__ = ["RAY_TRANSIENT_ERRORS", "JobSubmissionClient", "build_client"]
