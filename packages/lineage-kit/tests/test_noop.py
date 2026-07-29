"""The no-transport guarantee: lineage without an endpoint NEVER crashes compute."""

from __future__ import annotations

import logging
from typing import cast

import pytest
from lineage_kit import ClientEmitter, NoopEmitter, RunState, default_emitter, job_run, stage
from lineage_kit.schemas import Job, Run, RunEvent


def test_default_emitter_without_endpoint_is_noop() -> None:
    assert isinstance(default_emitter(), NoopEmitter)


def test_full_pipeline_shape_runs_unlineaged_without_crashing(caplog: pytest.LogCaptureFixture) -> None:
    # No endpoint, no injected emitter — the whole job→stage cycle must be a logged no-op.
    @stage("layout")
    def do_layout(x: int) -> int:
        return x * 2

    with caplog.at_level(logging.DEBUG, logger="lineage_kit.emitter"), job_run("ingest"):
        assert do_layout(21) == 42

    assert any("lineage_noop_drop" in record.message for record in caplog.records)


def test_failing_pipeline_still_reraises_the_real_error_without_transport() -> None:
    with pytest.raises(ValueError, match="the real error"), job_run("ingest"):
        msg = "the real error"
        raise ValueError(msg)


def test_transport_failure_is_swallowed_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    class ExplodingClient:
        def emit(self, event: object) -> None:
            msg = "connection refused"
            raise ConnectionError(msg)

    from openlineage.client import OpenLineageClient

    emitter = ClientEmitter(cast("OpenLineageClient", ExplodingClient()))
    event = RunEvent(
        event_type=RunState.START,
        event_time="2026-07-27T09:00:00+00:00",
        run=Run(run_id="3f2504e0-4f89-11d3-9a0c-0305e82c3301"),
        job=Job(namespace="rask", name="ingest"),
    )
    with caplog.at_level(logging.WARNING, logger="lineage_kit.emitter"):
        emitter.emit(event)  # must not raise
    assert any("lineage_emit_failed" in record.message for record in caplog.records)
