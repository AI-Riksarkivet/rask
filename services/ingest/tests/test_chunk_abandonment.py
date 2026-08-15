"""§2.4 — an abandoned fan-out must be STOPPED before its queue is reclaimed.

THE DEFECT. Two paths walk away from a live fan-out: the run deadline, and the error boundary. Both
then call `emit_terminal`, which releases the run's JetStream subject and DELETES the per-run durable
(`runtime.release_run_units` -> `queue.release_run`) — the exact consumer every live `drain_chunk` is
pulling from. The run abandoned its children and then pulled the queue out from under them.

IT GOT WORSE WHEN §2.3 LANDED. `when_all` completes on the FIRST failed child (the SDK's own comment
says late arrivals are ignored), so the error boundary is reached while every SIBLING is still
draining. Before the boundary existed only a run outliving `RASK_INGEST_MAX_RUN_HOURS` could reach
that state; afterwards one permanently-bad object does. A fix created the reachable version of an
older defect — which is why the ordering is pinned here rather than left to review.

WHAT IS NOT CLAIMED. `terminate_workflow` cannot stop an activity already executing; the SDK is
explicit ("there is no way to terminate an in-flight activity execution"). So this bounds the window
to one activity's runtime instead of the rest of the run. Whether a drain that finishes against a
deleted consumer fails loudly or silently is an open question and needs a live cluster.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
from ingest.workflow import ingest_run, terminate_chunks


if TYPE_CHECKING:
    from dapr.ext.workflow import DaprWorkflowContext


SPEC: dict[str, Any] = {"run_id": "r-abandon", "kind": "test-src", "project": "p1", "dataset": "pages", "options": {}}
#: A ceiling low enough that the deadline branch is taken, resolved as an activity result.
LIMITS: dict[str, Any] = {"max_run_hours": 1.0, "max_units": 0}
HANDLE: dict[str, Any] = {"location": "s3://b/t.lance", "read_version": 1}


class _Task:
    #: `when_all`/`when_any` assign and READ `_parent` on their children, so a bare object is not a
    #: sufficient stand-in for a task — declared here rather than discovered as an AttributeError
    #: raised from inside the SDK.
    _parent: Any = None

    def __init__(self) -> None:
        self.result: Any = None
        self.is_complete = False

    def get_result(self) -> Any:
        return self.result


class _Ctx:
    """Records the activity call ORDER, which is the whole subject of this file."""

    def __init__(self) -> None:
        self.activities: list[tuple[str, Any]] = []
        self.child_ids: list[str | None] = []
        self.timer: _Task | None = None
        self.instance_id = "r-abandon"

    def call_activity(self, fn: Any, *, input: Any = None, retry_policy: Any = None) -> _Task:  # noqa: A002
        self.activities.append((fn.__name__, input))
        return _Task()

    def call_child_workflow(self, fn: Any, *, input: Any = None, instance_id: str | None = None) -> _Task:  # noqa: A002
        self.child_ids.append(instance_id)
        return _Task()

    def create_timer(self, _delta: Any) -> _Task:
        # Held, because the workflow decides the deadline branch by IDENTITY (`winner is deadline`),
        # so the test has to be able to hand the same object back.
        self.timer = _Task()
        return self.timer

    def set_custom_status(self, status: str) -> None: ...


def _drive_to_fanout(ctx: _Ctx, chunks: list[dict[str, Any]]) -> Any:
    gen = ingest_run(cast("DaprWorkflowContext", ctx), SPEC)
    gen.send(None)  # -> emit_start
    gen.send(None)  # -> resolve_limits
    gen.send(LIMITS)  # -> ensure_dataset
    gen.send(HANDLE)  # -> enumerate_chunks
    gen.send(chunks)  # -> the fan-in (and the deadline timer)
    return gen


def test_every_child_is_given_a_deterministic_id() -> None:
    """Without an `instance_id` the runtime mints one, and a parent that abandons the fan-out has no
    NAME for what it is walking away from — it could not stop them even in principle."""
    ctx = _Ctx()
    _drive_to_fanout(ctx, [{"keys": ["a"]}, {"keys": ["b"]}, {"keys": ["c"]}])

    assert ctx.child_ids == ["r-abandon-c0", "r-abandon-c1", "r-abandon-c2"]


def test_the_error_boundary_stops_the_children_BEFORE_releasing_the_queue() -> None:
    """THE REGRESSION, in the shape §2.3 made reachable: one bad chunk, siblings still draining."""
    ctx = _Ctx()
    gen = _drive_to_fanout(ctx, [{"keys": ["a"]}, {"keys": ["b"]}])

    gen.throw(RuntimeError("Activity task #3 failed: object 404"))

    names = [n for n, _ in ctx.activities]
    assert "terminate_chunks" in names, f"the fan-out was abandoned without being stopped — calls were {names}"
    assert names.index("terminate_chunks") < len(names), "terminate_chunks must precede the terminal emit"
    assert names[-1] == "terminate_chunks", "the queue release must not have been reached yet"
    # And it is asked to stop exactly the children it started.
    _, payload = ctx.activities[-1]
    assert payload["child_ids"] == ["r-abandon-c0", "r-abandon-c1"]


def test_the_DEADLINE_path_stops_the_children_too() -> None:
    """The original §2.4 path. It was the only way to reach the defect before §2.3; it is still a way."""
    ctx = _Ctx()
    gen = _drive_to_fanout(ctx, [{"keys": ["a"]}])
    # `when_any` returns the DEADLINE task — the workflow compares identity, so hand back the timer
    # the fake context handed out.
    gen.send(ctx.timer)

    names = [n for n, _ in ctx.activities]
    assert names[-1] == "terminate_chunks", f"the deadline released the queue without stopping the fan-out — calls were {names}"
    assert ctx.activities[-1][1]["child_ids"] == ["r-abandon-c0"]


def test_a_failure_with_no_children_yet_does_not_dispatch_a_pointless_stop() -> None:
    """The boundary opens at `emit_start`, long before the fan-out, so most failures reaching it have
    no children — and an activity that would immediately return still costs a real history event."""
    ctx = _Ctx()
    gen = ingest_run(cast("DaprWorkflowContext", ctx), SPEC)
    gen.send(None)
    gen.send(None)

    gen.throw(RuntimeError("resolve_limits exhausted its four attempts"))

    names = [n for n, _ in ctx.activities]
    assert "terminate_chunks" not in names, f"a childless failure dispatched a stop — calls were {names}"
    assert names[-1] == "emit_terminal"


def test_terminate_chunks_is_best_effort_and_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """It runs while a run is already terminating. A tidy-up that fails must not turn a run that
    recorded its outcome into one that died — the rule `release_run_units` and the lineage emit already
    follow. Terminating an already-finished child is the NORMAL race, not an error.

    Patched on the SDK module's attribute rather than by swapping `sys.modules`: the activity imports
    `dapr.ext.workflow` locally, and replacing the entry in the module table leaks into every test that
    imports it afterwards — which is exactly the pollution this file caused before it was written this
    way.
    """
    import dapr.ext.workflow as wf_sdk

    attempted: list[str] = []

    class _Boom:
        def terminate_workflow(self, instance_id: str) -> None:
            attempted.append(instance_id)
            raise RuntimeError(f"instance {instance_id} is already terminal")

    monkeypatch.setattr(wf_sdk, "DaprWorkflowClient", _Boom)

    out = terminate_chunks(cast(Any, object()), {"child_ids": ["a", "b"]})

    assert out == {"terminated": 0, "requested": 2}, "a failing terminate must be counted, not raised"
    assert attempted == ["a", "b"], "one child failing must not stop the others being tried"


def test_terminate_chunks_counts_what_it_actually_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The count is what an operator reads to know how much of the fan-out was still live."""
    import dapr.ext.workflow as wf_sdk

    class _Half:
        def terminate_workflow(self, instance_id: str) -> None:
            if instance_id.endswith("1"):
                raise RuntimeError("already terminal")

    monkeypatch.setattr(wf_sdk, "DaprWorkflowClient", _Half)

    assert terminate_chunks(cast(Any, object()), {"child_ids": ["c0", "c1", "c2"]}) == {"terminated": 2, "requested": 3}


def test_no_children_is_a_cheap_no_op() -> None:
    assert terminate_chunks(cast(Any, object()), {"child_ids": []}) == {"terminated": 0, "requested": 0}
