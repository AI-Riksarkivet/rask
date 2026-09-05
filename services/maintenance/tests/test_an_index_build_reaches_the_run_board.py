"""A queued index build is a maintenance run, and it must reach the graph like every other one.

The catalog's `/index/create` door emits a versioned `WROTE` event when it builds in-process, and
when it QUEUES it deliberately does not — its own docstring says the emit "belongs to whichever one
actually built: a queued unit has produced no version to measure, and emitting one would put a
phantom index event on the graph at every request". That reasoning is right and the other half never
arrived: the worker emitted nothing, so moving index builds off the request path (2026-09-04) made
them invisible. An index build over a large table can run for an hour and leave no trace on the run
board, in a service whose other long operation — compaction — has emitted one all along.

THE OPERATION IS A PARAMETER because the marker is read as data. `emit_maintenance` stamped
`operation=compaction` unconditionally, and reusing it here would record an index build AS a
compaction — a wrong fact on the graph rather than a missing one, which is the worse of the two and
harder to notice. The lineage repository keys behaviour off that marker (a versionless `WROTE`, no
`DERIVED_FROM`, no `CREATED` edge), so it must say what actually happened.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lance
import pyarrow as pa
import pytest

from maintenance.api import index_work
from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import COMPACTION, CREATE_INDEX
from service_kit.lakehouse.work_items import SCALAR_INDEX, IndexWorkItem


class _Emitter:
    """Records what it was asked to emit — the operation included, which is the point."""

    def __init__(self) -> None:
        self.emitted: list[dict[str, Any]] = []
        self.failed: list[dict[str, Any]] = []

    async def emit_maintenance(self, *, table_id: str, namespace: str, operation: str = COMPACTION) -> None:
        self.emitted.append({"table_id": table_id, "namespace": namespace, "operation": operation})

    async def emit_maintenance_failed(self, *, table_id: str, namespace: str, error: str, operation: str = COMPACTION) -> None:
        self.failed.append({"table_id": table_id, "namespace": namespace, "error": error, "operation": operation})


def _table(tmp_path: Path) -> str:
    uri = str(tmp_path / "t.lance")
    lance.write_dataset(pa.table({"id": pa.array(range(300), pa.int64())}), uri)
    return uri


def _settings() -> MaintenanceSettings:
    return MaintenanceSettings.model_validate({"MAINTENANCE_INDEX_TOPIC": "idx", "MAINTENANCE_EXECUTE_WORK": True})


@pytest.mark.asyncio
async def test_a_BUILT_index_reaches_the_run_board(tmp_path: Path) -> None:
    emitter = _Emitter()
    item = IndexWorkItem(uri=_table(tmp_path), column="id", kind=SCALAR_INDEX, index_type="BTREE", table_id="ns$t")

    result = await index_work.handle_index_unit({"data": item.model_dump()}, _settings(), emitter)

    assert result["status"] == "SUCCESS"
    assert emitter.emitted == [{"table_id": "ns$t", "namespace": "ns", "operation": CREATE_INDEX}]


@pytest.mark.asyncio
async def test_the_operation_is_NOT_compaction(tmp_path: Path) -> None:
    """Recording an index build as a compaction is a wrong fact rather than a missing one, and the
    repository reads the marker to decide what edges the run gets."""
    emitter = _Emitter()
    item = IndexWorkItem(uri=_table(tmp_path), column="id", kind=SCALAR_INDEX, index_type="BTREE", table_id="ns$t")

    await index_work.handle_index_unit({"data": item.model_dump()}, _settings(), emitter)

    assert emitter.emitted[0]["operation"] != "compaction"


@pytest.mark.asyncio
async def test_an_UNBUILDABLE_unit_emits_nothing(tmp_path: Path) -> None:
    """A producer defect is acked, not run — so there is no run to record. Emitting a FAIL for a unit
    that never touched the table would put a maintenance failure on a dataset nothing maintained."""
    emitter = _Emitter()
    item = IndexWorkItem(uri=_table(tmp_path), column="absent", kind=SCALAR_INDEX, index_type="BTREE", table_id="ns$t")

    result = await index_work.handle_index_unit({"data": item.model_dump()}, _settings(), emitter)

    assert result["status"] == "SUCCESS"
    assert emitter.emitted == [] and emitter.failed == []


@pytest.mark.asyncio
async def test_a_TABLE_ID_LESS_unit_still_builds_and_emits_nothing(tmp_path: Path) -> None:
    """The emit is keyed on the catalog id. A unit carrying none is still built — the index is what the
    caller asked for — but there is no dataset node to hang the run on, so nothing is emitted rather
    than a run keyed on a URI the graph does not know."""
    emitter = _Emitter()
    item = IndexWorkItem(uri=_table(tmp_path), column="id", kind=SCALAR_INDEX, index_type="BTREE")

    result = await index_work.handle_index_unit({"data": item.model_dump()}, _settings(), emitter)

    assert result["status"] == "SUCCESS"
    assert emitter.emitted == []
