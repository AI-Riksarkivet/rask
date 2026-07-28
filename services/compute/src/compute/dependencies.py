"""compute service DI: the Ray Job SDK client + the dashboard HTTP client, from app.state."""

from typing import Annotated

import httpx
from fastapi import Depends, Request

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
    if client is None:
        client = build_client(request.app.state.settings.ray_dashboard_url)
        request.app.state.ray_client = client
    return client


HttpDep = Annotated[httpx.AsyncClient, Depends(get_http)]
RayClientDep = Annotated[JobSubmissionClient | None, Depends(get_ray_client)]
