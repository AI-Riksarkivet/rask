"""A table the catalog refuses is left alone for the tick: no version cleanup, no disarm commit.

The catalog answers for a table id at the vend door, and each answer stops the whole dataset:

    403    `vend_denied`         this identity may not maintain the table
    404    `table_not_governed`  no table or namespace by this id (spec.yaml codes 4 and 1)

Every step after a refusal would be signed by the credential that opened the dataset, which for a table
the catalog declined is the deployment's ambient key. Two more answers stop it for the same reason and are
not the catalog refusing the id: a vend that answers 200 for a location that does not cover the dataset
(`governed_elsewhere`), and a 401 (`unauthenticated`, maintenance's own credential).

With OIDC and FGA on (measured 2026-09-26), an id a rename left behind holds no tuples and the vend door
answers it 403, code 15: its alternative rung goes through `fga_deps._require_any`, which never converts a
denial to a 404.

The fixture carries seven versions, and a cleanup keeping one leaves one, so an unchanged count of seven proves
it did not run. The vend door is driven through the real client over respx.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import lance
import pyarrow as pa
import pytest
import respx

from maintenance.core.config import MaintenanceSettings
from maintenance.services import credentials
from maintenance.services.expiry import DatasetResult
from maintenance.services.plan import DatasetPlan


CATALOG = "http://catalog.test"
TABLE_ID = "acme-bronze$events"
VEND_URL = f"{CATALOG}/management/v1/table/{TABLE_ID}/credentials"
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
    monkeypatch.setattr(credentials, "service_headers", lambda _settings: {})


def _indexed(tmp_path: Path) -> str:
    """Six fragments, a BTREE index built before five of them: seven versions."""
    uri = str(tmp_path / "t.lance")
    for i in range(6):
        rows = pa.table({"id": pa.array(range(i * 10, (i + 1) * 10), pa.int64())})
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


def _execute(maintain_one: Callable[..., DatasetResult], uri: str, *, vend: VendAnswer) -> DatasetResult:
    settings = MaintenanceSettings.model_validate({"s3_bucket": "lake", "catalog_url": CATALOG, "min_versions_to_expire": 1})
    with respx.mock(assert_all_called=False) as router:
        router.post(VEND_URL).mock(return_value=vend(uri))
        return maintain_one(uri, settings, plan=CLEANUP_EVERYTHING, table_id=TABLE_ID)


REFUSALS = [
    pytest.param(_refused(403, PERMISSION_DENIED), "vend_denied", TABLE_ID, id="vend-door-403"),
    pytest.param(_refused(404, TABLE_NOT_FOUND), "table_not_governed", TABLE_ID, id="vend-door-404"),
    pytest.param(_vended_at(ELSEWHERE), "governed_elsewhere", None, id="vend-location-elsewhere"),
    pytest.param(_refused(401, UNAUTHENTICATED), "unauthenticated", None, id="vend-door-401"),
]


@pytest.mark.parametrize(("vend", "gate", "refused_table_id"), REFUSALS)
def test_a_refused_table_is_not_cleaned(
    *, tmp_path: Path, maintain_one: Callable[..., DatasetResult], vend: VendAnswer, gate: str, refused_table_id: str | None
) -> None:
    uri = _indexed(tmp_path)

    result = _execute(maintain_one, uri, vend=vend)

    assert len(lance.dataset(uri).versions()) == 7, "a refused table had its versions cleaned up"
    assert result.old_versions_removed == 0, result.old_versions_removed
    assert (result.refused_by, result.error) == (gate, None), (result.refused_by, result.error)
    assert result.refused is not None and TABLE_ID in result.refused, f"the refusal carries no reason: {result.refused!r}"
    assert result.refused_table_id == refused_table_id, result.refused_table_id
    assert result.data_storage_version == "2.2", f"the refusal dropped the manifest's version from the census: {result.data_storage_version!r}"


def test_the_fixture_is_cleaned_when_nothing_refuses_it(tmp_path: Path, maintain_one: Callable[..., DatasetResult]) -> None:
    """The control: the cleanup has work to do here, so an unchanged table means it was skipped."""
    uri = _indexed(tmp_path)

    result = _execute(maintain_one, uri, vend=_vended_at(None))

    assert (result.refused, result.error) == (None, None), (result.refused, result.error)
    assert len(lance.dataset(uri).get_fragments()) == 6, "maintenance rewrote fragments"
    assert len(lance.dataset(uri).versions()) == 1, "the cleanup reclaimed nothing"
