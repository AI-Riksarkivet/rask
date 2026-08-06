"""The run as a Dapr Workflow — the durable lane.

    flow_run                      (one per POST /flows/runs, when a sidecar is present)
      for each topological wave:
        when_all(run_node ...)     activity per node, fanned out across the wave

**Determinism, and why this file can be read as deterministic at a glance.** A workflow body is
REPLAYED from history on every continuation, so the same code must make the same decisions given the
same history. Everything here is either pure (`topo_waves`, `upstreams` from `flows.graph`, both
I/O-free by contract) or a `yield` on the durable history. There is no clock — node timing is
measured inside the activity, which runs once and whose result is persisted. There is no `uuid4` —
the run id is the workflow INSTANCE id, minted by the caller. There is no HTTP — the Serve call is
the activity's job. Break any of those and the run does not fail loudly; it wedges.

The waves are recomputed on each replay rather than persisted, which is safe precisely because
`topo_waves` sorts each wave: the plan is a pure function of the input graph, so replay derives a
byte-identical one.
"""

from collections.abc import Generator
from datetime import timedelta
from typing import Any

import dapr.ext.workflow as wf

from flows.activities import run_node
from flows.graph import topo_waves, upstreams
from flows.models import NodeJob, NodeResult, NodeRunState, RunJob, RunState
from flows.runtime import wfr


#: One retry sweep for a node. Dapr owns the backoff and the replay; a hand-rolled loop in the
#: workflow body would be both non-deterministic and unable to survive a worker restart.
#:
#: Retries here cover the activity FAILING (a worker died mid-call), not a node reporting failure:
#: `activities.run_node` returns a failed NodeResult instead of raising precisely so that a refused
#: node is not retried four times.
NODE_RETRY = wf.RetryPolicy(
    first_retry_interval=timedelta(seconds=2),
    max_number_of_attempts=3,
    backoff_coefficient=2.0,
)


@wfr.workflow(name="flow_run")
def flow_run_workflow(ctx: wf.DaprWorkflowContext, payload: dict[str, Any]) -> Generator[Any, Any, dict[str, Any]]:
    """Execute a flow graph wave by wave. Input is a serialized :class:`RunJob`; the return is a
    serialized :class:`RunState`.

    Typed as a Generator because it IS one — every `yield` is a durable await point. Annotating it
    `-> dict` (the shape it conceptually returns) is a lie the type checker catches.
    """
    request = RunJob.model_validate(payload)

    waves = topo_waves(request.graph)
    if waves is None:
        return RunState(run_id=ctx.instance_id, status="failed", error="graph has a cycle").model_dump()

    by_id = {node.id: node for node in request.graph.nodes}
    incoming = upstreams(request.graph)
    states: dict[str, NodeRunState] = {}
    outputs: dict[str, str] = {}

    for wave in waves:
        runnable: list[str] = []
        for node_id in wave:
            blocked = [u for u in incoming[node_id] if states.get(u) is None or states[u].status == "failed"]
            if blocked:
                # Same rule as the inline lane: a failed node blocks its dependents, which are
                # recorded rather than dispatched. The builder must paint the node that broke.
                states[node_id] = NodeRunState(status="failed", error="upstream failed")
            else:
                runnable.append(node_id)

        if not runnable:
            # Every node in this wave was blocked. `when_all([])` on an empty task list is not a
            # contract worth relying on — and there is nothing to await — so skip the yield. Skipping
            # is replay-safe because it is decided by the same pure computation on every replay.
            continue

        tasks = [
            ctx.call_activity(
                run_node,
                input=NodeJob(
                    node=by_id[node_id],
                    inputs=[outputs[u] for u in incoming[node_id]],
                    seed=request.seeds.get(node_id),
                    serve_url=request.serve_url,
                    serve_timeout=request.serve_timeout,
                ).model_dump(),
                retry_policy=NODE_RETRY,
            )
            for node_id in runnable
        ]
        results = yield wf.when_all(tasks)

        for node_id, raw in zip(runnable, results, strict=True):
            result = NodeResult.model_validate(raw)
            states[node_id] = result.state
            if result.payload_text is not None:
                outputs[node_id] = result.payload_text

    failed = any(s.status == "failed" for s in states.values())
    return RunState(
        run_id=ctx.instance_id,
        status="failed" if failed else "succeeded",
        nodes=states,
    ).model_dump()
