"""[[LH-215]] A rename carries what the table holds under its name, and a detach ends it.

A lance-ns table id is its name path, and Lance keeps no table UUID (`lance_docs/ns_catalog/catalog/dir/index.md`;
RenameTable, `lance_docs/ns_catalog/spec.yaml`), so every grant and every control-root record rask attaches to a table
is keyed by its name. A rename retires the name: what the table held must arrive at the new one and leave nothing at
the old, where it would govern no table, or the next one created there.

The real catalog app over a real `dir` namespace on pylance, the in-memory `TupleStore` behind the real
`service_kit.governed.fga` seed and revoke.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import MagicMock

import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from catalog.core.config import get_settings
from catalog.services import warehouses
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from tests.integration.openfga_grants import TupleStore, act_as


_ROWS = pa.table({"id": pa.array([1, 2], pa.int64())})


@pytest.fixture
def governed(real_ns_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[TupleStore]:
    """The real catalog with OIDC and FGA on, as alice, over an in-memory tuple store, with one table ``db$src``."""
    app = real_ns_client.app
    settings = get_settings().model_copy(update={"oidc_enabled": True, "oidc_issuer": "https://idp.example", "oidc_audience": "lance", "fga_enabled": True})
    app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(app.state, "oidc", MagicMock())
    act_as(real_ns_client, "alice")
    store = TupleStore()
    monkeypatch.setattr(app.state, "fga", store)
    real_ns_client.headers["Authorization"] = "Bearer t"
    _ok(real_ns_client, "/v1/namespace/db/create", json={})
    _ok(real_ns_client, "/v1/table/db$src/create", content=encode_arrow_stream(_ROWS), headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
    yield store


def _ok(client: TestClient, path: str, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401 — the request's own keywords
    answer = client.post(path, **kwargs)
    assert answer.status_code == 200, f"{path}: {answer.text}"
    return answer.json()


def _protected(client: TestClient, table: str) -> bool:
    answer = client.get(f"/management/v1/table/{table}/protection")
    assert answer.status_code == 200, answer.text
    return bool(answer.json()["protected"])


def test_a_forced_rename_carries_a_direct_grant_protection_and_policy_and_leaves_nothing_at_the_old_id(
    real_ns_client: TestClient, governed: TupleStore
) -> None:
    _ok(real_ns_client, "/management/v1/table/db$src/access/grant", json={"user": "bob", "relation": "reader"})
    _ok(real_ns_client, "/management/v1/table/db$src/protection", json={"protected": True})
    _ok(real_ns_client, "/management/v1/table/db$src/policy/set", json={"retention_days": 7})

    _ok(real_ns_client, "/v1/table/db$src/rename", params={"force": "true"}, json={"new_table_name": "dst"})

    assert ("user:bob", "reader", "table:db$dst") in governed.tuples, f"bob's reader grant did not follow the table: {sorted(governed.tuples)}"
    assert governed.naming(["table:db$src"]) == {}, "a tuple still names the old id, which no table holds"
    assert _protected(real_ns_client, "db$dst"), "the renamed table arrived unprotected"
    assert not _protected(real_ns_client, "db$src"), "the old id is still protected, so a table created there is born protected"
    assert _ok(real_ns_client, "/management/v1/table/db$dst/policy/describe")["retention_days"] == 7
    assert real_ns_client.post("/management/v1/table/db$src/policy/describe").status_code == 404


@pytest.mark.usefixtures("governed")
def test_a_deregister_ends_the_tables_policy(real_ns_client: TestClient) -> None:
    _ok(real_ns_client, "/management/v1/table/db$src/policy/set", json={"retention_days": 7})

    _ok(real_ns_client, "/v1/table/db$src/deregister", json={})

    assert real_ns_client.post("/management/v1/table/db$src/policy/describe").status_code == 404, "a table registered at this id would inherit the policy"


def test_a_rename_into_another_tenant_refuses_to_carry_a_role_the_grant_door_would_refuse(
    real_ns_client: TestClient, governed: TupleStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A carried grant is a grant written on the destination, so it is held to the grant door's tenant rule ([[LH-062]]):
    acme's role on a table renamed into beta's namespace is refused before anything moves."""
    tenants = {"db": "acme", "other": "beta"}
    monkeypatch.setattr(warehouses, "project_for_namespace", lambda _root, _so, top: tenants.get(top))
    _ok(real_ns_client, "/v1/namespace/other/create", json={})
    governed.tuples |= {("project:acme", "project", "role:analyst"), ("role:analyst#assignee", "reader", "table:db$src")}

    refused = real_ns_client.post("/v1/table/db$src/rename", json={"new_table_name": "dst", "new_namespace_id": ["other"]})

    assert refused.status_code == 400, refused.text
    assert "acme" in refused.json()["detail"] and "beta" in refused.json()["detail"], refused.text
    assert real_ns_client.post("/v1/table/db$src/describe", json={}).status_code == 200, "the refused rename moved the table"
