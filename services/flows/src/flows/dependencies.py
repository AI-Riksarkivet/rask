"""flows service DI — everything the routes need, read off `app.state` where the lifespan put it.

Same shape as `compute.dependencies`: `Annotated[X, Depends(getter)]` aliases, so a route signature
names the resource and nothing else. Reading `app.state` in a route body instead would make the
route untestable without a full app.
"""

from typing import Annotated

import httpx
from fastapi import Depends, Request

from flows.config import FlowsSettings
from flows.models import RunState


def get_flows_settings(request: Request) -> FlowsSettings:
    return request.app.state.flows_settings


def get_http(request: Request) -> httpx.AsyncClient:
    return request.app.state.http


def get_runs(request: Request) -> dict[str, RunState]:
    """The process-local run store.

    A dict, and explicitly v0: it does not survive a restart and it is not shared across replicas.
    That is honest for the inline lane — an inline run is over before the response is written, so
    there is nothing to recover — and it is superseded rather than fixed by the durable lane, where
    the workflow's own history is the record. A Dapr state store here would be a third writer of a
    state that already has an owner.
    """
    return request.app.state.runs


def get_scheduler(request: Request) -> "FlowScheduler | None":
    """The Dapr workflow scheduler, or None when no sidecar was found at startup."""
    return request.app.state.workflow_scheduler


class FlowScheduler:
    """The seam the routes schedule through. Implemented in `lifespan.py`; declared here as a
    Protocol-shaped base so a test can substitute one without a sidecar."""

    async def schedule(self, run_id: str, payload: dict[str, object]) -> None:
        raise NotImplementedError


FlowsSettingsDep = Annotated[FlowsSettings, Depends(get_flows_settings)]
HttpDep = Annotated[httpx.AsyncClient, Depends(get_http)]
RunsDep = Annotated[dict[str, RunState], Depends(get_runs)]
SchedulerDep = Annotated[FlowScheduler | None, Depends(get_scheduler)]
