"""`emit_pass_lineage` emits each dataset of one pass on the lane its result decides, and no publish fails the pass
(MAINT-04).

A raising publish is caught for its own dataset, in either lane, so every event queued behind it is still emitted and the
worker still schedules the table's next pass.
"""

from __future__ import annotations

import asyncio

import pytest

from lakehouse.lineage_events import DatasetUuids
from maintenance.core.lineage_emit import UNRESOLVED
from maintenance.services.expiry import DatasetResult
from maintenance.services.table_pass import emit_pass_lineage


class _RecordingEmitter:
    """Records each publish by lane, and raises on the one ``raise_on`` names."""

    def __init__(self, *, raise_on: str | None = None) -> None:
        self.raise_on = raise_on
        self.completed: list[str] = []
        self.failed: list[str] = []

    async def _publish(self, table_id: str, sink: list[str]) -> None:
        await asyncio.sleep(0)
        if self.raise_on == table_id:
            raise RuntimeError("mis-wired emitter")
        sink.append(table_id)

    async def emit_maintenance(self, *, table_id: str, location: str | None = None, uuids: DatasetUuids = UNRESOLVED) -> None:
        await self._publish(table_id, self.completed)

    async def emit_maintenance_failed(self, *, table_id: str, error: str, location: str | None = None, uuids: DatasetUuids = UNRESOLVED) -> None:
        await self._publish(table_id, self.failed)


def _material(n: int) -> list[DatasetResult]:
    """`n` datasets that each reclaimed something — the COMPLETE lane's trigger."""
    return [DatasetResult(uri=f"s3://b/{i:04d}_ns.tbl{i}", table_id=f"ns$tbl{i}", old_versions_removed=1) for i in range(n)]


@pytest.mark.asyncio
async def test_a_raising_complete_publish_does_not_abort_the_emit_phase() -> None:
    """The FAIL lane is documented raise-proof "even for a mis-wired emitter"; the COMPLETE lane was not."""
    results = [*_material(3), DatasetResult(uri="s3://b/9999_ns.bad", table_id="ns$bad", error="maintain: boom", error_type="RuntimeError")]
    emitter = _RecordingEmitter(raise_on="ns$tbl1")
    await emit_pass_lineage(emitter, results)
    assert sorted(emitter.completed) == ["ns$tbl0", "ns$tbl2"], "a raising publish must not take its siblings down"
    assert emitter.failed == ["ns$bad"], "the FAIL lane must still run after a COMPLETE publish raised"


@pytest.mark.parametrize(
    ("result", "lane"),
    [
        pytest.param(
            DatasetResult(uri="s3://b/0001_ns.t", table_id="ns$t", old_versions_removed=4, error="auto_cleanup: denied", error_type="OSError"),
            "complete",
            id="a-failed-disarm-beside-a-reclaim",
        ),
        pytest.param(
            DatasetResult(uri="s3://b/0001_ns.t", table_id="ns$t", error="auto_cleanup: denied", error_type="OSError"),
            None,
            id="a-failed-disarm-that-reclaimed-nothing",
        ),
    ],
)
@pytest.mark.asyncio
async def test_the_lane_is_decided_by_the_work_done_unless_the_pass_failed(result: DatasetResult, lane: str | None) -> None:
    """A failed disarm leaves the commit path armed, but whatever the pass reclaimed still changed the table."""
    emitter = _RecordingEmitter()
    await emit_pass_lineage(emitter, [result])

    reached = "complete" if emitter.completed else "fail" if emitter.failed else None
    assert reached == lane, (emitter.completed, emitter.failed)
