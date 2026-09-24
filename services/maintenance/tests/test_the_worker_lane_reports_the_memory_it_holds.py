"""The lane that is actually climbing has to report what it holds.

[[LH-183]]. The row's method is a comparison — RSS against the Python heap against the Lance session —
and it is readable on the PLANNER (`api/routes.py` puts `memory_readings()` on the tick line) and on a
committed rewrite (`compaction_distributed_committed`). It is readable nowhere on the unit path, which
is the path both deployed workers spend their lives on.

MEASURED 2026-09-24, on the deployed estate, over the same 6.8 h window: the 512Mi planner plateaued
(-1.49 Mi/h over the last six hours, 299.5 MiB) while both 4Gi workers climbed monotonically at +18.32
and +18.52 Mi/h with no inflection — two independent pods agreeing to 1%, which is a mechanism rather
than noise. Over thirty minutes of that window the pair logged 374 `compaction_distributed_nothing_to_do`
and ZERO `compaction_distributed_committed`, so the climb is on the NO-OP unit path: the one place the
existing instrument cannot reach, because it rides the commit.

THE INSTRUMENT MUST NOT RIDE THE COMMIT, and that is the whole shape of this gate. Gating the readings
on a rewrite is what made the climb invisible, and the same gating is why `should_retire` — the
recycle this row already shipped — has never fired: `passes_committed()` advances only on a commit, so
a worker doing nothing but no-op units never reaches its ceiling and never recycles.

DRIVEN THROUGH THE ROUTE, never asserted by walking the source. An AST walk passes on a call that is
present but unreachable, which is exactly the failure being repaired: `memory_readings()` is called in
two places today and neither runs on a deployed worker.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from maintenance.services.sweep import DatasetResult, DatasetWorkItem, memory_readings


#: The key set the row's comparison needs. Read from the seam rather than restated, so a fourth reading
#: added there becomes required here without editing this file — and asserted non-empty below, because
#: an empty set would make every assertion pass by having nothing to check.
EXPECTED = set(memory_readings())


async def _noop(*_a: Any, **_k: Any) -> None:
    return None


def test_the_measurement_has_something_to_measure() -> None:
    """Anti-vacuity, first: both gates below compare against `EXPECTED`."""
    assert EXPECTED, "memory_readings() reports nothing, so the gates below assert nothing"
    assert "rss_bytes" in EXPECTED, f"the comparison needs RSS to anchor it: {sorted(EXPECTED)}"


@pytest.mark.asyncio
async def test_a_COMPACTION_unit_reports_the_memory_it_held(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """EVERY unit, not only one that rewrote something.

    The fixture returns a `DatasetResult` with nothing committed — the no-op outcome that is 100% of
    the measured live traffic — so a reading gated on a rewrite leaves this red.
    """
    from maintenance.api import work as work_mod
    from maintenance.core.config import MaintenanceSettings
    from maintenance.core.lineage_emit import NoopEmitter

    settings = MaintenanceSettings.model_validate({"s3_bucket": "b"})
    monkeypatch.setattr(work_mod, "emit_sweep_lineage", _noop)
    monkeypatch.setattr(work_mod.base_refs, "sibling_base_refs", lambda uri, opts: work_mod.base_refs.BaseRefs())
    monkeypatch.setattr(work_mod, "execute_unit", lambda *a, **k: DatasetResult(uri="s3://b/t.lance"))

    with caplog.at_level(logging.INFO):
        await work_mod.handle_unit({"data": DatasetWorkItem(uri="s3://b/t.lance", table_id="ns$t", plan={}).model_dump(mode="json")}, settings, NoopEmitter())

    done = [r for r in caplog.records if r.message == "maintenance_unit_done"]
    assert done, f"no unit-done record at all: {[r.message for r in caplog.records]}"
    missing = sorted(k for k in EXPECTED if not hasattr(done[0], k))
    assert not missing, f"the unit lane reports no memory: {missing} absent from maintenance_unit_done"


@pytest.mark.asyncio
async def test_an_INDEX_unit_reports_it_too(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Both lanes share one process, so a reading on one of them attributes nothing.

    An index build can run for an hour and both subscriptions are served by the same worker pod; a
    series that covers only compaction units cannot say which lane grew the process.
    """
    from maintenance.api import index_work as index_mod
    from maintenance.core.config import MaintenanceSettings
    from maintenance.core.lineage_emit import NoopEmitter
    from maintenance.services.index_build import IndexOutcome
    from service_kit.lakehouse.work_items import SCALAR_INDEX

    settings = MaintenanceSettings.model_validate({"s3_bucket": "b"})
    monkeypatch.setattr(index_mod.credentials, "write_options_for", lambda *a, **k: {})
    monkeypatch.setattr(index_mod, "build_index", lambda *a, **k: IndexOutcome(name="id_idx", column="id", kind=SCALAR_INDEX, version=3))

    unit = {"uri": "s3://b/t.lance", "table_id": "", "column": "id", "kind": SCALAR_INDEX, "index_type": "BTREE", "name": "id_idx"}
    with caplog.at_level(logging.INFO):
        await index_mod.handle_index_unit({"data": unit}, settings, NoopEmitter())

    done = [r for r in caplog.records if r.message == "index_unit_done"]
    assert done, f"no index-unit-done record at all: {[r.message for r in caplog.records]}"
    missing = sorted(k for k in EXPECTED if not hasattr(done[0], k))
    assert not missing, f"the index lane reports no memory: {missing} absent from index_unit_done"
