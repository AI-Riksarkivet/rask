"""A namespace's `rask.requires_run` binds every write beneath it to name its run, only an admin declares it, and every
write is judged under it.

ADR 0156 C8 and ADR 0211, against the catalog as deployed over real pylance, in a registered warehouse: a loopback Dex
signs callers in and a real OpenFGA decides on a store of the test's own, so who may act IS the claim here and each
caller holds only the rungs named. A namespace named `gold` that declares nothing is judged by what it declares, never by
its name.
"""

import json
import logging
from pathlib import Path
from typing import Final

import httpx
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from catalog.api.dependencies import get_namespace
from catalog.core.config import get_settings
from catalog.services import object_uuids, table_claims, warehouses
from lakehouse.governed import fga_ids
from lakehouse.governed.audit import AUDIT_LOGGER
from tests import seeding
from tests.governed_catalog import GovernedCatalog


ARROW = {"content-type": "application/vnd.apache.arrow.stream"}
WAREHOUSE = "acme-bucket"
PROJECT = "acme"
SALES = f"{WAREHOUSE}$sales"
LEDGER = f"{WAREHOUSE}$ledger"


@pytest.fixture
def grants(governed_catalog: GovernedCatalog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GovernedCatalog:
    """The catalog as deployed, drops kept a week, with warehouse ``WAREHOUSE`` of project ``PROJECT`` at its root, its own
    row in that root's catalog and its project edge as the warehouse create door leaves them.

    alice administers the project; bob may only create namespaces in its warehouse.
    """
    monkeypatch.setenv("LANCE_TRASH_GRACE_DAYS", "7")
    get_settings.cache_clear()
    root, catalog = str(tmp_path), governed_catalog
    seeding.put_warehouse(
        root,
        get_settings().storage_options(),
        {"id": WAREHOUSE, "bucket": WAREHOUSE, "root_uri": root, "project": PROJECT, "status": "active", "created_at": "t"},
    )
    warehouses.ensure_catalog_row(catalog.client.app.dependency_overrides[get_namespace](), WAREHOUSE)
    catalog.store.write(
        (f"project:{PROJECT}", "project", f"warehouse:{WAREHOUSE}"),
        (catalog.person("alice"), "admin", f"project:{PROJECT}"),
        *((catalog.person(user), "writer", f"warehouse:{WAREHOUSE}") for user in ("alice", "bob")),
    )
    return catalog


def _object(client: TestClient, kind: str, name: str) -> str:
    """The FGA object the catalog governs the table or namespace ``name`` as: ``<kind>:<uuid>`` (ADR 0173 §8), read where
    the catalog keeps it, since a grant is written before the caller may describe it."""
    ns = client.app.dependency_overrides[get_namespace]()
    segments = name.split("$")
    if kind == "namespace":
        return fga_ids.namespace_object(object_uuids.require_namespace_uuid(ns, segments))
    table_uuid = object_uuids.table_uuid(ns, table_claims.claim_store(get_settings()), segments)
    assert table_uuid is not None, f"table {name} carries no uuid"
    return fga_ids.table_object(table_uuid)


def _top_level(client: TestClient, name: str, properties: dict[str, str] | None = None) -> int:
    """Create ``name`` directly in the warehouse, where the warehouse's ``can_create_namespace`` is what is asked."""
    body: dict[str, object] = {"properties": properties} if properties is not None else {}
    return client.post(f"/v1/namespace/{name}/create", json=body).status_code


def _properties(client: TestClient, namespace: str) -> dict[str, str]:
    described = client.post(f"/v1/namespace/{namespace}/describe", json={})
    assert described.status_code == 200, described.text
    return described.json().get("properties") or {}


RULE = {"rask.requires_run": "true"}


def _rule(client: TestClient, namespace: str) -> str | None:
    return _properties(client, namespace).get("rask.requires_run")


def _refused(resp: httpx.Response, code: int) -> None:
    assert resp.status_code in (400, 403), resp.text
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["code"] == code, resp.json()


def test_only_the_projects_admin_declares_or_drops_the_run_rule(grants: GovernedCatalog) -> None:
    """On a create in the warehouse, a nested create and an Overwrite: bob is refused PermissionDenied (15), alice lands it."""
    client = grants.client
    grants.act_as("bob")
    _refused(client.post(f"/v1/namespace/{SALES}/create", json={"properties": RULE}), 15)
    assert _top_level(client, SALES) == 200, "bob may still create the namespace without declaring the rule"
    _refused(client.post(f"/v1/namespace/{WAREHOUSE}$sales$lane/create", json={"properties": RULE}), 15)

    grants.act_as("alice")
    assert _top_level(client, LEDGER, RULE) == 200
    ledger = _object(client, "namespace", LEDGER)
    assert _rule(client, LEDGER) == "true"

    grants.act_as("bob")
    grants.store.write((grants.person("bob"), "owner", ledger))
    _refused(client.post(f"/v1/namespace/{WAREHOUSE}$ledger/create", json={"mode": "Overwrite", "properties": {}}), 15)
    assert _rule(client, LEDGER) == "true", "a refused Overwrite dropped the rule"


def test_a_declaration_names_no_reserved_key_but_the_run_rule_as_true(grants: GovernedCatalog) -> None:
    """A value other than "true", or another reserved key, the uuid the catalog mints among them ([[LH-519]]), is
    InvalidInput (13) in the warehouse and beneath it; a nested namespace may repeat the rule; an ExistOk naming the rule
    on a kept namespace that does not declare it is NamespaceAlreadyExists (2)."""
    client = grants.client
    grants.act_as("alice")
    forged = {"rask.uuid": "00000000-0000-4000-8000-000000000000"}
    _refused(client.post(f"/v1/namespace/{SALES}/create", json={"properties": {"rask.requires_run": "false"}}), 13)
    _refused(client.post(f"/v1/namespace/{SALES}/create", json={"properties": {"rask.tier": "silver"}}), 13)
    _refused(client.post(f"/v1/namespace/{SALES}/create", json={"properties": forged}), 13)
    assert _top_level(client, SALES, RULE) == 200

    _refused(client.post(f"/v1/namespace/{WAREHOUSE}$sales$lane/create", json={"properties": forged}), 13)
    assert client.post(f"/v1/namespace/{WAREHOUSE}$sales$lane/create", json={"properties": RULE}).status_code == 200
    assert _top_level(client, f"{WAREHOUSE}$plain") == 200
    assert client.post(f"/v1/namespace/{WAREHOUSE}$plain$sub/create", json={}).status_code == 200
    kept = client.post(f"/v1/namespace/{WAREHOUSE}$plain$sub/create", json={"mode": "ExistOk", "properties": RULE})
    assert (kept.status_code, kept.json()["code"]) == (409, 2), "ExistOk kept the namespace, so the rule it named never landed"


def _rows() -> bytes:
    table = pa.table({"id": pa.array([1, 2], pa.int64())})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


#: The run a write under the rule names (ADR 0156 C2): an input the writer may read, at a version.
_RUN: Final = frozendict(
    {
        "x-rask-run-id": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a07",
        "x-rask-job-namespace": "pipelines",
        "x-rask-job": "nightly",
        "x-rask-inputs": json.dumps([{"table": [WAREHOUSE, "src", "a"], "version": 1}]),
    }
)


def test_a_write_names_its_run_where_a_namespace_above_it_requires_one(grants: GovernedCatalog, caplog: pytest.LogCaptureFixture) -> None:
    """Without the writer rung the write is PermissionDenied (15). Under ``sales``, which declares the rule, and under
    ``sales$lane``, which inherits it, an insert naming no run is InvalidInput (13) and one naming its run lands; under
    ``gold``, which declares nothing, both land. Each decision is audited with the rule it was judged under."""
    client = grants.client
    grants.act_as("alice")
    assert _top_level(client, SALES, RULE) == 200
    assert _top_level(client, f"{WAREHOUSE}$gold") == 200
    assert _top_level(client, f"{WAREHOUSE}$src") == 200
    assert client.post(f"/v1/namespace/{SALES}$lane/create", json={}).status_code == 200
    spaces = (SALES, f"{SALES}$lane", f"{WAREHOUSE}$gold")
    # The run's input is a table alice may read: a name that holds no table grants nothing to read.
    assert client.post(f"/v1/table/{WAREHOUSE}$src$a/create?mode=create", content=_rows(), headers=ARROW).status_code == 200
    for ns in spaces:
        assert client.post(f"/v1/table/{ns}$t/create?mode=create", content=_rows(), headers={**ARROW, **_RUN}).status_code == 200

    grants.act_as("dave")
    _refused(client.post(f"/v1/table/{SALES}$t/insert", content=_rows(), headers={**ARROW, **_RUN}), 15)

    grants.act_as("alice")
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


def test_a_recoverable_drop_and_its_undrop_keep_the_run_rule_and_the_uuid(grants: GovernedCatalog) -> None:
    """An owner who may drop and undrop is not an admin, so the round trip must bring the rule back with the namespace,
    and the namespace comes back as itself, under the uuid it was created with ([[LH-519]])."""
    client = grants.client
    grants.act_as("alice")
    assert _top_level(client, LEDGER, RULE) == 200
    grants.act_as("bob")
    ledger = _object(client, "namespace", LEDGER)
    grants.store.write((grants.person("bob"), "owner", ledger))
    minted = _properties(client, LEDGER)["rask.uuid"]

    assert client.post(f"/v1/namespace/{WAREHOUSE}$ledger/drop", json={"behavior": "Cascade"}).status_code == 200
    assert client.post(f"/management/v1/namespace/{WAREHOUSE}$ledger/undrop", json={}).status_code == 200

    assert _properties(client, LEDGER) == {"rask.requires_run": "true", "rask.uuid": minted}
