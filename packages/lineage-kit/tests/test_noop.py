"""The no-crash guarantee: a transport failure is logged, never raised into the producer."""

from __future__ import annotations

import logging
from typing import cast

import pytest

from lineage_kit import ClientEmitter, RunState
from lineage_kit.schemas import Job, Run, RunEvent


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
