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
import time
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
    monkeypatch.setattr(work_mod.base_refs, "sibling_base_refs", lambda uri, opts: work_mod.base_refs.BaseRefs())
    monkeypatch.setattr(work_mod, "execute_unit", lambda *a, **k: DatasetResult(uri="s3://b/t.lance"))

    with caplog.at_level(logging.INFO):
        await work_mod.handle_unit({"data": _item().model_dump(mode="json")}, settings, NoopEmitter())

    done = [r for r in caplog.records if r.message == "maintenance_unit_done"]
    assert done, f"no unit-done record; the elapsed time is unreadable without the trace store: {[r.message for r in caplog.records]}"
    elapsed = getattr(done[0], "elapsed_seconds", None)
    assert isinstance(elapsed, float), f"elapsed_seconds missing or not a float: {elapsed!r}"
    assert elapsed >= 0.0
    assert getattr(done[0], "uri", None) == "s3://b/t.lance", "the record must name the dataset, or a max is unattributable"


@pytest.mark.asyncio
async def test_the_clock_covers_the_WHOLE_delivery_to_ack_window(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """The placement is the claim, and a stub that returns instantly cannot test it.

    The route does real work BEFORE `execute_unit` — it re-reads protection off object storage, one
    listing per unit — and the ack clock is already running for all of it. A timer started around the
    execution alone would under-report by exactly that listing, which on a slow store is the part that
    varies. Mutation-checked: moving the timer below the protection read leaves this test the only one
    that notices.

    The stub therefore SLEEPS, and the assertion is that the reported cost contains that sleep.
    """
    from maintenance.api import work as work_mod
    from maintenance.core.config import MaintenanceSettings
    from maintenance.core.lineage_emit import NoopEmitter

    settings = MaintenanceSettings.model_validate({"s3_bucket": "b"})
    monkeypatch.setattr(work_mod, "emit_sweep_lineage", _noop)

    def _slow_protection_read(uri: str, opts: Any) -> Any:
        time.sleep(0.15)
        return work_mod.base_refs.BaseRefs()

    monkeypatch.setattr(work_mod.base_refs, "sibling_base_refs", _slow_protection_read)
    monkeypatch.setattr(work_mod, "execute_unit", lambda *a, **k: DatasetResult(uri="s3://b/t.lance"))

    with caplog.at_level(logging.INFO):
        await work_mod.handle_unit({"data": _item().model_dump(mode="json")}, settings, NoopEmitter())

    # `extra=` fields are attached dynamically, so `LogRecord` does not declare them — read through
    # `getattr` and narrow, rather than asserting an attribute the type does not carry.
    raw = next(getattr(r, "elapsed_seconds", None) for r in caplog.records if r.message == "maintenance_unit_done")
    assert isinstance(raw, float), f"elapsed_seconds missing or not a float: {raw!r}"
    elapsed: float = raw
    assert elapsed >= 0.15, (
        f"the reported cost ({elapsed}s) excludes the pre-execution protection read, so the clock does "
        f"not start where the broker's ack clock starts — the lane reads cheaper than it is."
    )


@pytest.mark.asyncio
async def test_a_unit_that_FAILS_still_reports_its_cost(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """The expensive units are the ones that fail slowly — omitting them would bias the max downward,
    which is the direction that makes a too-short `ackWait` look safe."""
    from maintenance.api import work as work_mod
    from maintenance.core.config import MaintenanceSettings
    from maintenance.core.lineage_emit import NoopEmitter

    settings = MaintenanceSettings.model_validate({"s3_bucket": "b"})
    monkeypatch.setattr(work_mod, "emit_sweep_lineage", _noop)
    monkeypatch.setattr(work_mod.base_refs, "sibling_base_refs", lambda uri, opts: work_mod.base_refs.BaseRefs())
    monkeypatch.setattr(work_mod, "execute_unit", lambda *a, **k: DatasetResult(uri="s3://b/t.lance", error="maintain: connection reset", error_type="OSError"))

    with caplog.at_level(logging.INFO):
        await work_mod.handle_unit({"data": _item().model_dump(mode="json")}, settings, NoopEmitter())

    done = [r for r in caplog.records if r.message == "maintenance_unit_done"]
    assert done, "a failing unit reported no cost — the slow failures are exactly the tail that matters"
    assert isinstance(getattr(done[0], "elapsed_seconds", None), float)


@pytest.mark.asyncio
async def test_a_malformed_unit_reports_nothing(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """The control. A unit that never executes has no lane cost, and counting it as 0.0 would drag the
    distribution toward zero — the same direction that makes a too-short `ackWait` look safe."""
    from maintenance.api import work as work_mod
    from maintenance.core.config import MaintenanceSettings
    from maintenance.core.lineage_emit import NoopEmitter

    settings = MaintenanceSettings.model_validate({"s3_bucket": "b"})
    monkeypatch.setattr(work_mod, "execute_unit", lambda *a, **k: pytest.fail("a malformed unit must not execute"))

    with caplog.at_level(logging.INFO):
        await work_mod.handle_unit({"data": {"not": "a unit"}}, settings, NoopEmitter())

    assert not [r for r in caplog.records if r.message == "maintenance_unit_done"]
