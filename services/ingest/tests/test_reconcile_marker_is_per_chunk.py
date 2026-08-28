"""A short-drained chunk's reconcile diagnostic must survive the parent's fan-in merge.

`reconcile_from_queue` stamped every chunk's "N units still outstanding" message under the SAME
literal key `__chunk__`. The parent fan-in flattens each child's error map with `merged.update(...)`,
so N reconciling chunks collapse to ONE message — last write wins — and an operator reading the run's
errors sees a single chunk's diagnostic standing in for the whole fan-out. Keying the marker by the
chunk keeps one entry per chunk.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_two_reconciling_chunks_keep_distinct_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    from ingest import queue as queue_mod
    from ingest import runtime
    from ingest.workflow import ChunkSpec

    class _FakeSub:
        async def consumer_info(self) -> SimpleNamespace:
            return SimpleNamespace(num_pending=3)

    class _FakeQueue:
        async def subscribe(self, _run_id: str) -> _FakeSub:
            return _FakeSub()

        async def close(self) -> None: ...

    async def _fake_connect(_url: str) -> _FakeQueue:
        return _FakeQueue()

    monkeypatch.setattr(queue_mod.WorkQueue, "connect", staticmethod(_fake_connect))
    monkeypatch.setattr(runtime, "nats_url", lambda: "nats://unused:4222")

    result_a = await runtime.reconcile_from_queue(ChunkSpec(run_id="run-x", chunk_id="chunk-a"))
    result_b = await runtime.reconcile_from_queue(ChunkSpec(run_id="run-x", chunk_id="chunk-b"))

    # Exactly how the parent fan-in flattens its children (workflow.py: `merged.update(result.errors)`).
    merged: dict[str, str] = {}
    merged.update(result_a["errors"])
    merged.update(result_b["errors"])

    assert len(merged) == 2, f"two reconciling chunks collapsed to {len(merged)} entry(ies): {merged}"
