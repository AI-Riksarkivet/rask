"""The work queue must admit enough units in flight to drain what the sweep plans each tick.

MEASURED THE HARD WAY, 2026-09-22. Bounding the worker's thread limiter to the memory-safe figure
cut the lane's delivery bound with it — they were derived from one number — and the lane stalled:

    Max Ack Pending: 6
    Outstanding Acks: 6 out of maximum 6
    Unprocessed Messages: 2,128 -> 4,285     (growing, tick after tick)
    346 outcomes in 5 minutes                 = 1.15 units/sec against 4.7 injected

Nothing caught it. The sibling gate ties the lanes' bounds to the fleet's execution capacity, which
kept them CONSISTENT while both shrank — consistency is not adequacy, and a lane can be perfectly
proportioned and still too small for the work arriving.

So this compares the two halves that actually decide it, and they live in different files:

    units needed in flight  =  expectedDatasets / scheduleSeconds  x  secondsPerUnit

`secondsPerUnit` is a measurement (6 in flight -> 1.15 units/sec -> ~5.2s, rounded up), and most units
are no-ops of two HTTP calls, so the seconds are round-trips and governance checks rather than work.

DELIBERATELY A FLOOR, not an equality. Over-provisioning delivery costs a pointer per queued unit —
the units are claim-check POINTERS, not payloads — while under-provisioning grows a backlog without
bound. The two errors are not symmetric and the gate is not either.
"""

from __future__ import annotations

import math
import re

import pytest
import yaml

from tests.unit.chart_render import DEFAULT_ARGS, REPO, render


def _components(docs: tuple[dict, ...]) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Component"}


def _meta(component: dict) -> dict[str, str]:
    return {e["name"]: str(e.get("value", "")) for e in component["spec"]["metadata"]}


def _lane(docs: tuple[dict, ...], marker: str) -> dict:
    found = [c for name, c in _components(docs).items() if marker in name or _meta(c).get("name") == f"lance-dapr-{marker}"]
    assert found, f"no {marker} component rendered; components were {sorted(_components(docs))}"
    return found[0]


def _schedule_seconds(docs: tuple[dict, ...]) -> int:
    """The sweep cron's interval, from the rendered binding rather than from a constant here."""
    bindings = [d for d in docs if d.get("kind") == "Component" and d["spec"].get("type", "").startswith("bindings.cron")]
    for binding in bindings:
        if "maintenance-cron" not in binding["metadata"]["name"]:
            continue
        schedule = _meta(binding).get("schedule", "")
        found = re.search(r"@every\s+(\d+)s", schedule)
        assert found, f"the maintenance cron schedule {schedule!r} is not an @every Ns form this gate can read"
        return int(found.group(1))
    pytest.skip("no maintenance cron binding rendered")
    raise AssertionError  # unreachable; keeps the return type honest


def test_the_inputs_are_all_rendered() -> None:
    """An unreadable input would make the comparison below vacuous rather than false."""
    docs = render(*DEFAULT_ARGS)
    assert int(_meta(_lane(docs, "maintenance-work"))["maxAckPending"]) > 0
    assert _schedule_seconds(docs) > 0


def test_the_lane_can_keep_up_with_its_own_sweep() -> None:
    """Delivery capacity must cover the drain rate the sweep's own cadence demands."""
    docs = render(*DEFAULT_ARGS)
    work = int(_meta(_lane(docs, "maintenance-work"))["maxAckPending"])
    interval = _schedule_seconds(docs)

    values = (render.__module__,)  # noqa: F841  - documents that the numbers below come from the chart
    import yaml

    from tests.unit.chart_render import REPO  # local import: only this leg needs the raw values file

    chart_values = yaml.safe_load((REPO / "chart/values.yaml").read_text())
    datasets = int(chart_values["maintenance"]["expectedDatasets"])
    seconds_per_unit = float(chart_values["maintenance"]["secondsPerUnit"])

    required_rate = datasets / interval
    needed_in_flight = math.ceil(required_rate * seconds_per_unit)

    assert work >= needed_in_flight, (
        f"the sweep plans ~{datasets} units every {interval}s ({required_rate:.1f}/sec) and a unit takes "
        f"~{seconds_per_unit}s, so ~{needed_in_flight} must be in flight to keep up — the work lane admits "
        f"{work}. Measured 2026-09-22 with 6: the lane drained 1.15/sec and `Unprocessed Messages` grew "
        "past 4,000. Raise `maxConcurrentUnits` (the throughput knob) or lengthen the sweep schedule; the "
        "MEMORY bound is `maxConcurrentCompactions` and is not this number."
    )


@pytest.mark.parametrize(("in_flight", "sufficient"), [(6, False), (72, True)])
def test_the_gate_REFUSES_the_bound_that_stalled_the_lane(in_flight: int, sufficient: bool) -> None:
    """A gate that cannot fail is not a gate — so the number that actually stalled it must be refused."""
    chart_values = yaml.safe_load((REPO / "chart/values.yaml").read_text())
    interval = _schedule_seconds(render(*DEFAULT_ARGS))
    needed = math.ceil(int(chart_values["maintenance"]["expectedDatasets"]) / interval * float(chart_values["maintenance"]["secondsPerUnit"]))
    assert (in_flight >= needed) == sufficient, f"{in_flight} in flight against {needed} needed: expected sufficient={sufficient}"
