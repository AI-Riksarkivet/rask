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
from typing import Any, get_args

import httpx
import lance
import pyarrow as pa
import pydantic
import pytest
import respx

from maintenance.core import metrics
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
TABLE_NOT_FOUND = {"title": "TableNotFoundError", "status": 404, "code": 4, "detail": f"Table not found: table id '{TABLE_ID}'"}
UNAUTHENTICATED = {"title": "UnauthenticatedError", "status": 401, "code": 16, "detail": "the presented credential may not claim 'service-maintenance'"}

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
        pytest.param(_refused(404, TABLE_NOT_FOUND), None, "table_not_governed", id="vend-door-404"),
        pytest.param(_vended_at(None), httpx.Response(403, json=PERMISSION_DENIED), "plan_denied", id="plan-door-403"),
        pytest.param(_vended_at(None), httpx.Response(404, json=TABLE_NOT_FOUND), "table_not_governed", id="plan-door-404"),
    ],
)
def test_each_catalog_refusal_is_counted_under_its_table_and_its_door(
    tmp_path: Path, parked_added: Recorded, refused_added: Recorded, vend: VendAnswer, plan_answer: httpx.Response | None, gate: str
) -> None:
    _execute(_stamped(tmp_path, fragments=3), vend=vend, plan_answer=plan_answer)

    assert parked_added == [(1, {"table_id": TABLE_ID, "refused_by": gate})], parked_added
    assert refused_added == [(1, {"refused_by": gate})], f"the refusal reached the datasets counter unlabelled or not at all: {refused_added}"


@pytest.mark.parametrize(
    ("vend", "plan_answer"),
    [
        pytest.param(_refused(401, UNAUTHENTICATED), None, id="vend-door-401"),
        pytest.param(_vended_at(None), httpx.Response(401, json=UNAUTHENTICATED), id="plan-door-401"),
    ],
)
def test_a_401_is_maintenances_own_credential_not_a_table_the_catalog_refused(
    tmp_path: Path, parked_added: Recorded, refused_added: Recorded, vend: VendAnswer, plan_answer: httpx.Response | None
) -> None:
    """A rejected service token is refused at every table alike; counting it per table would page for each one."""
    result, _ = _execute(_stamped(tmp_path, fragments=3), vend=vend, plan_answer=plan_answer)

    assert (result.refused_by, result.refused_table_id) == ("unauthenticated", None), (result.refused_by, result.refused_table_id)
    assert refused_added == [(1, {"refused_by": "unauthenticated"})], refused_added
    assert parked_added == [], f"maintenance's own credential was counted as the catalog refusing the table: {parked_added}"


def test_a_location_the_catalog_governs_elsewhere_is_not_a_catalog_refusal(tmp_path: Path, parked_added: Recorded, refused_added: Recorded) -> None:
    """The vend door answered 200, for a table that lives somewhere else: the governed table is healthy."""
    result, vend_route = _execute(_stamped(tmp_path, fragments=3), vend=_vended_at("s3://acme-bucket/medallion/bronze"), plan_answer=None)

    assert vend_route.call_count == 1, "the fixture never reached the vend door"
    assert (result.refused_by, result.refused_table_id) == ("governed_elsewhere", None), (result.refused_by, result.refused_table_id)
    assert refused_added == [(1, {"refused_by": "governed_elsewhere"})], refused_added
    assert parked_added == [], f"a table the catalog vended for was counted as one it refused: {parked_added}"


@pytest.mark.parametrize(
    ("plan_answer", "gate"),
    [
        pytest.param(httpx.Response(403, json=PERMISSION_DENIED), "plan_denied", id="plan-door-403"),
        pytest.param(httpx.Response(404, json=TABLE_NOT_FOUND), "table_not_governed", id="plan-door-404"),
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


@pytest.mark.parametrize("gate", ["protected_base", "manifest_flags", "invalid_ref", "governed_elsewhere", "unauthenticated"])
def test_a_layout_gate_is_not_counted_even_beside_an_id(gate: str, parked_added: Recorded) -> None:
    """The label set is the catalog's three answers, whatever else a result carries."""
    sweep._record_parked(DatasetResult.model_validate({"uri": "s3://b/t", "refused": "no", "refused_by": gate, "refused_table_id": TABLE_ID}))

    assert parked_added == [], parked_added


def test_a_maintained_table_is_not_counted(tmp_path: Path, parked_added: Recorded) -> None:
    _execute(_stamped(tmp_path, fragments=3), vend=_vended_at(None), plan_answer=None)

    assert parked_added == [], parked_added


def test_the_tick_adds_no_unlabelled_point(monkeypatch: pytest.MonkeyPatch, parked_added: Recorded) -> None:
    monkeypatch.setattr(sweep, "_load_policies", lambda *_a, **_k: [])
    monkeypatch.setattr(sweep, "_trash_exclusions", lambda *_a, **_k: {})
    monkeypatch.setattr(sweep, "_s3fs", lambda *_a, **_k: object())
    monkeypatch.setattr(sweep, "_buckets_to_sweep", lambda *_a, **_k: ["b"])
    monkeypatch.setattr(sweep, "_discover_all", lambda *_a, **_k: [])
    monkeypatch.setattr(sweep, "_protected_roots", lambda *_a, **_k: base_refs.BaseRefs())
    settings = MaintenanceSettings.model_validate({"s3_bucket": "lake", "distributed_compaction": True, "catalog_url": CATALOG})

    assert sweep.plan_sweep(settings) == ([], [])
    assert parked_added == [], parked_added


def test_the_label_set_is_closed() -> None:
    """The three doors, and a gate outside the result's vocabulary cannot be recorded at all."""
    assert set(get_args(metrics.CatalogRefusal.__value__)) == {"vend_denied", "plan_denied", "table_not_governed"}
    with pytest.raises(pydantic.ValidationError):
        DatasetResult.model_validate({"uri": "s3://b/t", "refused": "no", "refused_by": "denied"})
