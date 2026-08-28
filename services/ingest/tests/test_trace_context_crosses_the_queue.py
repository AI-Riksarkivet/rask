"""The trace context must actually cross the queue boundary.

`UnitTask.traceparent` was declared on the wire model but never written at the one construction site
(`runtime.publish_chunk_units`) and never read at the far end (`worker.drain_chunk`). A declared but
dead field reads as a working mechanism: an operator sees the column and assumes one run's trace
spans api -> workers -> lander, when in fact every unit carries `None`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from opentelemetry.propagate import extract
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import get_current_span


if TYPE_CHECKING:
    from ingest.queue import UnitTask


@pytest.mark.asyncio
async def test_published_units_carry_the_active_trace_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """With a span active at publish, every UnitTask must carry that span's W3C traceparent."""
    from ingest import queue as queue_mod
    from ingest import runtime
    from ingest.workflow import ChunkSpec

    captured: list[UnitTask] = []

    class _FakeQueue:
        async def ensure_stream(self) -> None: ...

        async def publish_units(self, tasks: list[UnitTask]) -> int:
            captured.extend(tasks)
            return len(tasks)

        async def close(self) -> None: ...

    async def _fake_connect(_url: str) -> _FakeQueue:
        return _FakeQueue()

    monkeypatch.setattr(queue_mod.WorkQueue, "connect", staticmethod(_fake_connect))
    monkeypatch.setattr(runtime, "nats_url", lambda: "nats://unused:4222")

    chunk = ChunkSpec(
        run_id="run-trace",
        chunk_id="chunk-0",
        keys=["s3://bucket/a.tif", "s3://bucket/b.tif"],
        kind="",
        project="proj",
        dataset="ds",
        dataset_uri="s3://bucket/proj/ds.lance",
    )

    provider = TracerProvider()
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("run") as span:
        expected_trace_id = span.get_span_context().trace_id
        await runtime.publish_chunk_units(chunk)

    assert captured, "publish_chunk_units built no units"
    for task in captured:
        assert task.traceparent is not None, "UnitTask.traceparent is None — no trace context crossed the queue"
        carried = get_current_span(extract({"traceparent": task.traceparent})).get_span_context()
        assert carried.trace_id == expected_trace_id, "the carried traceparent names a different trace than the active span"
