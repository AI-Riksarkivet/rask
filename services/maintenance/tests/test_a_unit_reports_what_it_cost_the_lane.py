"""Every executed unit records how long it held its ack — the quantity `ackWait` has to exceed.

[[LH-190]]. The broker gives a delivered unit `ackWait` (720s) to be acked, measured FROM DELIVERY. So
the number that decides whether `ackWait` is correctly sized is the time from the route receiving a
unit to the route answering — semaphore wait plus execution — and the estate could not read it:

* the worker logs the OUTCOME of each unit and no elapsed field;
* `chart/values.yaml` records the handler at 0.21s (median 0.12, p90 0.60), but that is the trace
  SPAN, which measures the work rather than what the lane holds;
* and the trace store cannot be asked. Measured 2026-09-22, GreptimeDB answers
  `Exceeded memory limit: 1018.5MiB used globally (99%), hard limit: 1.0GiB` to both a `count(*)` and
  a `max(duration_nano)` over `opentelemetry_traces`.

So the one input that would settle whether `ackWait` can come down — and with it the ~324s stall
measured when every replica is lost at once — was unobtainable for an observability reason rather than
a lakehouse one. A field on the unit's own outcome removes that dependency: the distribution, including
the max, becomes readable from the worker's logs with a grep, on any estate, with no trace store at all.

MEASURED AT THE ROUTE, not around `execute_unit`, and the difference is the whole point: the ack clock
starts when Dapr delivers, so a unit queued behind the execution ceiling is holding its ack while it
waits. Timing only the execution would report the lane as cheaper than it is — the same error
`secondsPerUnit`'s own comment records, where the 0.21s span "would have certified a lane that could
not keep up".
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from maintenance.services.sweep import DatasetResult, DatasetWorkItem


def _item() -> DatasetWorkItem:
    return DatasetWorkItem(uri="s3://b/t.lance", table_id="ns$t", plan={})


async def _noop(*_a: Any, **_k: Any) -> None:
    return None


@pytest.mark.asyncio
async def test_an_executed_unit_logs_its_elapsed_time(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    from maintenance.api import work as work_mod
    from maintenance.core.config import MaintenanceSettings
    from maintenance.core.lineage_emit import NoopEmitter

    settings = MaintenanceSettings.model_validate({"s3_bucket": "b"})
    monkeypatch.setattr(work_mod, "emit_sweep_lineage", _noop)
    monkeypatch.setattr(work_mod.base_refs, "sibling_base_refs", lambda uri, opts, *, configured, record_of: work_mod.base_refs.BaseRefs())
    monkeypatch.setattr(work_mod, "execute_unit", lambda *a, **k: DatasetResult(uri="s3://b/t.lance"))

    with caplog.at_level(logging.INFO):
        await work_mod.handle_unit({"data": _item().model_dump(mode="json")}, settings, NoopEmitter())

    done = [r for r in caplog.records if r.message == "maintenance_unit_done"]
    assert done, f"no unit-done record; the elapsed time is unreadable without the trace store: {[r.message for r in caplog.records]}"
    elapsed = getattr(done[0], "elapsed_seconds", None)
    assert isinstance(elapsed, float), f"elapsed_seconds missing or not a float: {elapsed!r}"
    assert elapsed >= 0.0
    assert getattr(done[0], "uri", None) == "s3://b/t.lance", "the record must name the dataset, or a max is unattributable"
