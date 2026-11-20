"""A namespace's `rask.requires_run` binds every write beneath it to name its run, only an admin declares it, and every
write is judged under it.

ADR 0156 C8 and ADR 0211, on the real catalog app over real pylance with OIDC and FGA on, in a registered warehouse. The
OpenFGA store is an in-memory double behind the real `lakehouse.governed.fga` functions that answers a
check from an explicit grant set, so who may act IS the claim here. A namespace named `gold` that declares nothing is
judged by what it declares, never by its name.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from catalog.api.dependencies import get_namespace
from catalog.core.config import Settings, get_settings
from catalog.services import object_uuids, table_claims, warehouses
from lakehouse.governed import fga
from lakehouse.governed.audit import AUDIT_LOGGER
from lakehouse.storage.options import StorageOptions
from tests import seeding
from tests.integration.openfga_grants import Grants, act_as


AUTH = {"Authorization": "Bearer t"}
ARROW = {**AUTH, "content-type": "application/vnd.apache.arrow.stream"}
WAREHOUSE = "acme-bucket"
PROJECT = "acme"
SALES = f"{WAREHOUSE}$sales"
LEDGER = f"{WAREHOUSE}$ledger"


@pytest.fixture
def grants(real_ns_client: TestClient, tmp_path: Path) -> Iterator[Grants]:
    """The real catalog as the chart deploys it, with warehouse ``WAREHOUSE`` of project ``PROJECT`` at its root, its own
    row in that root's catalog as the warehouse create door leaves it.

    alice administers the project; bob may only create namespaces in its warehouse.
    """
    root = str(tmp_path)
    settings = Settings.model_validate(
        {
            "root": root,
            "oidc_enabled": True,
            "oidc_idp_id": "dex",
            "oidc_connectors": {"local": "dex"},
            "oidc_issuer": "https://idp.example",
            "oidc_audience": "lance",
            "fga_enabled": True,
            "fga_api_url": "http://openfga:8080",
            "trash_grace_days": 7,
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
        }
    )
    real_ns_client.app.dependency_overrides[get_settings] = lambda: settings
    real_ns_client.app.state.oidc = MagicMock()
    store = Grants()
    real_ns_client.app.state.fga = store
    seeding.put_warehouse(
        root, StorageOptions(), {"id": WAREHOUSE, "bucket": WAREHOUSE, "root_uri": root, "project": PROJECT, "status": "active", "created_at": "t"}
    )
    warehouses.ensure_catalog_row(real_ns_client.app.dependency_overrides[get_namespace](), WAREHOUSE)
    for user in ("alice", "bob"):
        store.grant(user, ("can_create_namespace", f"warehouse:{WAREHOUSE}"))
    store.grant("alice", ("can_administer", f"project:{PROJECT}"))
    yield store


def _object(client: TestClient, kind: str, name: str) -> str:
    """The FGA object the catalog governs the table or namespace ``name`` as: ``<kind>:<uuid>`` (ADR 0173 §8), read where
    the catalog keeps it, since a grant is written before the caller may describe it."""
    ns = client.app.dependency_overrides[get_namespace]()
    segments = name.split("$")
    if kind == "namespace":
        return fga.namespace_object(object_uuids.require_namespace_uuid(ns, segments))
    table_uuid = object_uuids.table_uuid(ns, table_claims.claim_store(client.app.dependency_overrides[get_settings]()), segments)
    assert table_uuid is not None, f"table {name} carries no uuid"
    return fga.table_object(table_uuid)


def _top_level(client: TestClient, name: str, properties: dict[str, str] | None = None) -> int:
    """Create ``name`` directly in the warehouse, where the warehouse's ``can_create_namespace`` is what is asked."""
    body: dict[str, object] = {"properties": properties} if properties is not None else {}
    return client.post(f"/v1/namespace/{name}/create", json=body, headers=AUTH).status_code


def _properties(client: TestClient, namespace: str) -> dict[str, str]:
    described = client.post(f"/v1/namespace/{namespace}/describe", json={}, headers=AUTH)
    assert described.status_code == 200, described.text
    return described.json().get("properties") or {}


RULE = {"rask.requires_run": "true"}


def _rule(client: TestClient, namespace: str) -> str | None:
    return _properties(client, namespace).get("rask.requires_run")


def _refused(resp: httpx.Response, code: int) -> None:
    assert resp.status_code in (400, 403), resp.text
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["code"] == code, resp.json()


def test_only_the_projects_admin_declares_or_drops_the_run_rule(real_ns_client: TestClient, grants: Grants) -> None:
    """On a create in the warehouse, a nested create and an Overwrite: bob is refused PermissionDenied (15), alice lands it."""
    client = real_ns_client
    act_as(client, "bob")
    _refused(client.post(f"/v1/namespace/{SALES}/create", json={"properties": RULE}, headers=AUTH), 15)
    assert _top_level(client, SALES) == 200, "bob may still create the namespace without declaring the rule"
    grants.grant("bob", ("can_create_namespace", _object(client, "namespace", SALES)))
    _refused(client.post(f"/v1/namespace/{WAREHOUSE}$sales$lane/create", json={"properties": RULE}, headers=AUTH), 15)

    act_as(client, "alice")
    assert _top_level(client, LEDGER, RULE) == 200
    ledger = _object(client, "namespace", LEDGER)
    for user in ("alice", "bob"):
        grants.grant(user, ("can_get_metadata", ledger))
    assert _rule(client, LEDGER) == "true"

    act_as(client, "bob")
    grants.grant("bob", ("can_create_namespace", ledger), ("can_delete", ledger))
    _refused(client.post(f"/v1/namespace/{WAREHOUSE}$ledger/create", json={"mode": "Overwrite", "properties": {}}, headers=AUTH), 15)
    assert _rule(client, LEDGER) == "true", "a refused Overwrite dropped the rule"


def test_a_declaration_names_no_reserved_key_but_the_run_rule_as_true(real_ns_client: TestClient, grants: Grants) -> None:
    """A value other than "true", or another reserved key, the uuid the catalog mints among them ([[LH-519]]), is
    InvalidInput (13) in the warehouse and beneath it; a nested namespace may repeat the rule; an ExistOk naming the rule
    on a kept namespace that does not declare it is NamespaceAlreadyExists (2)."""
    client = real_ns_client
    act_as(client, "alice")
    forged = {"rask.uuid": "00000000-0000-4000-8000-000000000000"}
    _refused(client.post(f"/v1/namespace/{SALES}/create", json={"properties": {"rask.requires_run": "false"}}, headers=AUTH), 13)
    _refused(client.post(f"/v1/namespace/{SALES}/create", json={"properties": {"rask.tier": "silver"}}, headers=AUTH), 13)
    _refused(client.post(f"/v1/namespace/{SALES}/create", json={"properties": forged}, headers=AUTH), 13)
    assert _top_level(client, SALES, RULE) == 200
    grants.grant("alice", ("can_create_namespace", _object(client, "namespace", SALES)))

    _refused(client.post(f"/v1/namespace/{WAREHOUSE}$sales$lane/create", json={"properties": forged}, headers=AUTH), 13)
    assert client.post(f"/v1/namespace/{WAREHOUSE}$sales$lane/create", json={"properties": RULE}, headers=AUTH).status_code == 200
    assert _top_level(client, f"{WAREHOUSE}$plain") == 200
    grants.grant("alice", ("can_create_namespace", _object(client, "namespace", f"{WAREHOUSE}$plain")))
    assert client.post(f"/v1/namespace/{WAREHOUSE}$plain$sub/create", json={}, headers=AUTH).status_code == 200
    kept = client.post(f"/v1/namespace/{WAREHOUSE}$plain$sub/create", json={"mode": "ExistOk", "properties": RULE}, headers=AUTH)
    assert (kept.status_code, kept.json()["code"]) == (409, 2), "ExistOk kept the namespace, so the rule it named never landed"


def _rows() -> bytes:
    table = pa.table({"id": pa.array([1, 2], pa.int64())})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


#: The run a write under the rule names (ADR 0156 C2): an input the writer may read, at a version.
_RUN = {
    "x-rask-run-id": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a07",
    "x-rask-job-namespace": "pipelines",
    "x-rask-job": "nightly",
    "x-rask-inputs": json.dumps([{"table": [WAREHOUSE, "src", "a"], "version": 1}]),
}


def test_a_write_names_its_run_where_a_namespace_above_it_requires_one(real_ns_client: TestClient, grants: Grants, caplog: pytest.LogCaptureFixture) -> None:
    """Without the writer rung the write is PermissionDenied (15). Under ``sales``, which declares the rule, and under
    ``sales$lane``, which inherits it, an insert naming no run is InvalidInput (13) and one naming its run lands; under
    ``gold``, which declares nothing, both land. Each decision is audited with the rule it was judged under."""
    client = real_ns_client
    act_as(client, "alice")
    assert _top_level(client, SALES, RULE) == 200
    assert _top_level(client, f"{WAREHOUSE}$gold") == 200
    assert _top_level(client, f"{WAREHOUSE}$src") == 200
    grants.grant("alice", ("can_create_namespace", _object(client, "namespace", SALES)))
    assert client.post(f"/v1/namespace/{SALES}$lane/create", json={}, headers=AUTH).status_code == 200
    spaces = (SALES, f"{SALES}$lane", f"{WAREHOUSE}$gold")
    grants.grant("alice", *[("can_create_table", _object(client, "namespace", ns)) for ns in (*spaces, f"{WAREHOUSE}$src")])
    # The run's input is a table alice may read: a name that holds no table grants nothing to read.
    assert client.post(f"/v1/table/{WAREHOUSE}$src$a/create?mode=create", content=_rows(), headers=ARROW).status_code == 200
    grants.grant("alice", ("can_read_data", _object(client, "table", f"{WAREHOUSE}$src$a")))
    for ns in spaces:
        assert client.post(f"/v1/table/{ns}$t/create?mode=create", content=_rows(), headers={**ARROW, **_RUN}).status_code == 200
    grants.grant("alice", *[("can_write_data", _object(client, "table", f"{ns}$t")) for ns in spaces])

    act_as(client, "dave")
    _refused(client.post(f"/v1/table/{SALES}$t/insert", content=_rows(), headers={**ARROW, **_RUN}), 15)

    act_as(client, "alice")
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    caplog.clear()
    for ns in (SALES, f"{SALES}$lane"):
        _refused(client.post(f"/v1/table/{ns}$t/insert", content=_rows(), headers=ARROW), 13)
    assert client.post(f"/v1/table/{WAREHOUSE}$gold$t/insert", content=_rows(), headers=ARROW).status_code == 200
    for ns in spaces:
        assert client.post(f"/v1/table/{ns}$t/insert", content=_rows(), headers={**ARROW, **_RUN}).status_code == 200
    judged = [
        (r.__dict__["audit.resource"], r.__dict__["audit.requires_run"], r.__dict__["audit.outcome"])
        for r in caplog.records
        if r.name == AUDIT_LOGGER and r.__dict__.get("audit.action") == "write_judge" and r.__dict__.get("audit.op") == "insert"
    ]
    # The judgement is audited under the written table's name (``Write.obj``), a refusal under the run rule as an allow is.
    assert judged == [
        (f"table:{SALES}$t", "true", "deny"),
        (f"table:{SALES}$lane$t", "true", "deny"),
        (f"table:{WAREHOUSE}$gold$t", "false", "allow"),
        (f"table:{SALES}$t", "true", "allow"),
        (f"table:{SALES}$lane$t", "true", "allow"),
        (f"table:{WAREHOUSE}$gold$t", "false", "allow"),
    ]


def test_a_recoverable_drop_and_its_undrop_keep_the_run_rule_and_the_uuid(real_ns_client: TestClient, grants: Grants) -> None:
    """An owner who may drop and undrop is not an admin, so the round trip must bring the rule back with the namespace,
    and the namespace comes back as itself, under the uuid it was created with ([[LH-519]])."""
    client = real_ns_client
    act_as(client, "alice")
    assert _top_level(client, LEDGER, RULE) == 200
    act_as(client, "bob")
    ledger = _object(client, "namespace", LEDGER)
    grants.grant("bob", ("can_delete", ledger), ("can_get_metadata", ledger))
    minted = _properties(client, LEDGER)["rask.uuid"]

    assert client.post(f"/v1/namespace/{WAREHOUSE}$ledger/drop", json={"behavior": "Cascade"}, headers=AUTH).status_code == 200
    assert client.post(f"/management/v1/namespace/{WAREHOUSE}$ledger/undrop", json={}, headers=AUTH).status_code == 200

    assert _properties(client, LEDGER) == {"rask.requires_run": "true", "rask.uuid": minted}
