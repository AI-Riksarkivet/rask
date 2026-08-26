"""Run-state transitions: START → COMPLETE / FAIL / ABORT, and the terminal-once guard."""

from __future__ import annotations

import logging

import pytest
from lineage_kit import LineageRun, RecordingEmitter, RunState, job_run


def _states(recording: RecordingEmitter) -> list[RunState]:
    return [e.event_type for e in recording.events]


def test_start_then_complete(recording: RecordingEmitter) -> None:
    run = LineageRun(job_name="ingest", namespace="rask", emitter=recording)
    run.start()
    assert run.pending
    run.complete(outputs=[("lance", "silver.lines")])
    assert not run.pending
    assert _states(recording) == [RunState.START, RunState.COMPLETE]
    assert recording.events[1].outputs[0].name == "silver.lines"


def test_fail_carries_error_message_and_stack_trace(recording: RecordingEmitter) -> None:
    run = LineageRun(job_name="ingest", namespace="rask", emitter=recording)
    run.start()
    try:
        msg = "boom"
        raise ValueError(msg)
    except ValueError as exc:
        run.fail(exc)
    assert _states(recording) == [RunState.START, RunState.FAIL]
    facet = recording.events[1].run.facets.error_message
    assert facet is not None
    assert facet.message == "ValueError: boom"
    assert facet.stack_trace and "ValueError" in facet.stack_trace
    assert facet.programming_language == "PYTHON"


def test_abort_with_reason(recording: RecordingEmitter) -> None:
    run = LineageRun(job_name="ingest", namespace="rask", emitter=recording)
    run.start()
    run.abort("operator stop")
    assert _states(recording) == [RunState.START, RunState.ABORT]
    facet = recording.events[1].run.facets.error_message
    assert facet is not None and facet.message == "operator stop"


def test_abort_can_NAME_what_the_run_touched(recording: RecordingEmitter) -> None:
    """ABORT was the only terminal that could not carry inputs/outputs, and the asymmetry cost real
    visibility rather than tidiness.

    `complete()` and `fail()` both take them; `abort()` did not. So a cancelled run emitted a terminal
    with no dataset edge at all — and a FAILED run keeps its WROTE edge deliberately
    (`lineage/services/repository.py`: "FAILed runs keep WROTE edges (producers() shows the attempt),
    so the event_type=COMPLETE filter is load-bearing"). The estate's guard against a half-written
    dataset being read as a real one is that COMPLETE filter, NOT the absence of the edge — so
    withholding the edge on ABORT bought no safety and lost the run from every surface that finds a
    run through the dataset it touched.

    Measured 2026-08-26: a terminated ingest run landed in the graph correctly (right job, right
    source_run_id, right error) and did not appear on the compute zone's run board at all.
    """
    run = LineageRun(job_name="ingest", namespace="rask", emitter=recording)
    run.start()
    run.abort("operator stop", outputs=[("rask", "acme-bronze$abortproof")])

    assert _states(recording) == [RunState.START, RunState.ABORT]
    outputs = recording.events[1].outputs
    assert outputs, "ABORT emitted no output — the run is invisible to anything that looks up by dataset"
    assert outputs[0].name == "acme-bronze$abortproof"


def test_terminal_is_emitted_at_most_once(recording: RecordingEmitter, caplog: pytest.LogCaptureFixture) -> None:
    run = LineageRun(job_name="ingest", namespace="rask", emitter=recording)
    run.start()
    assert run.complete() is not None
    with caplog.at_level(logging.WARNING, logger="lineage_kit.runs"):
        assert run.fail("too late") is None
        assert run.abort() is None
        assert run.complete() is None
    assert _states(recording) == [RunState.START, RunState.COMPLETE]
    assert sum("lineage_terminal_repeat" in r.message for r in caplog.records) == 3


def test_running_heartbeat_is_repeatable_and_non_terminal(recording: RecordingEmitter) -> None:
    run = LineageRun(job_name="ingest", namespace="rask", emitter=recording)
    run.start()
    run.running()
    run.running()
    assert run.pending
    run.complete()
    assert _states(recording) == [RunState.START, RunState.RUNNING, RunState.RUNNING, RunState.COMPLETE]


def test_job_run_success_emits_start_and_complete(recording: RecordingEmitter) -> None:
    with job_run("ingest", namespace="rask", emitter=recording, inputs=[("iiif", "riksarkivet")]):
        pass
    assert _states(recording) == [RunState.START, RunState.COMPLETE]
    assert recording.events[0].inputs[0].namespace == "iiif"


def test_job_run_failure_emits_fail_and_reraises(recording: RecordingEmitter) -> None:
    with pytest.raises(RuntimeError, match="pipeline exploded"), job_run("ingest", namespace="rask", emitter=recording):
        msg = "pipeline exploded"
        raise RuntimeError(msg)
    assert _states(recording) == [RunState.START, RunState.FAIL]
    facet = recording.events[1].run.facets.error_message
    assert facet is not None and "pipeline exploded" in facet.message


def test_job_run_respects_a_manually_terminated_run(recording: RecordingEmitter) -> None:
    with job_run("ingest", namespace="rask", emitter=recording) as run:
        run.abort("shutting down")
    # the context manager must NOT stack a COMPLETE on top of the manual ABORT
    assert _states(recording) == [RunState.START, RunState.ABORT]
