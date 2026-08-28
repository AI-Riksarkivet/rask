"""compute service DI: the Ray Job SDK client + the dashboard HTTP client, from app.state."""

import time
from functools import lru_cache
from typing import Annotated

import httpx
from fastapi import Depends, Request

from compute.config import ComputeSettings
from ray_kit import JobSubmissionClient, build_client


def get_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.http


def get_ray_client(request: Request) -> JobSubmissionClient | None:
    # The client is built once in lifespan, but there is no startup ordering
    # guarantee between the compute service and the Ray head (under k8s it boots first
    # and build_client returns None until the dashboard is reachable — minutes,
    # on a fresh cluster). Rebuild lazily when still unset so /health and /jobs
    # recover on their own once Ray comes up, with no pod restart.
    client = request.app.state.ray_client
    if client is not None:
        return client
    # A None result means Ray is still down, and build_client issues blocking version-check HTTP
    # calls each time it is constructed. Without a cooldown every request would restorm the dashboard
    # (a burst of /ray/health + /ray/jobs each rebuilds); the negative cache holds off a retry until
    # the interval elapses, and the client still self-heals on the first attempt after Ray comes up.
    now = time.monotonic()
    last_attempt = getattr(request.app.state, "ray_client_last_attempt", None)
    cooldown = get_compute_settings().ray_client_retry_cooldown_s
    if last_attempt is not None and now - last_attempt < cooldown:
        return None
    request.app.state.ray_client_last_attempt = now
    client = build_client(request.app.state.settings.ray_dashboard_url)
    request.app.state.ray_client = client
    return client


HttpDep = Annotated[httpx.AsyncClient, Depends(get_http)]
RayClientDep = Annotated[JobSubmissionClient | None, Depends(get_ray_client)]


@lru_cache(maxsize=1)
def get_compute_settings() -> ComputeSettings:
    """The service's own settings, carrying the estate's auth knobs.

    Separate from `app.state.settings` (the generic object the lifespan builds) because
    `make_auth_deps` binds against a TYPE, and the generic one has no auth fields to bind. Built once;
    `lru_cache` on a module-level function, never on a method (writing-python).
    """
    return ComputeSettings()


ComputeSettingsDep = Annotated[ComputeSettings, Depends(get_compute_settings)]
