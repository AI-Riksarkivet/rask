"""ray-api DI: the Ray Job SDK client + the dashboard HTTP client, from app.state."""

from typing import Annotated

import httpx
from fastapi import Depends, Request

from ray_kit import JobSubmissionClient


def get_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.http


def get_ray_client(request: Request) -> JobSubmissionClient | None:
    return request.app.state.ray_client


HttpDep = Annotated[httpx.AsyncClient, Depends(get_http)]
RayClientDep = Annotated[JobSubmissionClient | None, Depends(get_ray_client)]
