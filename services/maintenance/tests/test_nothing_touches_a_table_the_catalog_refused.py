"""A table the catalog refuses is left alone for the tick: no rewrite, no index optimize, no cleanup.

The catalog answers for a table id at two doors, and each answer stops the whole dataset:

    vend door 403    `vend_denied`         this identity may not write the table
    vend door 404    `table_not_governed`  no table or namespace by this id (spec.yaml codes 4 and 1)
    plan door 403    `plan_denied`         this identity may not maintain the table
    plan door 404    `table_not_governed`  as at the vend door

Every step after a refusal would be signed by the credential that opened the dataset, which for a table
the catalog declined is the deployment's ambient key. Two more answers stop it for the same reason and are
not the catalog refusing the id: a vend that answers 200 for a location that does not cover the dataset
(`governed_elsewhere`), and a 401 at either door (`unauthenticated`, maintenance's own credential).

With OIDC and FGA on (measured 2026-09-26), an id a rename left behind holds no tuples and both doors
answer it 403, code 15: the plan door's `can_maintain` is not in `fga_deps._READ_RELATIONS`, and the vend
door's alternative rung goes through `fga_deps._require_any`, which never converts a denial to a 404.

The fixture carries a scalar index and seven versions. On pylance 12.0.0 `optimize_indices` over it commits
an eighth version and a cleanup keeping one leaves one, so an unchanged count of seven proves neither ran.
Both doors are driven through the real clients over respx.
"""

from __future__ import annotations

import logging
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
from maintenance.services import catalog_compaction, credentials, optimize, sweep
from maintenance.services.optimize import DatasetResult
from service_kit.lakehouse.work_items import DatasetPlan, DatasetWorkItem


CATALOG = "http://catalog.test"
TABLE_ID = "acme-bronze$events"
VEND_URL = f"{CATALOG}/management/v1/table/{TABLE_ID}/credentials"
PLAN_URL = f"{CATALOG}/management/v1/table/{TABLE_ID}/compaction_plan"
#: Where the catalog governs `TABLE_ID` in the crossing case: not the dataset the sweep holds.
ELSEWHERE = "s3://acme-bucket/medallion/bronze"
#: Keep only the newest version, so a cleanup that runs is visible as a version count.
CLEANUP_EVERYTHING = DatasetPlan(older_than=timedelta(0), retain_versions=1)
PERMISSION_DENIED = {
    "type": "https://lance.org/problems/permissiondeniederror",
    "title": "PermissionDeniedError",
    "status": 403,
    "code": 15,
    "detail": f"permission denied: can_maintain on table:{TABLE_ID}",
}
TABLE_NOT_FOUND = {
    "type": "https://lance.org/problems/tablenotfounderror",
    "title": "TableNotFoundError",
    "status": 404,
    "code": 4,
    "detail": f"Table not found: table id '{TABLE_ID}'",
}
UNAUTHENTICATED = {"title": "UnauthenticatedError", "status": 401, "code": 16, "detail": "the presented credential may not claim 'service-maintenance'"}

#: The vend door's answer, given the dataset's uri (a covering location names it).
type VendAnswer = Callable[[str], httpx.Response]


@pytest.fixture(autouse=True)
def _identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog_compaction, "service_headers", lambda _settings: {})
    monkeypatch.setattr(credentials, "service_headers", lambda _settings: {})


def _indexed(tmp_path: Path) -> str:
    """Six fragments stamped with `TABLE_ID`, a BTREE index built before five of them: seven versions."""
    uri = str(tmp_path / "t.lance")
    for i in range(6):
        rows = pa.table({"id": pa.array(range(i * 10, (i + 1) * 10), pa.int64())}).replace_schema_metadata({b"lineage.dataset_id": TABLE_ID.encode()})
        if i == 0:
            lance.write_dataset(rows, uri, mode="create", data_storage_version="2.2")
            lance.dataset(uri).create_scalar_index("id", "BTREE")
        else:
            lance.write_dataset(rows, uri, mode="append")
    return uri


def _vended_at(location: str | None) -> VendAnswer:
    """A direct write credential the catalog says covers `location`; ``None`` covers the dataset itself."""
    return lambda uri: httpx.Response(200, json={"mode": "direct", "credentials": {"storage_options": {}}, "location": location or uri})


def _refused(status: int, body: dict[str, Any]) -> VendAnswer:
    return lambda _uri: httpx.Response(status, json=body)


def _execute(uri: str, *, vend: VendAnswer, plan_answer: httpx.Response | None) -> DatasetResult:
    settings = MaintenanceSettings.model_validate({"s3_bucket": "lake", "distributed_compaction": plan_answer is not None, "catalog_url": CATALOG})
    with respx.mock(assert_all_called=False) as router:
        router.post(VEND_URL).mock(return_value=vend(uri))
        if plan_answer is not None:
            router.post(PLAN_URL).mock(return_value=plan_answer)
        return sweep.execute_unit(DatasetWorkItem(uri=uri, plan=CLEANUP_EVERYTHING), settings=settings, options={}, now=datetime.now(UTC))


REFUSALS = [
    pytest.param(_refused(403, PERMISSION_DENIED), None, "vend_denied", TABLE_ID, id="vend-door-403"),
    pytest.param(_refused(404, TABLE_NOT_FOUND), None, "table_not_governed", TABLE_ID, id="vend-door-404"),
    pytest.param(_vended_at(None), httpx.Response(403, json=PERMISSION_DENIED), "plan_denied", TABLE_ID, id="plan-door-403"),
    pytest.param(_vended_at(None), httpx.Response(404, json=TABLE_NOT_FOUND), "table_not_governed", TABLE_ID, id="plan-door-404"),
    pytest.param(_vended_at(ELSEWHERE), None, "governed_elsewhere", None, id="vend-location-elsewhere"),
    pytest.param(_refused(401, UNAUTHENTICATED), None, "unauthenticated", None, id="vend-door-401"),
    pytest.param(_vended_at(None), httpx.Response(401, json=UNAUTHENTICATED), "unauthenticated", None, id="plan-door-401"),
]


@pytest.mark.parametrize(("vend", "plan_answer", "gate", "refused_table_id"), REFUSALS)
def test_a_refused_table_is_neither_rewritten_nor_reindexed_nor_cleaned(
    tmp_path: Path, vend: VendAnswer, plan_answer: httpx.Response | None, gate: str, refused_table_id: str | None
) -> None:
    uri = _indexed(tmp_path)

    result = _execute(uri, vend=vend, plan_answer=plan_answer)

    dataset = lance.dataset(uri)
    assert len(dataset.get_fragments()) == 6, "a refused table was rewritten"
    assert len(dataset.versions()) == 7, "a refused table had its indices optimized or its versions cleaned up"
    assert (result.indices_optimized, result.old_versions_removed) == (0, 0), (result.indices_optimized, result.old_versions_removed)
    assert (result.refused_by, result.error) == (gate, None), (result.refused_by, result.error)
    assert result.refused is not None and TABLE_ID in result.refused, f"the refusal carries no reason: {result.refused!r}"
    assert result.refused_table_id == refused_table_id, result.refused_table_id
    assert result.data_storage_version == "2.2", f"the refusal dropped the manifest's version from the census: {result.data_storage_version!r}"


#: The WARNING each refusal logs, and the module that logs it. Neither counter carries the dataset, so this
#: line is what names it: the crossing's only such signal, and the one MaintenanceTableParked sends operators to.
WARNINGS = [
    pytest.param(_refused(403, PERMISSION_DENIED), None, sweep.__name__, "maintenance_vend_denied", id="vend-door-403"),
    pytest.param(_refused(404, TABLE_NOT_FOUND), None, sweep.__name__, "maintenance_table_not_governed", id="vend-door-404"),
    pytest.param(_refused(401, UNAUTHENTICATED), None, sweep.__name__, "maintenance_unauthenticated", id="vend-door-401"),
    pytest.param(_vended_at(ELSEWHERE), None, sweep.__name__, "maintenance_governed_elsewhere", id="vend-location-elsewhere"),
    pytest.param(_vended_at(None), httpx.Response(403, json=PERMISSION_DENIED), optimize.__name__, "maintenance_rewrite_denied", id="plan-door-403"),
    pytest.param(_vended_at(None), httpx.Response(404, json=TABLE_NOT_FOUND), optimize.__name__, "maintenance_table_not_governed", id="plan-door-404"),
    pytest.param(_vended_at(None), httpx.Response(401, json=UNAUTHENTICATED), optimize.__name__, "maintenance_unauthenticated", id="plan-door-401"),
]


@pytest.mark.parametrize(("vend", "plan_answer", "logger", "line"), WARNINGS)
def test_each_refusal_logs_one_WARNING_naming_the_dataset_and_the_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, vend: VendAnswer, plan_answer: httpx.Response | None, logger: str, line: str
) -> None:
    uri = _indexed(tmp_path)

    with caplog.at_level(logging.WARNING):
        _execute(uri, vend=vend, plan_answer=plan_answer)

    warned = [(r.name, r.levelno, getattr(r, "uri", None), getattr(r, "table_id", None)) for r in caplog.records if r.getMessage() == line]
    assert warned == [(logger, logging.WARNING, uri, TABLE_ID)], warned


def test_the_fixture_is_touched_when_nothing_refuses_it(tmp_path: Path) -> None:
    """The control: every step above has work to do here, so an unchanged table means it was skipped."""
    uri = _indexed(tmp_path)

    result = _execute(uri, vend=_vended_at(None), plan_answer=None)

    assert (result.refused, result.error, result.indices_optimized) == (None, None, 1), (result.refused, result.error, result.indices_optimized)
    assert len(lance.dataset(uri).get_fragments()) < 6, "the in-pod rewrite merged nothing"
    assert len(lance.dataset(uri).versions()) == 1, "the cleanup reclaimed nothing"
