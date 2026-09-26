"""`scripts/backfill_declared_dataset_id.py` skips a table the catalog answered about, and plans the rest.

The vend door answers about the id in three ways that each forbid the ambient key: a 401 or 403, a 404
naming no table or namespace (`TableNotGoverned`), or a 200 at a location that does not cover the dataset
(`GovernedElsewhere`). The 404 is the ordinary one: `catalog.trashGraceDays` defaults to 7, and a
recoverable drop keeps both its bytes in the `<uuid8>_<ns>$<table>` layout this script walks and its
tuples, so the door answers 404 code 4 for a table dropped that week.

Each case puts the refused dataset FIRST, so a refusal that escapes `_plan()` never plans the healthy one.
The real `credentials.write_options_for` answers both over respx.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from maintenance.core import config as maintenance_config
from maintenance.core.config import MaintenanceSettings
from maintenance.services import credentials, optimize
from maintenance.services.optimize import Discovery
from service_kit.governed import secrets
from service_kit.lakehouse import objectfs, warehouse_records


REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location("backfill_declared_dataset_id", REPO_ROOT / "scripts" / "backfill_declared_dataset_id.py")
assert _SPEC and _SPEC.loader
backfill = importlib.util.module_from_spec(_SPEC)
sys.modules["backfill_declared_dataset_id"] = backfill
_SPEC.loader.exec_module(backfill)

CATALOG = "http://catalog.test"
REFUSED = "s3://wh/4c49d010_acme-bronze$events"
HEALTHY = "s3://wh/5d5ae121_acme-bronze$pages"
ELSEWHERE = "s3://acme-bucket/medallion/bronze"
VENDED = {"aws_access_key_id": "scoped"}


def _vended_at(location: str) -> httpx.Response:
    return httpx.Response(200, json={"mode": "direct", "credentials": {"storage_options": VENDED}, "location": location})


@pytest.fixture(autouse=True)
def _one_bucket_holding_both(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = MaintenanceSettings.model_validate({"s3_bucket": "wh", "catalog_url": CATALOG})
    monkeypatch.setattr(maintenance_config, "MaintenanceSettings", lambda: settings)
    monkeypatch.setattr(secrets, "apply_dapr_secrets", lambda _settings: None)
    monkeypatch.setattr(warehouse_records, "list_warehouse_records", lambda *_a, **_k: [])
    monkeypatch.setattr(warehouse_records, "maintainable_buckets", lambda _registry: [])
    monkeypatch.setattr(objectfs, "s3_filesystem", lambda _options: object())
    monkeypatch.setattr(optimize, "discover_datasets", lambda _fs, _bucket: Discovery(uris=[REFUSED, HEALTHY]))
    monkeypatch.setattr(credentials, "service_headers", lambda _settings: {})


@pytest.mark.parametrize(
    ("answer", "names"),
    [
        pytest.param(httpx.Response(404, json={"code": 4, "detail": "Table not found"}), "TABLE_NOT_FOUND", id="vend-door-404-table"),
        pytest.param(httpx.Response(404, json={"code": 1, "detail": "Namespace not found"}), "NAMESPACE_NOT_FOUND", id="vend-door-404-namespace"),
        pytest.param(httpx.Response(403, json={"code": 15, "detail": "permission denied"}), "(403)", id="vend-door-403"),
        pytest.param(httpx.Response(401, json={"detail": "token rejected"}), "(401)", id="vend-door-401"),
        pytest.param(_vended_at(ELSEWHERE), ELSEWHERE, id="vend-location-elsewhere"),
    ],
)
def test_a_table_the_catalog_answered_about_is_skipped_and_the_next_is_planned(capsys: pytest.CaptureFixture[str], answer: httpx.Response, names: str) -> None:
    with respx.mock() as router:
        refused_route = router.post(f"{CATALOG}/management/v1/table/acme-bronze$events/credentials").mock(return_value=answer)
        router.post(f"{CATALOG}/management/v1/table/acme-bronze$pages/credentials").mock(return_value=_vended_at(HEALTHY))
        plan: list[tuple[str, str, dict[str, Any] | None]] = backfill._plan()

    assert refused_route.call_count == 1, "the fixture never reached the refused table's door"
    assert plan == [(HEALTHY, "acme-bronze$pages", VENDED)], f"the refused table was planned, or the healthy one was not: {plan}"
    skip = [line for line in capsys.readouterr().out.splitlines() if "acme-bronze$events" in line]
    assert len(skip) == 1 and names in skip[0], f"the skip does not state the catalog's answer ({names!r}): {skip}"


def test_a_location_elsewhere_is_not_worded_as_a_refusal(capsys: pytest.CaptureFixture[str]) -> None:
    """The door answered 200: the table it names is healthy, so "refuses" would send an operator to grant it."""
    with respx.mock() as router:
        router.post(f"{CATALOG}/management/v1/table/acme-bronze$events/credentials").mock(return_value=_vended_at(ELSEWHERE))
        router.post(f"{CATALOG}/management/v1/table/acme-bronze$pages/credentials").mock(return_value=_vended_at(HEALTHY))
        backfill._plan()

    skip = next(line for line in capsys.readouterr().out.splitlines() if "acme-bronze$events" in line)
    assert "refuse" not in skip.lower(), skip
