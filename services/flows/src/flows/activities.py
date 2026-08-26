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
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from flows import executor
from flows.metrics import record_node
from flows.models import NodeJob, NodeResult
from flows.runtime import wfr


log = logging.getLogger(__name__)


@wfr.activity(name="run_node")
def run_node(ctx: WorkflowActivityContext, job: NodeJob) -> dict[str, object]:
    """Execute one node and return its :class:`~flows.models.NodeResult` as a dict.

    THE INPUT IS TYPED (DWF-ACT-009). This docstring used to argue the opposite — "dict in, dict out
    … a plain dict is the shape that survives a model gaining a field" — on replay-compatibility
    grounds. That position is withdrawn, not merely overruled, because both halves of it have moved:
    the owner waived back-compat for this estate (ruling 2026-08-25, "do what best practices"), and
    the SDK now COERCES an activity's input into whatever model its second parameter names
    (`workflow_runtime._coerce_activity_input` → `_model_protocol.coerce_to_model`), so the annotation
    is enforced rather than decorative. `ingest` and `medallion` are typed the same way; leaving this
    paragraph standing would have left the estate carrying two contradictory rules for one seam.

    The OUTPUT stays a dict: it is written to history and then again as the input of every dependent
    node, and `NodeResult` owns the two ceilings that keep it bounded (see below).

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
    result = asyncio.run(_run(job))
    record_node(result.state.status)

    # THE SPAN THAT ALREADY EXISTS, not a new one. daprd emits `activity||run_node` (SERVER) and the SDK
    # emits `activity: run_node` (INTERNAL); opening a third on the same hop would make the trace view
    # worse. Neither can carry what this service knows — the SDK's holds task.instance_id/task.id and
    # daprd's holds its own, so in a 256-span run nothing says WHICH node.
    #
    # The identifiers ride the SPAN and not a metric: a node id is caller-supplied and unbounded, which
    # is exactly the series-per-object-id rule this estate has already been burned by. `flows.nodes`
    # above carries only the closed status.
    span = trace.get_current_span()
    span.set_attribute("lance.flows.node_id", job.node.id)
    span.set_attribute("lance.flows.node_kind", job.node.kind)

    if result.state.status == "failed":
        # daprd marks the ORCHESTRATION span when an activity RAISES — but this one deliberately
        # RETURNS a failed NodeResult (see below), so no `with` block anywhere sees an exception and
        # every activity span stays UNSET. Measured estate-wide: 46/46 SDK activity spans UNSET,
        # including runs whose nodes failed. Trace-based error search shows a clean estate.
        #
        # `set_status` and NOT `record_exception`: there is no exception here, and the Span Event API is
        # being deprecated — the failure detail already rides the log line below and the NodeResult.
        span.set_status(Status(StatusCode.ERROR, f"node {job.node.id} failed: {result.state.error}"))

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
