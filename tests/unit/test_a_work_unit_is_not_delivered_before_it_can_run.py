"""The work queue may not hold more units in flight than the workers can actually execute.

[[LH-188]] measured the live lane: `Max Ack Pending: 1,000` — NATS's DEFAULT, because the component
sets none — against an execution ceiling of `anyio`'s default thread limiter, 40 per pod. So up to a
thousand units may be DELIVERED while at most `40 x replicas` can be RUNNING.

THE HARM IS THE ACK WINDOW, NOT THE QUEUE DEPTH. `ackWait` runs from DELIVERY, not from the start of
execution, so a unit sitting behind others burns its 720s window while idle. When it expires the
broker redelivers, and a redelivered compaction runs TWICE on one dataset — the exact race the
in-process single-flight lock exists to prevent, reintroduced by the broker. Observed on demand: a
manual sweep took `Ack Pending` 181 -> 721 and `Redelivered` 0 -> 1.

SO THE BOUND IS DERIVED, NOT PICKED. Deliver no more than can run, and a delivered unit's window
starts when it can actually start:

    maxAckPending == maxConcurrentUnits x worker replicas

That needs `maxConcurrentUnits` to be a DECISION rather than `anyio`'s default, which is why the
worker sets the limiter from its own setting: a bound derived from a library default is a bound
nobody chose, and an upstream change would silently break the equality this gate asserts.

WHAT THIS DOES **NOT** FIX, stated because the row's two halves fail independently and only one is
answered here: it does not reduce the MEMORY exposure. `maxConcurrentUnits` still admits 40
compactions per pod, and sizing that number needs a per-unit memory figure this estate cannot yet
produce — no tier has run a compaction that rewrites fragments. The value is PROVISIONAL and the row
says so; what changes is that it is now a value someone chose, in one place, with the delivery bound
following from it.
"""

from __future__ import annotations

from tests.unit.chart_render import DEFAULT_ARGS, render


WORKER_ENV = "MAINTENANCE_MAX_CONCURRENT_UNITS"


def _components(docs: tuple[dict, ...]) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d.get("kind") == "Component"}


def _meta(component: dict) -> dict[str, str]:
    """A Dapr component's `spec.metadata` list as a mapping."""
    return {entry["name"]: str(entry.get("value", "")) for entry in component["spec"]["metadata"]}


def _work_component(docs: tuple[dict, ...]) -> dict:
    found = [c for name, c in _components(docs).items() if "maintenance-work" in name or _meta(c).get("name") == "lance-dapr-maintenance-work"]
    assert found, f"no maintenance work pubsub component rendered; components were {sorted(_components(docs))}"
    return found[0]


def _index_component(docs: tuple[dict, ...]) -> dict:
    found = [c for name, c in _components(docs).items() if "maintenance-index" in name or _meta(c).get("name") == "lance-dapr-maintenance-index"]
    assert found, f"no maintenance index pubsub component rendered; components were {sorted(_components(docs))}"
    return found[0]


def test_THE_TWO_LANES_SHARE_ONE_POOL_AND_THEIR_BOUNDS_SUM_TO_IT() -> None:
    """The equality that matters, and it is a SUM rather than a per-lane match.

    `api/work.py` and `api/index_work.py` both reach `run_in_threadpool`, whose limiter is
    process-GLOBAL. So bounding each lane to `maxConcurrentUnits x replicas` would admit TWICE what
    the fleet can run — the defect these bounds exist to remove, reintroduced by counting the same
    capacity twice. Asserted as a sum so a future third lane cannot quietly over-subscribe it either.
    """
    docs = render(*DEFAULT_ARGS)
    work = int(_meta(_work_component(docs))["maxAckPending"])
    index = int(_meta(_index_component(docs))["maxAckPending"])
    workers = [d for d in docs if d.get("kind") == "Deployment" and "maintenance-worker" in d["metadata"]["name"]]
    assert workers, "no maintenance-worker Deployment rendered"
    replicas = int(workers[0]["spec"]["replicas"])
    env = {e["name"]: str(e.get("value", "")) for c in workers[0]["spec"]["template"]["spec"]["containers"] for e in c.get("env", [])}
    assert WORKER_ENV in env, f"the worker carries no {WORKER_ENV}, so its execution ceiling is anyio's default rather than a decision"
    capacity = int(env[WORKER_ENV]) * replicas
    assert work + index == capacity, (
        f"the lanes may deliver {work} + {index} = {work + index} units while the fleet can run "
        f"{env[WORKER_ENV]} x {replicas} = {capacity}. They share ONE thread limiter, so the bounds must "
        "split that capacity, not each claim it."
    )
    assert index > 0 and work > 0, f"one lane was given the whole pool (work={work}, index={index}); both must be able to make progress"
