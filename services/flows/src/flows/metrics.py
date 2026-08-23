"""Domain metrics for the flow runner — the two facts nothing else can carry.

WHY THIS SERVICE NEEDED ONE AT ALL. `flows` emitted no failure signal of any kind: no lineage, no
control event, no metric, and `workflow.py` has no logger. The only record of a failed node was an
INFO line in the activity carrying no run id. So "did any flow run fail in the last hour, and whose?"
had no answer on any surface.

Dapr's own workflow families cannot substitute, for two independent reasons. The runner RETURNS
failure — `flow_run` yields `RunState(status="failed")` and `run_node` returns a failed `NodeResult`
rather than raising — so the orchestrator completes normally and the sidecar records
`status="success"` for a run in which every node died. And the INLINE lane has no workflow at all, so
no Dapr family observes it even in principle.

Bounded labels only: the lane and the node status are closed sets owned by this service. Run ids, node
ids and error text stay on spans and logs — `models.NodeResult` already carries them.
"""

from __future__ import annotations

from opentelemetry import metrics


_meter = metrics.get_meter("lance.flows")

_runs = _meter.create_counter(
    "flows.runs",
    unit="{run}",
    description="Flow runs by the lane that executed them (durable|inline).",
)
_nodes = _meter.create_counter(
    "flows.nodes",
    unit="{node}",
    description="Flow nodes by terminal status — the service's only failure signal.",
)


def record_run(lane: str) -> None:
    """Count one run by the lane that executed it.

    The lane is decided PER REQUEST (`routes.py`, on whether a scheduler is present) but announced only
    ONCE PER PROCESS at startup, so without this "did THIS run execute durably or inline?" is
    unanswerable — and the two are not comparable: a durable schedule can burn seconds of engine
    round-trip while an inline text graph returns in milliseconds, so a latency series that mixes them
    is worse than no series.
    """
    _runs.add(1, {"lance.flows.lane": lane})


def record_node(status: str) -> None:
    """Count one node by terminal status.

    Not labelled by node id or kind: an id is caller-supplied and unbounded, and the kind belongs on the
    span where the per-node detail already lives. `status` is the closed set `models.NodeRunState`
    owns.
    """
    _nodes.add(1, {"lance.flows.status": status})
