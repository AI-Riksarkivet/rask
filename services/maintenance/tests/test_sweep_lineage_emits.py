"""`emit_sweep_lineage` must treat its two event lanes the same way (MAINT-04).

The FAIL lane is gathered concurrently and capped, with a written argument for both: each publish is
bounded by the emitter's 5 s timeout, so an unbounded SEQUENTIAL fan-out over a bucket where every
dataset is interesting pushes the cron handler past Dapr's 30 s ack window. Every word of that applies
to the COMPLETE lane, which was awaited one dataset at a time inside the loop — and, unlike the FAIL
lane, with no guard, so a single raising publish aborted the whole emit phase (including every FAIL
emit queued behind it) and 500'd the tick.
"""

from __future__ import annotations

import asyncio

import pytest

from maintenance.core.lineage_emit import COMPACTION
from maintenance.services.optimize import DatasetResult
from maintenance.services.sweep import emit_sweep_lineage


class _RecordingEmitter:
    """Counts concurrency: every publish yields, so a gathered batch overlaps and a serial one cannot."""

    def __init__(self, *, raise_on: str | None = None) -> None:
        self.raise_on = raise_on
        self.in_flight = 0
        self.max_in_flight = 0
        self.completed: list[str] = []
        self.failed: list[str] = []

    async def _publish(self, table_id: str, sink: list[str]) -> None:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
            if self.raise_on == table_id:
                raise RuntimeError("mis-wired emitter")
            sink.append(table_id)
        finally:
            self.in_flight -= 1

    async def emit_maintenance(self, *, table_id: str, namespace: str, operation: str = COMPACTION) -> None:
        await self._publish(table_id, self.completed)

    async def emit_maintenance_failed(self, *, table_id: str, namespace: str, error: str, operation: str = COMPACTION) -> None:
        await self._publish(table_id, self.failed)


def _material(n: int) -> list[DatasetResult]:
    """`n` datasets that each reclaimed something — the COMPLETE lane's trigger."""
    return [DatasetResult(uri=f"s3://b/{i:04d}_ns.tbl{i}", declared_table_id=f"ns.tbl{i}", fragments_removed=1) for i in range(n)]


@pytest.mark.asyncio
async def test_complete_emits_are_gathered_not_awaited_one_at_a_time() -> None:
    emitter = _RecordingEmitter()
    await emit_sweep_lineage(emitter, _material(40), delimiter=".")
    assert len(emitter.completed) == 40
    assert emitter.max_in_flight > 1, f"COMPLETE publishes ran strictly serially (max in flight {emitter.max_in_flight}) — 40 x 5s of ack window"


@pytest.mark.asyncio
async def test_a_raising_complete_publish_does_not_abort_the_emit_phase() -> None:
    """The FAIL lane is documented raise-proof "even for a mis-wired emitter"; the COMPLETE lane was not."""
    results = [*_material(3), DatasetResult(uri="s3://b/9999_ns.bad", declared_table_id="ns.bad", error="maintain: boom", error_type="RuntimeError")]
    emitter = _RecordingEmitter(raise_on="ns.tbl1")
    await emit_sweep_lineage(emitter, results, delimiter=".")
    assert sorted(emitter.completed) == ["ns.tbl0", "ns.tbl2"], "a raising publish must not take its siblings down"
    assert emitter.failed == ["ns.bad"], "the FAIL lane must still run after a COMPLETE publish raised"
