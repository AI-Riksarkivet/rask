"""The workflow's activities — the side of the durable lane that is ALLOWED to do I/O.

Exactly one activity, `run_node`, because a run's only real work is executing nodes; the plan
(topological waves) is pure and therefore belongs in the orchestrator, not in an activity.

It delegates to `executor.run_node`, the same function the inline lane calls. That is deliberate: a
sandbox whose answer depends on which orchestrator ran it is a sandbox nobody can trust.
"""

import asyncio
import logging

import httpx
from dapr.ext.workflow import WorkflowActivityContext

from flows import executor
from flows.metrics import record_node
from flows.models import NodeJob, NodeResult
from flows.runtime import wfr


log = logging.getLogger(__name__)


@wfr.activity(name="run_node")
def run_node(ctx: WorkflowActivityContext, activity_input: dict[str, object]) -> dict[str, object]:
    """Execute one node and return its :class:`~flows.models.NodeResult` as a dict.

    Dict in, dict out — not the pydantic models directly. 1.18 can serialize a model, but the
    activity's input and output are what land in the workflow's durable HISTORY: a replay after a
    deploy must be able to read a payload written by the previous version, and a plain dict is the
    shape that survives a model gaining a field.

    `asyncio.run` is correct HERE and would be wrong almost anywhere else in the service: a Dapr
    activity is invoked SYNCHRONOUSLY on a worker thread the runtime owns, so there is no event loop
    to hijack — unlike the request path, where an `asyncio.run` would try to nest inside uvicorn's.
    The client is per-activity for the same reason: the worker thread has no app-scoped one, and an
    activity may run in a process that never served a request.

    The returned dict is bounded, and it has to be: this value is written to the workflow history and
    then again as the input of every dependent node, so an unbounded payload costs O(dependents)
    writes to the state store. Both ceilings live on the model (`models.NodeResult`), not here — a
    cap applied at one of its three construction sites is a cap that holds at one of three.
    """
    job = NodeJob.model_validate(activity_input)
    result = asyncio.run(_run(job))
    record_node(result.state.status)
    if result.state.status == "failed":
        # Logged, not raised. Raising would make Dapr retry the activity and — after the retries —
        # fail the whole workflow, when a failed node is a NORMAL outcome of a sandbox run that the
        # builder paints red. Retrying an "image seeds are frontend-only" refusal four times helps
        # nobody.
        log.info("node %s failed: %s", job.node.id, result.state.error)
    return result.model_dump()


async def _run(job: NodeJob) -> NodeResult:
    # The per-call budget rides the job (`serve_timeout`), so the client only needs a connect bound.
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=job.serve_timeout)) as client:
        return await executor.run_node(job, client=client)
