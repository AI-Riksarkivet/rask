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

from collections.abc import Iterable

from opentelemetry import metrics
from opentelemetry.metrics import CallbackOptions, Observation


#: The two lanes a run can execute on. Declared HERE because this module owns the label vocabulary —
#: `record_run` and `record_lane` both spend it, and `lifespan` and `routes` both name it. A second
#: copy of these two strings is how a startup gauge and a per-run counter come to disagree about what
#: "durable" is called.
DURABLE = "durable"
INLINE = "inline"

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


#: Which lane THIS PROCESS settled on at startup, published by `lifespan` through `record_lane`.
#: Module state rather than a counter argument because a gauge is observed on the collector's
#: schedule, not on ours.
_startup_lane = INLINE


def _observe_lane(_options: CallbackOptions) -> Iterable[Observation]:
    """Report the startup lane as a 1/0 gauge, labelled with the lane it settled on."""
    yield Observation(1 if _startup_lane == DURABLE else 0, {"lance.flows.lane": _startup_lane})


_meter.create_observable_gauge(
    "flows.durable_lane",
    callbacks=[_observe_lane],
    unit="{lane}",
    description="1 when this process started the durable workflow lane, 0 when it fell back to inline.",
)


def record_lane(lane: str) -> None:
    """Publish the lane this process started on — the answer to "durable expected, inline actual".

    A GAUGE, not a counter, and it is the one flows signal that is useful on a service with no
    traffic at all: `record_run` can only speak once somebody runs a flow, so a deployment whose
    sidecar never came up looked identical to one nobody had used. The startup decision was
    previously announced only as a log line, which meant an operator had to already suspect the
    problem to find it. Paired with `lifespan`'s ERROR line, which carries the exception this gauge
    cannot.
    """
    # One process-wide fact, read by the observable-gauge callback above.
    global _startup_lane
    _startup_lane = lane
