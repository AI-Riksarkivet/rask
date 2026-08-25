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


def get_run_reader(request: Request) -> "FlowRunReader | None":
    """The workflow-state reader, or None when no sidecar was found at startup."""
    return request.app.state.workflow_reader


class ScheduleUnconfirmed(RuntimeError):
    """The engine neither confirmed nor denied the instance — the run MAY already exist.

    The one schedule outcome a caller must NOT degrade on: running the graph inline here would run a
    graph the engine is also running. `routes.create_run` answers 503 and asks for a retry with the
    same `Idempotency-Key`, which derives the same instance id and therefore converges rather than
    forking.

    Declared beside `FlowScheduler` rather than beside its only raiser (`lifespan.DaprFlowScheduler`)
    because it is part of the SEAM's contract, not of one implementation: `routes.create_run`
    branches on it, so an implementer that cannot see it here would collapse the three outcomes into
    two and hand the route back the ambiguity the type exists to remove.
    """


class FlowScheduler:
    """The seam the routes schedule through. Implemented in `lifespan.py`; declared here as a
    Protocol-shaped base so a test can substitute one without a sidecar.

    `schedule` has THREE outcomes, and `routes.create_run` branches on all three — an implementation
    that collapses them is how one run executes twice:

    * **returns** — the instance EXISTS. Nothing may run inline.
    * **raises `ScheduleUnconfirmed`** — unknown, and only a retry can resolve it. The route refuses
      (503) rather than degrading.
    * **raises anything else** — the engine did NOT create it. This is the only outcome that may
      degrade to the inline lane.

    "The call raised" is not one of them: a timeout is evidence about the WAIT, never about the
    engine. See `lifespan.DaprFlowScheduler` for why that distinction is load-bearing.
    """

    async def schedule(self, run_id: str, payload: dict[str, object]) -> None:
        raise NotImplementedError


class FlowRunReader:
    """Reads a durable run's live state back OUT of the workflow engine.

    The scheduler's counterpart, and it was missing — which made the durable lane write-only.
    `app.state.runs` is written by `_remember` and read by `get_run`, and in the durable lane the
    only thing ever written is `RunState(status="running")` with an empty `nodes` map. So a durable
    run that COMPLETED still read as running forever, a FAILED one never surfaced its error, the
    record was evicted once 200 newer runs arrived, and a pod restart turned every prior run into a
    404 — while the workflow itself was executing, or had long finished, perfectly well.

    The engine already holds all of it: `flow_run_workflow` RETURNS `RunState.model_dump()`, so the
    instance's `serialized_output` IS the answer. Nothing needs to be persisted here; the dict stays
    the cache its docstring claims to be, which is exactly the argument `ingest.runs` makes for the
    same seam (`record_from_workflow_state`).

    A class rather than a `typing.Protocol` to match `FlowScheduler` above — one seam style per
    module beats a marginally better one used inconsistently.
    """

    def state(self, run_id: str) -> dict[str, object] | None:
        raise NotImplementedError

    def terminate(self, run_id: str) -> None:
        """Stop scheduling further work for a run. Sync, like `state`, and driven through a thread.

        On the READER rather than a separate seam because both answer the same question — "reach the
        engine about one run" — and a second class would be a second thing to wire, stub and forget.
        """
        raise NotImplementedError


FlowsSettingsDep = Annotated[FlowsSettings, Depends(get_flows_settings)]
HttpDep = Annotated[httpx.AsyncClient, Depends(get_http)]
RunsDep = Annotated[dict[str, RunState], Depends(get_runs)]
SchedulerDep = Annotated[FlowScheduler | None, Depends(get_scheduler)]
RunReaderDep = Annotated[FlowRunReader | None, Depends(get_run_reader)]
