"""The cascade's operator surface: observe and stop an in-flight `stage_run` (DWF-MGT-002/003).

THESE ROUTES LIVE ON THE MOVER, and that is forced rather than chosen. `stage_run` executes in the
mover's own runtime (`mover.py`), and both `get_workflow_state` and `terminate_workflow` resolve an
instance through the CALLING app's app-id. A copy of these routes on the producer would look for the
instance under `medallion-producer`, not find it, and — the part that makes it dangerous — **accept
the call anyway**: a 202 for a terminate that stopped nothing. `promotions.py` records the same trap
from the other direction, which is why the promotion workflow is hosted beside its door.

The mover has no gateway row and no Ingress, so it is reached through the producer, which has both:
`producer.py` authenticates the human and forwards here over the mover's ClusterIP. Authorization
happens THERE, at the door a person can reach; this side verifies the service token, exactly like the
mover's event routes.

What terminate does NOT do is stated in the response body. `stage_run` submits a Ray job and then
polls it; terminating stops the WATCH and the next-tier trigger, never the job. An operator told
"terminated" would reasonably free the GPUs in their head, and they are not free.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from service_kit.exceptions import ServiceUnavailableError
from service_kit.governed.dapr_auth import require_dapr_token


router = APIRouter(tags=["stages"])


class StageRunState(BaseModel):
    """A DECLARED field list, not the SDK's state object.

    `WorkflowState` carries the serialized input and output — the whole `StageJobSpec`, including the
    URIs and the lineage blob. A status question must not disclose them to answer.
    """

    instance_id: str
    status: str
    #: Echoed so an operator can cross-check the Ray dashboard for the job the watch is polling.
    submission_id: str | None = None
    polls_done: int = 0


class StageTerminateAccepted(BaseModel):
    instance_id: str
    detail: str


def _client(request: Request) -> Any:
    """The lifespan's client. Constructing one per request re-opens a gRPC channel to the sidecar."""
    client = getattr(request.app.state, "workflow_client", None)
    if client is None:
        raise ServiceUnavailableError("the workflow engine is not available")
    return client


async def _state_or_404(client: Any, instance_id: str, *, payloads: bool) -> Any:
    # The SDK client is SYNCHRONOUS. Awaiting it inline would block the event loop for every other
    # request on this worker — the same reason ingest and flows read their state through a thread.
    state = await asyncio.to_thread(lambda: client.get_workflow_state(instance_id, fetch_payloads=payloads))
    if state is None:
        raise HTTPException(status_code=404, detail=f"no stage run {instance_id!r}")
    return state


@router.get("/stages/{instance_id}")
async def show_stage(instance_id: str, request: Request, _token: Annotated[None, Depends(require_dapr_token)]) -> StageRunState:
    """DWF-MGT-002. Before this, an in-flight cascade stage was unobservable over HTTP entirely —
    `services/compute` proxies Ray read-only and knows nothing about the workflow watching it."""
    state = await _state_or_404(_client(request), instance_id, payloads=True)
    submission_id: str | None = None
    polls = 0
    with suppress(Exception):
        # Best-effort: a spec this build cannot parse must still answer the STATUS question, which is
        # the one the caller asked.
        spec = json.loads(state.serialized_input or "{}")
        submission_id = str(spec.get("submission_id") or "") or None
        polls = int(spec.get("polls_done") or 0)
    return StageRunState(
        instance_id=instance_id,
        status=str(getattr(state.runtime_status, "name", state.runtime_status)),
        submission_id=submission_id,
        polls_done=polls,
    )


@router.post("/stages/{instance_id}/terminate", status_code=202)
async def terminate_stage(instance_id: str, request: Request, _token: Annotated[None, Depends(require_dapr_token)]) -> StageTerminateAccepted:
    """DWF-MGT-003 for the cascade.

    A HARD terminate is right here, and it is worth saying why this differs from ingest, where the
    same finding got a `cancel` EVENT instead: ingest's skipped tail held `emit_terminal`, the only
    caller of `release_run_units`, so terminating stranded the run's JetStream consumer. `stage_run`
    holds no queue and no consumer — it submits, polls, and reports — so there is no cleanup a skipped
    path could leak. The lever an operator actually lacks is the one that stops a wrongly-dispatched
    stage from publishing the next tier's trigger, and that is exactly what this stops.
    """
    client = _client(request)
    await _state_or_404(client, instance_id, payloads=False)
    await asyncio.to_thread(lambda: client.terminate_workflow(instance_id))
    return StageTerminateAccepted(
        instance_id=instance_id,
        detail="the watch stops and no downstream trigger is published; the Ray job it was polling keeps running and must be stopped through Ray",
    )
