"""One dead chunk must leave a FAIL record, not erase the run's bookkeeping.

THE HOLE. `ingest_run` yielded its fan-in bare: a chunk that exhausted its retries raised straight
out of `when_all`, and the workflow died BEFORE `finalize`, before the FAIL lineage record, and
before the queue release that rides `emit_terminal`. A run with one permanently bad object lost
everything an operator needs: the START emitted at accept stayed orphaned in the graph forever, the
queue's units leaked until stream retention, and the only trace was a bare Dapr failure with no
reason attached. On a large backfill — the run shape this plane advertises — one poisoned object
out of a million would erase the bookkeeping of the other 999,999.

HOW THESE TESTS WORK — the generator protocol IS the runtime's protocol. A Dapr workflow function
is a generator; the runtime drives it with `.send(result)` for each yielded task and `.throw(exc)`
when a yielded task FAILED (the vendored runtime re-raises the recorded failure into the generator
at the yield point, identically on every replay). So driving the generator by hand is not a
simulation of the runtime — it is the same protocol, minus persistence. That makes these behavioral
tests of the boundary, where the plane's other workflow gates are source-shape assertions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
from ingest.workflow import RunOutcome, RunSpec, TerminalInput, ingest_run


if TYPE_CHECKING:
    from dapr.ext.workflow import DaprWorkflowContext


_FANOUT: list[Any] = []


def _remember_fanout(_tasks: Any) -> Any:
    """`when_all`'s result, kept so a test can hand the fan-in its chunk results.

    The workflow now reads them via `fanout.get_result()` rather than from the value sent into
    the yield, because the fan-in races cancellation and the sent value is the WINNER."""
    task = _Task()
    _FANOUT.append(task)
    return task


def _recorded(payload: object) -> dict[str, Any]:
    """An activity input as the RUNTIME records it: JSON, never the model instance.

    Activities declare Pydantic inputs now (DWF-ACT-009) and the SDK coerces on the worker side, so a
    workflow body hands `call_activity` a MODEL. History still holds the serialized form, so a fake
    context that stored the instance would let assertions read attributes the real recorded payload
    does not have — a double diverging from the thing it doubles.
    """
    dump = getattr(payload, "model_dump", None)
    if callable(dump):
        dumped = dump(mode="json")
        return dumped if isinstance(dumped, dict) else {}
    return payload if isinstance(payload, dict) else {}


class _Task:
    """A stand-in for a durabletask task — identity is all `when_any` comparisons use."""

    def __init__(self) -> None:
        self.result: Any = None

    def get_result(self) -> Any:
        return self.result


def _finish_fanout(gen: Any, results: list[dict[str, Any]]) -> Any:
    """Complete the fan-out with `results`, then let the race resolve to it.

    The fan-in now races cancellation, so the value sent into the yield is the WINNER and the chunk
    results are read from `fanout.get_result()`. Sending them directly would make the workflow treat
    a list of chunk results as the winning task.
    """
    _FANOUT[-1].result = results
    return gen.send(_FANOUT[-1])


class _Ctx:
    """Records the activity calls the workflow makes, in order, with their inputs.

    Structural, and `cast` to the declared `DaprWorkflowContext` at each call site — this plane's own
    idiom (`conftest.py`'s `activity_ctx`, and `test_pipelines_registry.py`'s `cast(JobSubmissionClient,
    ...)` before it). There is no real context to construct: `DaprWorkflowContext` wraps a
    `durabletask` orchestration context that exists only inside a running worker, and the generator
    protocol above is the whole of what `ingest_run` uses `ctx` for.
    """

    def __init__(self) -> None:
        self.activities: list[tuple[str, dict[str, Any]]] = []
        self.statuses: list[str] = []

    def call_activity(self, fn: Any, *, input: Any = None, retry_policy: Any = None) -> _Task:  # noqa: A002 — the runtime's own keyword
        self.activities.append((getattr(fn, "__name__", str(fn)), _recorded(input)))
        return _Task()

    # `instance_id` is accepted (and unused — this fake returns a completed task) because the REAL
    # `DaprWorkflowContext.call_child_workflow` takes it, and `ingest_run` now passes it so an
    # abandonment path can name the children it must stop (§2.4). A fake that omits a parameter the
    # code under test supplies fails as a TypeError swallowed by the error boundary — which reads as
    # a product bug, not a fixture gap.
    def call_child_workflow(self, fn: Any, *, input: Any = None, instance_id: str | None = None) -> _Task:  # noqa: A002
        return _Task()

    def wait_for_external_event(self, _name: str) -> _Task:
        """The cancellation seam. `terminate` raises this instead of killing the instance, so the
        run reaches its own cleanup — a fake that omits it fails as an AttributeError swallowed by
        the error boundary, which reads as a product bug rather than a fixture gap."""
        return _Task()

    def create_timer(self, _delta: Any) -> _Task:
        return _Task()

    def set_custom_status(self, status: str) -> None:
        self.statuses.append(status)


SPEC = {"run_id": "boundary-test", "kind": "s3-prefix", "project": "bind86", "dataset": "pages", "options": {}}

#: What `resolve_limits` hands back for a deployment that opted into neither ceiling. Sent as an
#: activity RESULT rather than set as env, which is the point of the fix these tests now run against:
#: the workflow branches on history, so a test drives the branch by driving the history.
NO_LIMITS = {"max_run_hours": 0.0, "max_units": 0}

#: What `ensure_dataset` hands back — the location AND the base version the run commits against.
HANDLE = {"location": "s3://wh/loc", "read_version": 7}


@pytest.fixture(autouse=True)
def _plain_fanout(monkeypatch: pytest.MonkeyPatch) -> None:
    """`wf.when_all` wraps tasks in runtime bookkeeping these stand-ins do not carry; the workflow
    only ever YIELDS its return value, so a bare task is a faithful double at this seam."""
    from ingest import workflow as wf_module

    monkeypatch.setattr(wf_module.wf, "when_all", _remember_fanout)
    monkeypatch.setattr(wf_module.wf, "when_any", lambda tasks: _Task())


def _drive_to_fanout(ctx: _Ctx) -> Any:
    """Drive to the fan-in yield: emit_start -> resolve_limits -> ensure_dataset -> enumerate -> fanout."""
    gen = ingest_run(cast("DaprWorkflowContext", ctx), SPEC)
    gen.send(None)  # start -> yields emit_start
    gen.send(None)  # emit_start done -> yields resolve_limits
    gen.send(NO_LIMITS)  # ceilings, from history -> yields ensure_dataset
    gen.send(HANDLE)  # location + base version -> yields enumerate_chunks
    # One chunk of two units -> the workflow computes units_total=2 and yields the fan-in.
    gen.send([{"keys": ["a", "b"]}])
    return gen


@pytest.mark.parametrize(
    ("recorded", "step"),
    [
        ([], "resolve_limits"),
        ([NO_LIMITS], "ensure_dataset"),
        ([NO_LIMITS, HANDLE], "enumerate_chunks"),
    ],
)
def test_EVERY_step_between_the_START_and_the_terminal_is_inside_the_boundary(recorded: list[Any], step: str) -> None:
    """The boundary begins immediately after `emit_start`, and F12c is why it had to.

    It used to open below `enumerate_chunks`, which was survivable only while that activity could
    merely DEGRADE — its anti-join swallowed an unreadable bronze and continued with an empty set.
    Making that a refusal (`AntiJoinUnavailable`, so a transient object-store error cannot silently
    re-land an entire tier) put a raise ABOVE the boundary, which reproduces the boundary's own
    defect one activity earlier: the run dies with the START emitted at accept orphaned in the graph
    forever and no reason anywhere an operator looks.

    So the property is structural rather than per-activity — nothing between the START and the
    terminal may exit unrecorded — and it is pinned for all three pre-fan-out steps.
    """
    ctx = _Ctx()
    gen = ingest_run(cast("DaprWorkflowContext", ctx), SPEC)
    gen.send(None)  # -> emit_start
    gen.send(None)  # emit_start done -> the first step under test
    for result in recorded:
        gen.send(result)
    assert ctx.activities[-1][0] == step

    gen.throw(RuntimeError(f"{step} exhausted its four attempts"))

    assert ctx.activities[-1][0] == "emit_terminal", f"a permanently-failed {step} died without a FAIL record — calls were {[n for n, _ in ctx.activities]}"
    outcome = ctx.activities[-1][1]["outcome"]
    assert outcome["status"] == "FAILED"
    assert step in outcome["errors"]["run"], "the FAIL record must carry the reason, not a bare failure"
    assert outcome["units_total"] == 0, "nothing was enumerated, and zero is the honest count to report"


def test_a_failed_chunk_reaches_emit_terminal_with_a_FAIL_outcome() -> None:
    """The regression. `.throw()` at the fan-in is exactly what the runtime does when a child
    workflow's failure is replayed; the boundary must convert it into the ONE terminal step."""
    ctx = _Ctx()
    gen = _drive_to_fanout(ctx)

    # The runtime throws the RECORDED child failure into the generator at the yield point.
    gen.throw(RuntimeError("Activity task #8 failed: catalog refused describe (403)"))

    # STOP THE CHILDREN FIRST (§2.4). `when_all` completes on the FIRST failed child, so this
    # boundary is reached while siblings are still draining — and `emit_terminal` deletes the
    # JetStream durable they are pulling from. The order is the fix, so it is asserted, not skipped.
    names = [n for n, _ in ctx.activities]
    assert names[-1] == "terminate_chunks", f"the boundary released the queue without stopping the fan-out — calls were {names}"
    gen.send({"terminated": 1, "requested": 1})

    # The generator must now be suspended on emit_terminal — not dead.
    names = [n for n, _ in ctx.activities]
    assert names[-1] == "emit_terminal", f"the failure did not route to the terminal step — calls were {names}"

    _, payload = ctx.activities[-1]
    outcome = payload["outcome"]
    assert outcome["status"] == "FAILED"
    assert "catalog refused describe" in outcome["errors"]["run"], "the FAIL record must carry the reason, not a bare failure"
    assert outcome["units_total"] == 2, "the enumerated total must survive into the FAIL record"

    # Completing emit_terminal ends the run with the FAILED outcome as its return value.
    with pytest.raises(StopIteration) as stop:
        gen.send(None)
    assert stop.value.value["status"] == "FAILED"


def test_a_PERMANENTLY_REFUSED_finalize_also_leaves_a_FAIL_record() -> None:
    """`tests/test_empty_commit.py` documented this leg verbatim: a finalize that burns its retries
    killed the workflow before its own FAIL record. The boundary must cover it identically."""
    ctx = _Ctx()
    gen = _drive_to_fanout(ctx)

    # Fan-in SUCCEEDS: one chunk result, no errors -> the workflow proceeds to finalize.
    _finish_fanout(gen, [{"chunk_id": "c0", "units_done": 2, "fragments": [], "errors": {}}])
    assert ctx.activities[-1][0] == "finalize"

    # finalize exhausts its retries -> the runtime throws the recorded failure.
    gen.throw(RuntimeError("commit conflict on bronze$pages at read_version 7"))

    # The §2.4 child-stop runs here TOO, and that is deliberate rather than incidental: the fan-in
    # completing does not prove every child is finished — `when_all` returns on the first FAILURE, and
    # a chunk that failed leaves siblings live. One ordering for every abandonment path is easier to
    # hold than a rule about which of them may skip it.
    assert ctx.activities[-1][0] == "terminate_chunks"
    gen.send({"terminated": 0, "requested": 1})

    assert ctx.activities[-1][0] == "emit_terminal"
    outcome = ctx.activities[-1][1]["outcome"]
    assert outcome["status"] == "FAILED"
    assert "commit conflict" in outcome["errors"]["run"]


def test_the_SUCCESS_path_is_untouched_by_the_boundary() -> None:
    """The boundary must not change what a healthy run does: fan-in -> finalize -> terminal, with
    finalize's outcome — not a synthesized one — as the terminal payload and the return value."""
    ctx = _Ctx()
    gen = _drive_to_fanout(ctx)

    _finish_fanout(gen, [{"chunk_id": "c0", "units_done": 2, "fragments": [], "errors": {}}])  # fan-in result -> finalize
    ok = {"status": "COMPLETE", "rows": 2, "committed_version": 9, "errors": {}}
    gen.send(ok)  # finalize's outcome -> emit_terminal

    assert ctx.activities[-1][0] == "emit_terminal"
    # The CANONICAL form, not the sparse dict the test wrote. Activities declare Pydantic inputs now
    # (DWF-ACT-009), so the body validates the outcome into `RunOutcome` and history carries every
    # declared field rather than only the keys this test happened to set. Same meaning, fuller record.
    assert ctx.activities[-1][1]["outcome"] == RunOutcome.model_validate(ok).model_dump(mode="json")

    with pytest.raises(StopIteration) as stop:
        gen.send(None)
    assert stop.value.value == ok


@pytest.mark.parametrize(
    ("limits", "chunks", "expected_status"),
    [
        ({"max_run_hours": 0.0, "max_units": 0}, [], "COMPLETE"),
        ({"max_run_hours": 0.0, "max_units": 1}, [{"keys": ["a", "b"]}], "FAILED"),
    ],
    ids=["empty-source", "unit-ceiling"],
)
def test_an_ALREADY_TERMINAL_short_circuit_is_NOT_contradicted_by_a_second_record(
    limits: dict[str, Any], chunks: list[dict[str, Any]], expected_status: str
) -> None:
    """The hole widening the boundary opened, and the reason `terminal_emitted` exists.

    The short-circuits — unit ceiling, empty source, deadline — each state how the run ended and then
    return, and moving the boundary up put them INSIDE it. So a permanently-failed `emit_terminal` on
    the "empty source is COMPLETE with zero rows" path reached the handler, which answered it with a
    SECOND emit carrying a FAILED outcome for a run that did not fail. A transient outage that
    outlasts four retries but not eight is the ordinary shape of one, so that second attempt can
    SUCCEED — and then the graph carries the lie permanently and the workflow returns it.

    The rule the boundary already stated ("there is no record to write about failing to write the
    record") has to hold for these paths too: the emit's own failure kills the run, visibly.
    """
    ctx = _Ctx()
    gen = ingest_run(cast("DaprWorkflowContext", ctx), SPEC)
    gen.send(None)
    gen.send(None)
    gen.send(limits)
    gen.send(HANDLE)
    gen.send(chunks)

    assert ctx.activities[-1][0] == "emit_terminal"
    assert ctx.activities[-1][1]["outcome"]["status"] == expected_status
    emits = len([n for n, _ in ctx.activities if n == "emit_terminal"])

    with pytest.raises(RuntimeError, match="lineage door down"):
        gen.throw(RuntimeError("lineage door down"))

    assert len([n for n, _ in ctx.activities if n == "emit_terminal"]) == emits, (
        f"the handler answered a failed terminal with a second one — calls were {[n for n, _ in ctx.activities]}"
    )


def test_emit_terminal_failing_INSIDE_the_boundary_still_fails_the_workflow() -> None:
    """Deliberate non-goal, pinned so it is a decision and not an accident: there is no record to
    write about failing to write the record. If the terminal emit itself dies, the workflow dies —
    visibly — rather than swallowing the loss one level deeper."""
    ctx = _Ctx()
    gen = _drive_to_fanout(ctx)

    gen.throw(RuntimeError("chunk dead"))
    # Through the §2.4 child-stop first — see `test_a_failed_chunk_reaches_emit_terminal_with_a_FAIL_outcome`.
    assert ctx.activities[-1][0] == "terminate_chunks"
    gen.send({"terminated": 1, "requested": 1})
    assert ctx.activities[-1][0] == "emit_terminal"

    with pytest.raises(RuntimeError, match="lineage door down"):
        gen.throw(RuntimeError("lineage door down"))


def test_a_terminal_run_is_COUNTED_by_status_and_volume(monkeypatch: pytest.MonkeyPatch) -> None:
    """The run-level verdict is a fact only this application knows.

    `ingest` converts failure into a RETURNED value — the error boundary produces
    `RunOutcome(status="FAILED")` rather than letting the orchestrator raise — so the sidecar's
    `dapr_runtime_workflow_execution_count_total` records `status="success"` for a run that died. An
    alert on that label reads green while every harvest fails, and the service had no metrics module at
    all: `grep -rn opentelemetry services/ingest/` returned nothing, and `chart/alerting/rules.yml`
    duly contains zero ingest rules.

    Volume rides the same terminal activity because it is the only place that knows both halves: rows
    committed and units that never made it. `outcome` is a closed pair owned by this module, never a
    value off a payload — the per-unit ids and the error text stay in the `errors` dict on the log.
    """
    import pytest as _pytest  # noqa: F401
    from ingest import metrics as ingest_metrics
    from ingest.workflow import emit_terminal

    runs: list[tuple[int, dict[str, str]]] = []
    units: list[tuple[int, dict[str, str]]] = []
    monkeypatch.setattr(ingest_metrics, "_runs", type("C", (), {"add": lambda _s, n, a=None: runs.append((n, dict(a or {})))})())
    monkeypatch.setattr(ingest_metrics, "_units", type("C", (), {"add": lambda _s, n, a=None: units.append((n, dict(a or {})))})())
    monkeypatch.setattr("ingest.runtime.release_run_units", lambda _r: _noop_coro())
    monkeypatch.setattr("ingest.workflow._lineage", lambda: _SilentLineage())

    emit_terminal(
        cast("Any", None),
        TerminalInput(
            spec=RunSpec.model_validate({"run_id": "run-1", "project": "acme", "dataset": "d", "kind": "k", "source": "s"}),
            outcome=RunOutcome.model_validate({"status": "FAILED", "rows": 12, "errors_total": 3}),
        ),
    )

    assert runs and runs[0][1].get("lance.ingest.status") == "FAILED", f"the run verdict was not counted: {runs}"
    by_outcome = {a.get("lance.ingest.outcome"): n for n, a in units}
    assert by_outcome.get("written") == 12, f"rows committed not counted: {units}"
    assert by_outcome.get("failed") == 3, f"failed units not counted: {units}"


async def _noop_coro() -> None:
    return None


class _SilentLineage:
    def terminal(self, *_a: object, **_k: object) -> None:
        return None


def test_the_terminal_span_names_the_RUN_and_marks_a_failed_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """A harvest trace answers "an activity ran" and never "which run, on which dataset".

    Both existing spans on this hop — daprd's `activity||emit_terminal` and the SDK's
    `activity: emit_terminal` — are correctly parented and carry only SDK/sidecar identifiers. Nothing
    names the run or the dataset, so a failed harvest cannot be found in a trace view by the thing an
    operator actually knows about it.

    And a FAILED run never marks its span: the error boundary RETURNS `RunOutcome(status="FAILED")`
    rather than raising, so daprd sees an activity that completed normally.
    """
    from ingest.workflow import emit_terminal
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.trace import StatusCode

    monkeypatch.setattr("ingest.runtime.release_run_units", lambda _r: _noop_coro())
    monkeypatch.setattr("ingest.workflow._lineage", lambda: _SilentLineage())
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    with provider.get_tracer("test").start_as_current_span("activity: emit_terminal"):
        emit_terminal(
            cast("Any", None),
            TerminalInput(
                spec=RunSpec.model_validate({"run_id": "run-77", "project": "acme", "dataset": "pages", "kind": "k", "source": "s"}),
                outcome=RunOutcome.model_validate({"status": "FAILED", "rows": 0, "errors_total": 2}),
            ),
        )

    (span,) = exporter.get_finished_spans()
    attrs = dict(span.attributes or {})
    assert attrs.get("lance.ingest.run_id") == "run-77", f"the span does not name the run: {attrs}"
    assert attrs.get("lance.dataset") == "pages", f"the span does not name the dataset: {attrs}"
    assert span.status.status_code is StatusCode.ERROR, (
        "a FAILED run leaves its terminal span UNSET — the boundary RETURNS the failure, so daprd sees an "
        "activity that completed and trace-based error search shows a clean estate."
    )
