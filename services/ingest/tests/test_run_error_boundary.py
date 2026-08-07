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

from typing import Any

import pytest
from ingest.workflow import ingest_run


class _Task:
    """A stand-in for a durabletask task — identity is all `when_any` comparisons use."""


class _Ctx:
    """Records the activity calls the workflow makes, in order, with their inputs."""

    def __init__(self) -> None:
        self.activities: list[tuple[str, dict[str, Any]]] = []
        self.statuses: list[str] = []

    def call_activity(self, fn: Any, *, input: Any = None, retry_policy: Any = None) -> _Task:  # noqa: A002 — the runtime's own keyword
        self.activities.append((getattr(fn, "__name__", str(fn)), input or {}))
        return _Task()

    def call_child_workflow(self, fn: Any, *, input: Any = None) -> _Task:  # noqa: A002
        return _Task()

    def create_timer(self, _delta: Any) -> _Task:
        return _Task()

    def set_custom_status(self, status: str) -> None:
        self.statuses.append(status)


SPEC = {"run_id": "boundary-test", "kind": "s3-prefix", "project": "bind86", "dataset": "pages", "options": {}}


@pytest.fixture(autouse=True)
def _plain_fanout(monkeypatch: pytest.MonkeyPatch) -> None:
    """`wf.when_all` wraps tasks in runtime bookkeeping these stand-ins do not carry; the workflow
    only ever YIELDS its return value, so a bare task is a faithful double at this seam."""
    from ingest import workflow as wf_module

    monkeypatch.setattr(wf_module.wf, "when_all", lambda tasks: _Task())
    monkeypatch.setattr(wf_module.wf, "when_any", lambda tasks: _Task())


def _drive_to_fanout(ctx: _Ctx) -> Any:
    """Run the generator up to the fan-in yield: emit_start -> ensure_dataset -> enumerate -> fanout."""
    gen = ingest_run(ctx, SPEC)  # type: ignore[arg-type]
    gen.send(None)  # start -> yields emit_start
    gen.send(None)  # emit_start done -> yields ensure_dataset
    gen.send("s3://wh/loc")  # location -> yields enumerate_chunks
    # One chunk of two units -> the workflow computes units_total=2 and yields the fan-in.
    gen.send([{"keys": ["a", "b"]}])
    return gen


def test_a_failed_chunk_reaches_emit_terminal_with_a_FAIL_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression. `.throw()` at the fan-in is exactly what the runtime does when a child
    workflow's failure is replayed; the boundary must convert it into the ONE terminal step."""
    monkeypatch.setenv("RASK_INGEST_MAX_RUN_HOURS", "")
    ctx = _Ctx()
    gen = _drive_to_fanout(ctx)

    # The runtime throws the RECORDED child failure into the generator at the yield point.
    gen.throw(RuntimeError("Activity task #8 failed: catalog refused describe (403)"))

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


def test_a_PERMANENTLY_REFUSED_finalize_also_leaves_a_FAIL_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """`tests/test_empty_commit.py` documented this leg verbatim: a finalize that burns its retries
    killed the workflow before its own FAIL record. The boundary must cover it identically."""
    monkeypatch.setenv("RASK_INGEST_MAX_RUN_HOURS", "")
    ctx = _Ctx()
    gen = _drive_to_fanout(ctx)

    # Fan-in SUCCEEDS: one chunk result, no errors -> the workflow proceeds to finalize.
    gen.send([{"chunk_id": "c0", "units_done": 2, "fragments": [], "errors": {}}])
    assert ctx.activities[-1][0] == "finalize"

    # finalize exhausts its retries -> the runtime throws the recorded failure.
    gen.throw(RuntimeError("commit conflict on bronze$pages at read_version 7"))

    assert ctx.activities[-1][0] == "emit_terminal"
    outcome = ctx.activities[-1][1]["outcome"]
    assert outcome["status"] == "FAILED"
    assert "commit conflict" in outcome["errors"]["run"]


def test_the_SUCCESS_path_is_untouched_by_the_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    """The boundary must not change what a healthy run does: fan-in -> finalize -> terminal, with
    finalize's outcome — not a synthesized one — as the terminal payload and the return value."""
    monkeypatch.setenv("RASK_INGEST_MAX_RUN_HOURS", "")
    ctx = _Ctx()
    gen = _drive_to_fanout(ctx)

    gen.send([{"chunk_id": "c0", "units_done": 2, "fragments": [], "errors": {}}])  # fan-in result -> finalize
    ok = {"status": "COMPLETE", "rows": 2, "committed_version": 9, "errors": {}}
    gen.send(ok)  # finalize's outcome -> emit_terminal

    assert ctx.activities[-1][0] == "emit_terminal"
    assert ctx.activities[-1][1]["outcome"] == ok

    with pytest.raises(StopIteration) as stop:
        gen.send(None)
    assert stop.value.value == ok


def test_emit_terminal_failing_INSIDE_the_boundary_still_fails_the_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deliberate non-goal, pinned so it is a decision and not an accident: there is no record to
    write about failing to write the record. If the terminal emit itself dies, the workflow dies —
    visibly — rather than swallowing the loss one level deeper."""
    monkeypatch.setenv("RASK_INGEST_MAX_RUN_HOURS", "")
    ctx = _Ctx()
    gen = _drive_to_fanout(ctx)

    gen.throw(RuntimeError("chunk dead"))
    assert ctx.activities[-1][0] == "emit_terminal"

    with pytest.raises(RuntimeError, match="lineage door down"):
        gen.throw(RuntimeError("lineage door down"))
