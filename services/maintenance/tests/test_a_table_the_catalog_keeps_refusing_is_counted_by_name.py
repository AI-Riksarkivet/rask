"""Every refusal the catalog gives for a table id is counted under that id, so one stuck table can page.

`compaction.datasets.refused` cannot page on one table: measured 2026-09-24, 45.3-46.5% of every sweep
is refused, most of it someone else's clone, and `MaintenanceRefusalsRising` needs more than half.
A stamp or a stale id the catalog refuses 403 at the vend door is refused on every tick and never maintained
again. `compaction.tables.parked` counts each such refusal with `table_id` and a closed `refused_by`
(`vend_denied`, `table_not_governed`), so `MaintenanceTableParked` can fire on the same table staying refused.

Only the catalog's answers about the id are counted there. A vend the catalog answers 200 for a location
that does not cover the dataset is maintenance's own refusal (`governed_elsewhere`): the table it names is
healthy, and granting it changes nothing. A 401 (`unauthenticated`) is about maintenance's
own service credential, which every table would report at once.

There is no unlabelled point: an absent series is the healthy estate, and a zero could name no table.
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
from maintenance.services import credentials, sweep
from maintenance.services.expiry import DatasetResult
from maintenance.services.plan import DatasetPlan
from service_kit.lakehouse import base_refs


CATALOG = "http://catalog.test"
TABLE_ID = "acme-bronze$events"
VEND_URL = f"{CATALOG}/management/v1/table/{TABLE_ID}/credentials"
PERMISSION_DENIED = {"title": "PermissionDeniedError", "status": 403, "code": 15, "detail": f"permission denied: can_maintain on table:{TABLE_ID}"}

type Recorded = list[tuple[int, dict[str, Any] | None]]
#: The vend door's answer, given the dataset's uri (a covering location names it).
type VendAnswer = Callable[[str], httpx.Response]


@pytest.fixture(autouse=True)
def _identity(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _execute(maintain_one: Callable[..., DatasetResult], uri: str, *, vend: VendAnswer, protected: base_refs.BaseRefs | None = None) -> DatasetResult:
    """One dataset's pass through the real vend client."""
    settings = MaintenanceSettings.model_validate({"s3_bucket": "lake", "catalog_url": CATALOG, "min_versions_to_expire": 1})
    with respx.mock(assert_all_called=False) as router:
        router.post(VEND_URL).mock(return_value=vend(uri))
        return maintain_one(uri, settings, protected=protected, plan=DatasetPlan(older_than=timedelta(0), retain_versions=1))


def test_a_vend_refusal_is_counted_under_its_table_and_its_door(
    *, tmp_path: Path, maintain_one: Callable[..., DatasetResult], parked_added: Recorded, refused_added: Recorded
) -> None:
    _execute(maintain_one=maintain_one, uri=_stamped(tmp_path, fragments=3), vend=_refused(403, PERMISSION_DENIED))

    assert parked_added == [(1, {"table_id": TABLE_ID, "refused_by": "vend_denied"})], parked_added
    assert refused_added == [(1, {"refused_by": "vend_denied"})], f"the refusal reached the datasets counter unlabelled or not at all: {refused_added}"


def test_a_layout_refusal_is_not_a_table_the_catalog_refused(
    *, tmp_path: Path, maintain_one: Callable[..., DatasetResult], parked_added: Recorded, refused_added: Recorded
) -> None:
    """A clone's source is refused by the estate's own pre-pass, under a location that may name no table."""
    uri = _stamped(tmp_path, fragments=3)

    _execute(maintain_one=maintain_one, uri=uri, vend=_vended_at(None), protected=base_refs.BaseRefs(protected={base_refs.normalise(uri)}))

    assert refused_added == [(1, {"refused_by": "protected_base"})], "the fixture did not reach the protected-base refusal"
    assert parked_added == [], parked_added


@pytest.mark.parametrize("gate", ["manifest_flags"])
def test_a_layout_gate_is_not_counted_even_beside_an_id(gate: str, parked_added: Recorded) -> None:
    """The label set is the catalog's answers, whatever else a result carries."""
    sweep._record_parked(DatasetResult.model_validate({"uri": "s3://b/t", "refused": "no", "refused_by": gate, "refused_table_id": TABLE_ID}))

    assert parked_added == [], parked_added
