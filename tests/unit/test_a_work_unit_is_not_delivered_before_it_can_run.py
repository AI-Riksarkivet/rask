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

import pathlib
import re

import pytest

from tests.unit.chart_render import DEFAULT_ARGS, render


REPO = pathlib.Path(__file__).resolve().parents[2]
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


def _script(job: dict) -> str:
    """Every argv token of a Job's containers as one string — the shell script is the last of them."""
    return " ".join(part for c in job["spec"]["template"]["spec"]["containers"] for part in [*c.get("command", []), *c.get("args", [])])


def test_the_work_component_renders_at_all() -> None:
    """An empty render would make every leg below vacuously true."""
    assert _work_component(render(*DEFAULT_ARGS))


def test_the_work_queue_DECLARES_a_bound() -> None:
    """A component that sets none inherits NATS's 1,000, which nobody chose."""
    meta = _meta(_work_component(render(*DEFAULT_ARGS)))
    assert "maxAckPending" in meta, (
        "the maintenance work component sets no `maxAckPending`, so the lane runs on NATS's default of "
        "1,000 units in flight against an execution ceiling two orders of magnitude lower — and a unit "
        f"delivered but queued burns its {meta.get('ackWait', 'ackWait')} window while idle. Set it from "
        "`maintenance.dedicatedWorkers.maxConcurrentUnits` x replicas."
    )


def test_the_bound_EQUALS_what_the_workers_can_run() -> None:
    """The equality IS the design: deliver no more than can run, so no window opens on an idle unit."""
    docs = render(*DEFAULT_ARGS)
    meta = _meta(_work_component(docs))
    workers = [d for d in docs if d.get("kind") == "Deployment" and "maintenance-worker" in d["metadata"]["name"]]
    assert workers, "no maintenance-worker Deployment rendered"
    replicas = int(workers[0]["spec"]["replicas"])
    env = {e["name"]: str(e.get("value", "")) for c in workers[0]["spec"]["template"]["spec"]["containers"] for e in c.get("env", [])}
    assert WORKER_ENV in env, f"the worker carries no {WORKER_ENV}, so its execution ceiling is anyio's default rather than a decision"
    assert int(meta["maxAckPending"]) == int(env[WORKER_ENV]) * replicas, (
        f"maxAckPending={meta['maxAckPending']} but the fleet can run {env[WORKER_ENV]} x {replicas} units. "
        "Delivering more than can run is what lets a unit burn its ack window queued behind others."
    )


def test_the_WORKER_actually_enforces_the_number_it_is_given() -> None:
    """A chart value the process ignores is a bound nobody applies — the gap this estate keeps finding.

    Asserted against the SOURCE rather than by importing and reading the limiter, because the limiter is
    process-global: a test that set it would change every later test in the same worker.
    """
    source = (REPO / "services/maintenance/src/maintenance/service.py").read_text(encoding="utf-8")
    assert re.search(r"current_default_thread_limiter\(\)\.total_tokens\s*=", source), (
        "the maintenance service never sets anyio's thread limiter, so `max_concurrent_units` is "
        "configuration the process does not apply and the real ceiling stays the library default of 40"
    )


@pytest.mark.parametrize("flag", ["maintenance.dedicatedWorkers.maxConcurrentUnits"])
def test_the_value_is_declared_where_an_operator_can_find_it(flag: str) -> None:
    """Beside the replica count it is multiplied with, not buried in a template."""
    assert flag.rsplit(".", 1)[-1] in (REPO / "chart/values.yaml").read_text(encoding="utf-8"), f"{flag} is not declared in values.yaml"


def test_an_EXISTING_consumer_is_converged_too() -> None:
    """The component sets the bound at CREATION only, so a live estate needs someone to converge it.

    MEASURED 2026-09-22: adding `maxAckPending` to the component left the deployed durable — created
    2026-09-03 — on NATS's default of 1,000 while the chart, the rendered Component and the legs above
    all said 80. Deleting the durable and letting Dapr rebuild it gave `Max Ack Pending: 80`, so the key
    is honoured and only at creation. Without this step the gate is green on an estate that is not.
    """
    job = [d for d in render(*DEFAULT_ARGS) if d.get("kind") == "Job" and "nats-stream" in d["metadata"]["name"]]
    assert job, "no nats-stream Job rendered"
    script = _script(job[0])
    assert "converge_max_ack_pending MAINTENANCE_WORK" in script, (
        "the stream Job never converges the work durable's max_ack_pending, so an estate whose consumer "
        "predates the setting keeps the old bound silently and forever — the chart says 80 and the "
        "broker does 1,000"
    )


def test_the_JOB_and_the_COMPONENT_agree_on_the_number() -> None:
    """Two spellings of one bound is how they drift; both come from `lance.maintenanceMaxAckPending`."""
    docs = render(*DEFAULT_ARGS)
    component = int(_meta(_work_component(docs))["maxAckPending"])
    job = next(d for d in docs if d.get("kind") == "Job" and "nats-stream" in d["metadata"]["name"])
    script = _script(job)
    found = re.search(r"converge_max_ack_pending MAINTENANCE_WORK \S+ (\d+)", script)
    assert found, "the converge call carries no number"
    assert int(found.group(1)) == component, f"the Job converges to {found.group(1)} while the component sets {component}"
