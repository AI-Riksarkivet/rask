"""A table's records are keyed by its uuid, so a rename moves none of them and a new table under an old name inherits none.

The catalog mints a table's uuid when the table comes into being and a rename keeps it ([[LH-519]], ADR 0173 §2). Every
control-root record kept for the table names that uuid (ADR 0173 §8, [[LH-523]]): its protection, its maintenance
policy, a promotion hold, maintenance's due record and an erasure obligation. A rename changes the table's name and
writes no record, and every surface that lists one names the table by its name now. A table created later under a
name another table gave up is given a new uuid, so nothing kept for the old table governs it, even when the drop that
ended the old table failed to end its records.

Grants are keyed by the uuid too, so a rename writes no tuple; that half is pinned against a real OpenFGA by
``services/catalog/tests/test_a_grant_is_held_by_the_objects_uuid.py``.

The real catalog app over a real `dir` namespace on pylance, the in-memory `TupleStore` behind the real
`service_kit.governed.fga` seed and revoke; maintenance's own policy resolution over the same control root.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from catalog.core.config import get_settings
from catalog.services import promotions, warehouses
from maintenance.core.config import MaintenanceSettings
from maintenance.services.due import table_ids
from maintenance.services.naming import policy_owners
from maintenance.services.plan import PlanContext, load_policies, resolve_plan
from service_kit.lakehouse import location_claims, maintenance_due, object_records
from service_kit.lakehouse.maintenance_due import CompactionDue, DueRecord
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from tests.integration.openfga_grants import TupleStore, act_as


_ROWS = pa.table({"id": pa.array([1, 2], pa.int64())})
#: The control-root registries a table's own records live in.
_RECORD_PREFIXES = ("_protection", "_policies", "_promotions", "_maintenance_due", "_erasure_obligations")


@pytest.fixture
def governed(real_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[TupleStore]:
    """The real catalog with OIDC and FGA on, as the person `dex~alice`, over an in-memory tuple store, with one table ``db$src``."""
    app = real_ns_client.app
    settings = get_settings().model_copy(
        update={"oidc_enabled": True, "oidc_connectors": {"local": "dex"}, "oidc_issuer": "https://idp.example", "oidc_audience": "lance", "fga_enabled": True}
    )
    app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(app.state, "oidc", MagicMock())
    act_as(real_ns_client, "alice")
    store = TupleStore()
    monkeypatch.setattr(app.state, "fga", store)
    real_ns_client.headers["Authorization"] = "Bearer t"
    _create(real_ns_client, "db$src", namespace=True)
    yield store


def _ok(client: TestClient, path: str, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401 — the request's own keywords
    answer = client.post(path, **kwargs)
    assert answer.status_code == 200, f"{path}: {answer.text}"
    return answer.json()


def _create(client: TestClient, table: str, *, namespace: bool = False) -> None:
    if namespace:
        _ok(client, f"/v1/namespace/{table.split('$')[0]}/create", json={})
    _ok(client, f"/v1/table/{table}/create", content=encode_arrow_stream(_ROWS), headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})


def _protected(client: TestClient, table: str) -> bool:
    answer = client.get(f"/management/v1/table/{table}/protection")
    assert answer.status_code == 200, answer.text
    return bool(answer.json()["protected"])


def _described(client: TestClient, table: str) -> tuple[str, str]:
    """The table's uuid and location, as DescribeTable answers them."""
    described = _ok(client, f"/v1/table/{table}/describe", json={})
    properties = described.get("properties") or {}
    assert "rask.uuid" in properties, f"{table} answers no uuid: {properties}"
    return str(properties["rask.uuid"]), str(described["location"])


def _record_keys(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for prefix in _RECORD_PREFIXES for path in (root / prefix).rglob("*.json")}


def _listed(client: TestClient, path: str, field: str) -> list[str]:
    answer = client.get(path)
    assert answer.status_code == 200, f"{path}: {answer.text}"
    return [str(entry["table"]) for entry in answer.json()[field]]


def test_a_rename_keeps_the_tables_protection_and_policy_and_writes_no_record(real_ns_client: TestClient, governed: TupleStore, tmp_path: Path) -> None:
    _ok(real_ns_client, "/management/v1/table/db$src/protection", json={"protected": True})
    _ok(real_ns_client, "/management/v1/table/db$src/policy/set", json={"retention_days": 7})
    minted, location = _described(real_ns_client, "db$src")
    records = _record_keys(tmp_path)

    _ok(real_ns_client, "/v1/table/db$src/rename", params={"force": "true"}, json={"new_table_name": "dst"})

    assert _record_keys(tmp_path) == records, "the rename wrote or removed a record"
    assert _described(real_ns_client, "db$dst")[0] == minted, "the renamed table answers another uuid"
    assert _protected(real_ns_client, "db$dst"), "the renamed table arrived unprotected"
    assert _ok(real_ns_client, "/management/v1/table/db$dst/policy/describe")["retention_days"] == 7
    settings = MaintenanceSettings.model_validate({"MAINTENANCE_POLICY_ROOT": str(tmp_path), "MAINTENANCE_CONTROL_ROOT": str(tmp_path)})
    named = table_ids({str(tmp_path)}, {})
    owners = policy_owners(named, "db$dst", claims=location_claims.ClaimStore(control_root=str(tmp_path)))
    assert owners is not None, "maintenance reads no uuid for the renamed table"
    context = PlanContext(policies=load_policies(settings, {}), now=datetime.now(UTC), older_than=timedelta(days=30))
    plan = resolve_plan(location, context=context, owners=owners)
    assert (plan.policy or {}).get("uuid") == minted, f"maintenance resolves another policy for the renamed table: {plan.policy}"
    assert plan.older_than == timedelta(days=7)


@pytest.mark.usefixtures("governed")
def test_a_hold_a_due_record_and_an_erasure_obligation_are_listed_under_the_tables_new_name(real_ns_client: TestClient, tmp_path: Path) -> None:
    root = str(tmp_path)
    minted, location = _described(real_ns_client, "db$src")
    now = datetime.now(UTC)
    held = promotions.Hold(
        table="db$src",
        uuid=minted,
        location=location,
        version=1,
        published_version=None,
        reasons=["row_count_delta"],
        requested_by=None,
        opened_at=now,
        expires_at=now + timedelta(days=1),
    )
    promotions.open_hold(root, {}, held)
    due = DueRecord(
        table="db$src", uuid=minted, location=location, version=1, compaction=CompactionDue(fragments=4, candidate_fragments=4, tasks=1), evaluated_at=now
    )
    maintenance_due.write_due(root, {}, due)
    recorded = real_ns_client.post("/management/v1/table/db$src/erasure", json={"predicate": "id = 1", "retain_days": 0})
    assert recorded.status_code == 202, recorded.text

    _ok(real_ns_client, "/v1/table/db$src/rename", json={"new_table_name": "dst"})

    assert _listed(real_ns_client, "/management/v1/promotions", "holds") == ["db$dst"]
    assert _listed(real_ns_client, "/management/v1/maintenance/due", "tables") == ["db$dst"]
    assert _listed(real_ns_client, "/management/v1/erasures", "obligations") == ["db$dst"]


@pytest.mark.usefixtures("governed")
def test_a_table_created_under_a_dropped_name_inherits_none_of_the_old_tables_records(real_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Even when the drop's own ending of the old table's records is lost: no record names the new table's uuid."""
    _ok(real_ns_client, "/management/v1/table/db$src/protection", json={"protected": True})
    _ok(real_ns_client, "/management/v1/table/db$src/policy/set", json={"retention_days": 7})
    monkeypatch.setattr(object_records, "end_object_records", lambda *_args, **_kwargs: [])
    _ok(real_ns_client, "/v1/table/db$src/drop", params={"force": "true"}, json={})

    _create(real_ns_client, "db$src")

    assert not _protected(real_ns_client, "db$src"), "the new table was born protected by the dropped one's record"
    assert real_ns_client.post("/management/v1/table/db$src/policy/describe").status_code == 404, "the new table inherited the dropped one's policy"


def test_a_rename_into_another_tenant_refuses_to_carry_a_role_the_grant_door_would_refuse(
    real_ns_client: TestClient, governed: TupleStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A grant stays on the table's uuid and so follows it, so it is held to the grant door's tenant rule ([[LH-062]]):
    acme's role on a table renamed into beta's namespace is refused before anything moves."""
    tenants = {"db": "acme", "other": "beta"}
    monkeypatch.setattr(warehouses, "project_for_namespace", lambda _root, _so, top: tenants.get(top))
    _ok(real_ns_client, "/v1/namespace/other/create", json={})
    table = f"table:{_described(real_ns_client, 'db$src')[0]}"
    governed.tuples |= {("project:acme", "project", "role:analyst"), ("role:analyst#assignee", "reader", table)}

    refused = real_ns_client.post("/v1/table/db$src/rename", json={"new_table_name": "dst", "new_namespace_id": ["other"]})

    assert refused.status_code == 400, refused.text
    assert "acme" in refused.json()["detail"] and "beta" in refused.json()["detail"], refused.text
    assert real_ns_client.post("/v1/table/db$src/describe", json={}).status_code == 200, "the refused rename moved the table"
