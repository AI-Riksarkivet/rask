"""Every refusal the catalog gives for a table id is counted under that id, so one stuck table can page.

`compaction.datasets.refused` cannot page on one table: measured 2026-09-24, 45.3-46.5% of every sweep
is refused, most of it someone else's clone, and `MaintenanceRefusalsRising` needs more than half.
A rename leaves the old id holding no tuples, and maintenance derives the id from the location, so on a
governed estate the renamed table is refused 403 at the vend or plan door on every tick and is never
maintained again. `compaction.tables.parked` counts each such refusal with `table_id` and a closed
`refused_by` (`vend_denied`, `plan_denied`, `table_not_governed`), so `MaintenanceTableParked` can fire
on the same table staying refused.

Only the catalog's answers about the id are counted there. A vend the catalog answers 200 for a location
that does not cover the dataset is maintenance's own refusal (`governed_elsewhere`): the table it names is
healthy, and granting it changes nothing. A 401 at either door (`unauthenticated`) is about maintenance's
own service credential, which every table would report at once.

There is no unlabelled point: an absent series is the healthy estate, and a zero could name no table.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import lance
import pyarrow as pa
import pytest
import respx

from maintenance.core.config import MaintenanceSettings
from maintenance.services import catalog_compaction, credentials, sweep
from maintenance.services.optimize import DatasetResult
from service_kit.lakehouse import base_refs
from service_kit.lakehouse.work_items import DatasetPlan, DatasetWorkItem


CATALOG = "http://catalog.test"
TABLE_ID = "acme-bronze$events"
VEND_URL = f"{CATALOG}/management/v1/table/{TABLE_ID}/credentials"
PLAN_URL = f"{CATALOG}/management/v1/table/{TABLE_ID}/compaction_plan"
PERMISSION_DENIED = {"title": "PermissionDeniedError", "status": 403, "code": 15, "detail": f"permission denied: can_maintain on table:{TABLE_ID}"}

type Recorded = list[tuple[int, dict[str, Any] | None]]
#: The vend door's answer, given the dataset's uri (a covering location names it).
type VendAnswer = Callable[[str], httpx.Response]


@pytest.fixture(autouse=True)
def _identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog_compaction, "service_headers", lambda _settings: {})
    monkeypatch.setattr(credentials, "service_headers", lambda _settings: {})


def _stamped(tmp_path: Path, *, fragments: int) -> str:
    uri = str(tmp_path / "t.lance")
    for i in range(fragments):
        rows = pa.table({"id": pa.array(range(i * 10, (i + 1) * 10), pa.int64())}).replace_schema_metadata({b"lineage.dataset_id": TABLE_ID.encode()})
        lance.write_dataset(rows, uri, mode="create" if i == 0 else "append")
    return uri


def _vended_at(location: str | None) -> VendAnswer:
    """A direct write credential the catalog says covers `location`; ``None`` covers the dataset itself."""
    return lambda uri: httpx.Response(200, json={"mode": "direct", "credentials": {"storage_options": {}}, "location": location or uri})


def _refused(status: int, body: dict[str, Any]) -> VendAnswer:
    return lambda _uri: httpx.Response(status, json=body)


def _execute(uri: str, *, vend: VendAnswer, plan_answer: httpx.Response | None, protected_by: str | None = None) -> tuple[DatasetResult, respx.Route]:
    """One unit through the real vend and plan clients; returns the result and the vend door's route."""
    settings = MaintenanceSettings.model_validate({"s3_bucket": "lake", "distributed_compaction": plan_answer is not None, "catalog_url": CATALOG})
    item = DatasetWorkItem(uri=uri, plan=DatasetPlan(older_than=timedelta(0), retain_versions=1), protected_by=protected_by)
    with respx.mock(assert_all_called=False) as router:
        vend_route = router.post(VEND_URL).mock(return_value=vend(uri))
        if plan_answer is not None:
            router.post(PLAN_URL).mock(return_value=plan_answer)
        return sweep.execute_unit(item, settings=settings, options={}, now=datetime.now(UTC)), vend_route


@pytest.mark.parametrize(
    ("vend", "plan_answer", "gate"),
    [
        pytest.param(_refused(403, PERMISSION_DENIED), None, "vend_denied", id="vend-door-403"),
    ],
)
def test_each_catalog_refusal_is_counted_under_its_table_and_its_door(
    tmp_path: Path, parked_added: Recorded, refused_added: Recorded, vend: VendAnswer, plan_answer: httpx.Response | None, gate: str
) -> None:
    _execute(_stamped(tmp_path, fragments=3), vend=vend, plan_answer=plan_answer)

    assert parked_added == [(1, {"table_id": TABLE_ID, "refused_by": gate})], parked_added
    assert refused_added == [(1, {"refused_by": gate})], f"the refusal reached the datasets counter unlabelled or not at all: {refused_added}"


@pytest.mark.parametrize(
    ("plan_answer", "gate"),
    [
        pytest.param(httpx.Response(403, json=PERMISSION_DENIED), "plan_denied", id="plan-door-403"),
    ],
)
def test_a_unit_that_vends_nothing_is_counted_under_its_stamp(tmp_path: Path, parked_added: Recorded, plan_answer: httpx.Response, gate: str) -> None:
    """One fragment, one version, no index: the probe skips the vend, and the plan door is asked under the stamp."""
    result, vend_route = _execute(_stamped(tmp_path, fragments=1), vend=_vended_at(None), plan_answer=plan_answer)

    assert vend_route.call_count == 0, "the fixture vended, so it does not reach the path that passes no table id"
    assert result.refused_table_id == TABLE_ID, result.refused_table_id
    assert parked_added == [(1, {"table_id": TABLE_ID, "refused_by": gate})], parked_added


def test_a_layout_refusal_is_not_a_table_the_catalog_refused(tmp_path: Path, parked_added: Recorded, refused_added: Recorded) -> None:
    """A clone's source is refused by the estate's own pre-pass, under a location that may name no table."""
    uri = _stamped(tmp_path, fragments=3)

    _execute(uri, vend=_vended_at(None), plan_answer=None, protected_by=base_refs.normalise(uri))

    assert refused_added == [(1, {"refused_by": "protected_base"})], "the fixture did not reach the protected-base refusal"
    assert parked_added == [], parked_added


@pytest.mark.parametrize("gate", ["manifest_flags"])
def test_a_layout_gate_is_not_counted_even_beside_an_id(gate: str, parked_added: Recorded) -> None:
    """The label set is the catalog's three answers, whatever else a result carries."""
    sweep._record_parked(DatasetResult.model_validate({"uri": "s3://b/t", "refused": "no", "refused_by": gate, "refused_table_id": TABLE_ID}))

    assert parked_added == [], parked_added
