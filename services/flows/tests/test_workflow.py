"""The durable lane, without a sidecar: what gets REGISTERED, and what the orchestrator decides.

Both halves are worth pinning, and the estate has already paid for the first one. `ingest` shipped an
in-cluster deploy whose sidecar logged `Workflow engine started` and still could not run a workflow,
because the APP side had registered nothing — an asymmetry that looks healthy from every angle except
an actual run. A registration test is two lines and closes that whole class.

The second half drives the orchestrator as the GENERATOR it is: a fake context records what it was
asked to call and hands back activity results, so the wave plan and the upstream-failure rule are
exercised with no engine, no sidecar and no network. That is the only way this file gets tested at all
short of a cluster — and the rule it must agree with (a failed node blocks its dependents, and the
blocked node is never dispatched) is the same one `test_executor.py` pins for the inline lane. A
sandbox whose answer depends on which orchestrator ran it is a sandbox nobody can trust.
"""

from typing import Any

import dapr.ext.workflow as wf
import pytest

from flows.models import NodeResult, NodeRunState, RunJob, RunState


SERVE = "http://serve.test:8000"

JOB = RunJob(
    graph={  # type: ignore[arg-type]  — pydantic coerces the dicts; writing the models out adds nothing
        "nodes": [
            {"id": "t", "kind": "text"},
            {"id": "m", "kind": "model", "config": {"app": "htrflow"}},
            {"id": "a", "kind": "alto"},
            {"id": "s", "kind": "inspect"},
        ],
        "edges": [
            {"source": "t", "target": "m"},
            {"source": "m", "target": "a"},
            {"source": "a", "target": "s"},
        ],
    },
    seeds={"t": "seed"},
    serve_url=SERVE,
)


def _orchestrator():
    """The real generator function behind `@wfr.workflow`.

    `WorkflowRuntime.workflow` does NOT return the decorated function: it registers it and returns a
    zero-argument accessor (`workflow_runtime.py:399-411` — `def innerfn(): return fn`, with the
    original's `__signature__` copied onto it, which is why calling it with `(ctx, payload)` fails with
    "takes 0 positional arguments" while the signature says otherwise). Production code never notices,
    because `ctx.call_activity` and `schedule_new_workflow` both resolve the reference through the
    `_dapr_alternate_name` the accessor carries. A test that drives the body directly does notice, and
    unwrapping in one named place is better than four call sites with a bare `()`.
    """
    from flows.workflow import flow_run_workflow

    return flow_run_workflow()


class _FakeContext:
    """Records every `call_activity` and answers nothing — the results come back through `send`."""

    instance_id = "run-wf"
    is_replaying = False

    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []

    def call_activity(self, activity: object, *, input: dict[str, Any], retry_policy: object = None) -> tuple[str, dict[str, Any]]:  # noqa: A002
        self.dispatched.append(input)
        return ("task", input)


def test_importing_the_lane_registers_the_workflow_and_its_activity() -> None:
    # Imported for the registration side effect — the decorators ARE the registration.
    import flows.activities
    import flows.workflow
    from flows.runtime import wfr

    assert flows.workflow.NODE_RETRY is not None  # the imports are load-bearing, not incidental
    assert flows.activities.log is not None

    # getattr, because the name is PRIVATE AND MANGLED (`self.__worker` inside WorkflowRuntime) — the
    # SDK exposes no public reader for its own registry, and a dotted access would be an attribute the
    # type checker is right to say does not exist. This is the only place the test reaches inside.
    registry = getattr(wfr, "_WorkflowRuntime__worker")._registry  # noqa: B009
    assert "flow_run" in registry.orchestrators
    assert "run_node" in registry.activities


def test_the_orchestrator_fans_out_per_wave_and_blocks_on_upstream_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    # when_all is the engine's fan-in primitive; here it only has to preserve the task list so the
    # driver can see what one wave asked for.
    monkeypatch.setattr(wf, "when_all", lambda tasks: ("when_all", tasks))

    ctx = _FakeContext()
    generator = _orchestrator()(ctx, JOB.model_dump())

    waves: list[list[str]] = []
    outcome: dict[str, Any] = {}
    reply: list[dict[str, Any]] | None = None
    while True:
        try:
            yielded = generator.send(reply)  # None on the first send, by the generator protocol
        except StopIteration as stop:
            outcome = stop.value
            break
        assert yielded[0] == "when_all"
        jobs = [task[1] for task in yielded[1]]
        waves.append([job["node"]["id"] for job in jobs])
        reply = [_result(job) for job in jobs]

    # Two waves dispatched, then nothing: `a` and `s` were blocked by `m`'s failure and never sent.
    assert waves == [["t"], ["m"]]
    assert [job["node"]["id"] for job in ctx.dispatched] == ["t", "m"]

    state = RunState.model_validate(outcome)
    assert state.run_id == "run-wf"  # the run id IS the workflow instance id
    assert state.status == "failed"
    assert state.nodes["t"].status == "succeeded"
    assert state.nodes["m"].error == "serve returned 500 for app 'htrflow'"
    assert state.nodes["a"].error == "upstream failed"
    assert state.nodes["s"].error == "upstream failed"
    assert state.nodes["a"].ms is None


def test_the_orchestrator_refuses_a_cycle_before_dispatching_anything() -> None:
    job = RunJob(
        graph={  # type: ignore[arg-type]
            "nodes": [{"id": "a", "kind": "text"}, {"id": "b", "kind": "inspect"}],
            "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
        },
        serve_url=SERVE,
    )
    ctx = _FakeContext()
    generator = _orchestrator()(ctx, job.model_dump())

    with pytest.raises(StopIteration) as stop:
        generator.send(None)

    state = RunState.model_validate(stop.value.value)
    assert state.status == "failed"
    assert state.error == "graph has a cycle"
    assert ctx.dispatched == []


def _result(job: dict[str, Any]) -> dict[str, Any]:
    """The activity's answer, as the engine would hand it back: the model node fails, the rest pass."""
    if job["node"]["kind"] == "model":
        return NodeResult(state=NodeRunState(status="failed", ms=1.0, error="serve returned 500 for app 'htrflow'")).model_dump()
    return NodeResult(
        state=NodeRunState(status="succeeded", ms=1.0, output_text="ok"),
        payload_text="ok",
    ).model_dump()
