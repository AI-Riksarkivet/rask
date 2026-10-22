"""A table's tier is the `rask.tier` its namespace declares, only an admin declares it, and every write is judged under it.

ADR 0156 C8 and ADR 0160, on the real catalog app over real pylance with OIDC, FGA and warehouses on. The
OpenFGA store is an in-memory double behind the real `service_kit.governed.fga` functions that answers a
check from an explicit grant set, so who may act IS the claim here. Namespaces are named against their
tier on purpose (`sales` is silver, `gold` is untiered): a judge reading names would answer both wrong.
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
from service_kit.governed import fga
from service_kit.governed.audit import AUDIT_LOGGER
from tests.integration.openfga_grants import Grants, act_as


AUTH = {"Authorization": "Bearer t"}
ARROW = {**AUTH, "content-type": "application/vnd.apache.arrow.stream"}
WAREHOUSE = "acme-bucket"
PROJECT = "acme"


@pytest.fixture
def grants(real_ns_client: TestClient, tmp_path: Path) -> Iterator[Grants]:
    """The real catalog as the chart deploys it, with warehouse ``WAREHOUSE`` of project ``PROJECT`` at its root.

    alice administers the project; bob may only create namespaces in its warehouse.
    """
    root = str(tmp_path)
    settings = Settings.model_validate(
        {
            "root": root,
            "warehouses_enabled": True,
            "oidc_enabled": True,
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
    warehouses.put_warehouse(root, {}, {"id": WAREHOUSE, "bucket": WAREHOUSE, "root_uri": root, "project": PROJECT, "status": "active", "created_at": "t"})
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
    body: dict[str, object] = {"namespace": name} | ({"properties": properties} if properties is not None else {})
    return client.post(f"/management/v1/warehouses/{WAREHOUSE}/namespaces", json=body, headers=AUTH).status_code


def _properties(client: TestClient, namespace: str) -> dict[str, str]:
    described = client.post(f"/v1/namespace/{namespace}/describe", json={}, headers=AUTH)
    assert described.status_code == 200, described.text
    return described.json().get("properties") or {}


def _tier(client: TestClient, namespace: str) -> str | None:
    return _properties(client, namespace).get("rask.tier")


def _refused(resp: httpx.Response, code: int) -> None:
    assert resp.status_code in (400, 403), resp.text
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["code"] == code, resp.json()


def test_only_the_projects_admin_declares_or_drops_a_tier(real_ns_client: TestClient, grants: Grants) -> None:
    """On the warehouse door, the nested door and an Overwrite: bob is refused PermissionDenied (15), alice lands it."""
    client = real_ns_client
    act_as(client, "bob")
    _refused(
        client.post(f"/management/v1/warehouses/{WAREHOUSE}/namespaces", json={"namespace": "sales", "properties": {"rask.tier": "silver"}}, headers=AUTH), 15
    )
    assert _top_level(client, "sales") == 200, "bob may still create the namespace without declaring a tier"
    grants.grant("bob", ("can_create_namespace", _object(client, "namespace", "sales")))
    _refused(client.post("/v1/namespace/sales$lane/create", json={"properties": {"rask.tier": "silver"}}, headers=AUTH), 15)

    act_as(client, "alice")
    assert _top_level(client, "ledger", {"rask.tier": "gold"}) == 200
    ledger = _object(client, "namespace", "ledger")
    for user in ("alice", "bob"):
        grants.grant(user, ("can_get_metadata", ledger))
    assert _tier(client, "ledger") == "gold"

    act_as(client, "bob")
    grants.grant("bob", ("can_create_namespace", ledger), ("can_delete", ledger))
    _refused(client.post("/v1/namespace/ledger/create", json={"mode": "Overwrite", "properties": {}}, headers=AUTH), 15)
    assert _tier(client, "ledger") == "gold", "a refused Overwrite dropped the tier"


def test_a_declaration_names_no_reserved_key_but_a_tier_its_ancestors_agree_with(real_ns_client: TestClient, grants: Grants) -> None:
    """A value outside bronze/silver/gold, a deeper namespace re-tiering its ancestor's, or another reserved key, the
    uuid the catalog mints among them ([[LH-519]]), is InvalidInput (13) on both doors; an ExistOk naming a tier the
    kept namespace does not have is NamespaceAlreadyExists (2)."""
    client = real_ns_client
    act_as(client, "alice")
    forged = {"rask.uuid": "00000000-0000-4000-8000-000000000000"}
    _refused(
        client.post(f"/management/v1/warehouses/{WAREHOUSE}/namespaces", json={"namespace": "sales", "properties": {"rask.tier": "platinum"}}, headers=AUTH), 13
    )
    _refused(client.post(f"/management/v1/warehouses/{WAREHOUSE}/namespaces", json={"namespace": "sales", "properties": forged}, headers=AUTH), 13)
    assert _top_level(client, "sales", {"rask.tier": "silver"}) == 200
    grants.grant("alice", ("can_create_namespace", _object(client, "namespace", "sales")))

    _refused(client.post("/v1/namespace/sales$lane/create", json={"properties": {"rask.tier": "bronze"}}, headers=AUTH), 13)
    _refused(client.post("/v1/namespace/sales$lane/create", json={"properties": forged}, headers=AUTH), 13)
    assert client.post("/v1/namespace/sales$lane/create", json={"properties": {"rask.tier": "silver"}}, headers=AUTH).status_code == 200
    assert _top_level(client, "plain") == 200
    grants.grant("alice", ("can_create_namespace", _object(client, "namespace", "plain")))
    assert client.post("/v1/namespace/plain$sub/create", json={"properties": {"rask.tier": "bronze"}}, headers=AUTH).status_code == 200
    kept = client.post("/v1/namespace/plain$sub/create", json={"mode": "ExistOk", "properties": {"rask.tier": "gold"}}, headers=AUTH)
    assert (kept.status_code, kept.json()["code"]) == (409, 2), "ExistOk kept the namespace, so the gold it named never landed"


def _rows() -> bytes:
    table = pa.table({"id": pa.array([1, 2], pa.int64())})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


#: The run a silver write names (ADR 0156 C2): an input the writer may read, at a version.
_RUN = {"x-rask-run-id": "run-7", "x-rask-inputs": json.dumps([{"table": ["src", "a"], "version": 1}])}


def test_a_write_is_judged_under_the_tier_its_namespace_declares(real_ns_client: TestClient, grants: Grants, caplog: pytest.LogCaptureFixture) -> None:
    """Without the writer rung the write is PermissionDenied (15); with it the decision is audited under the
    declared tier, and a namespace whose NAME is a tier but which declares none is judged untiered."""
    client = real_ns_client
    act_as(client, "alice")
    assert _top_level(client, "sales", {"rask.tier": "silver"}) == 200
    assert _top_level(client, "gold") == 200
    assert _top_level(client, "src") == 200
    grants.grant("alice", *[("can_create_table", _object(client, "namespace", ns)) for ns in ("sales", "gold", "src")])
    # The run's input is a table alice may read: a name that holds no table grants nothing to read.
    assert client.post("/v1/table/src$a/create?mode=create", content=_rows(), headers=ARROW).status_code == 200
    grants.grant("alice", ("can_read_data", _object(client, "table", "src$a")))
    for ns in ("sales", "gold"):
        assert client.post(f"/v1/table/{ns}$t/create?mode=create", content=_rows(), headers={**ARROW, **_RUN}).status_code == 200
    grants.grant("alice", *[("can_write_data", _object(client, "table", f"{ns}$t")) for ns in ("sales", "gold")])

    act_as(client, "dave")
    _refused(client.post("/v1/table/sales$t/insert", content=_rows(), headers={**ARROW, **_RUN}), 15)

    act_as(client, "alice")
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    for ns in ("sales", "gold"):
        assert client.post(f"/v1/table/{ns}$t/insert", content=_rows(), headers={**ARROW, **_RUN}).status_code == 200
    judged = [
        (r.__dict__["audit.resource"], r.__dict__["audit.tier"])
        for r in caplog.records
        if r.name == AUDIT_LOGGER and r.__dict__.get("audit.action") == "tier_judge" and r.__dict__.get("audit.op") == "insert"
    ]
    # The judgement is audited under the written table's name (``WriteOp.obj``), as a refusal names it.
    assert judged == [("table:sales$t", "silver"), ("table:gold$t", "untiered")]


def test_a_recoverable_drop_and_its_undrop_keep_the_declared_tier_and_the_uuid(real_ns_client: TestClient, grants: Grants) -> None:
    """An owner who may drop and undrop is not an admin, so the round trip must bring the tier back with the namespace,
    and the namespace comes back as itself, under the uuid it was created with ([[LH-519]])."""
    client = real_ns_client
    act_as(client, "alice")
    assert _top_level(client, "ledger", {"rask.tier": "gold"}) == 200
    act_as(client, "bob")
    ledger = _object(client, "namespace", "ledger")
    grants.grant("bob", ("can_delete", ledger), ("can_get_metadata", ledger))
    minted = _properties(client, "ledger")["rask.uuid"]

    assert client.post("/v1/namespace/ledger/drop", json={"behavior": "Cascade"}, headers=AUTH).status_code == 200
    assert client.post("/management/v1/namespace/ledger/undrop", json={}, headers=AUTH).status_code == 200

    assert _properties(client, "ledger") == {"rask.tier": "gold", "rask.uuid": minted}
