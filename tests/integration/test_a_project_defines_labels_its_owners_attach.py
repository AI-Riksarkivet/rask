"""A project defines its labels; an object's owner attaches them; a member finds the objects by them (ADR 0211).

On the real catalog app over real pylance with OIDC and FGA on, in a registered warehouse of project ``acme``. The
OpenFGA store is an in-memory double behind the real `lakehouse.governed.fga` functions that answers a check from an
explicit grant set, so who may act IS the claim here: alice administers the project, bob owns one table and reads the
project, carol is a member who sees only what she is granted.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from catalog.api.dependencies import get_control_emitter, get_namespace
from catalog.core.config import Settings, get_settings
from catalog.services import object_uuids, projects, table_claims, warehouses
from lakehouse.control_events import CatalogControlEvent
from lakehouse.governed import fga
from lakehouse.storage_options import StorageOptions
from tests.integration.openfga_grants import Grants, act_as


AUTH = {"Authorization": "Bearer t"}
ARROW = {**AUTH, "content-type": "application/vnd.apache.arrow.stream"}
WAREHOUSE = "acme-bucket"
PROJECT = "acme"
#: The project and its warehouse as OpenFGA names them, by the creations their records name (ADR 0245).
PROJECT_OBJ, WAREHOUSE_OBJ = f"project:{'a' * 32}", f"warehouse:{'b' * 32}"
SALES = f"{WAREHOUSE}$sales"
ORDERS = f"{SALES}$orders"
REFUNDS = f"{SALES}$refunds"


@pytest.fixture
def estate(real_ns_client: TestClient, tmp_path: Path) -> Iterator[Grants]:
    """Project ``acme`` with warehouse ``WAREHOUSE``, namespace ``sales`` holding ``orders`` and ``refunds``.

    alice administers the project and owns everything in it; bob owns ``orders`` alone; carol is a member reading
    ``refunds`` alone.
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
    client = real_ns_client
    client.app.dependency_overrides[get_settings] = lambda: settings
    client.app.state.oidc = MagicMock()
    store = Grants()
    client.app.state.fga = store
    projects.put_project(root, StorageOptions(), {"id": PROJECT, "uuid": "a" * 32, "created_at": "t"})
    warehouses.put_warehouse(
        root,
        StorageOptions(),
        {"id": WAREHOUSE, "uuid": "b" * 32, "bucket": WAREHOUSE, "root_uri": root, "project": PROJECT, "status": "active", "created_at": "t"},
    )
    warehouses.ensure_catalog_row(client.app.dependency_overrides[get_namespace](), WAREHOUSE)
    store.grant("alice", ("can_create_namespace", WAREHOUSE_OBJ), ("can_administer", PROJECT_OBJ), ("member", PROJECT_OBJ))
    act_as(client, "alice")
    assert client.post(f"/v1/namespace/{SALES}/create", json={}, headers=AUTH).status_code == 200
    sales = _object(client, "namespace", SALES)
    store.grant("alice", ("can_create_table", sales), ("can_label", sales), ("can_get_metadata", sales))
    for table in (ORDERS, REFUNDS):
        assert client.post(f"/v1/table/{table}/create?mode=create", content=_rows(), headers=ARROW).status_code == 200
        store.grant("alice", ("can_label", _object(client, "table", table)), ("can_get_metadata", _object(client, "table", table)))
    orders, refunds = _object(client, "table", ORDERS), _object(client, "table", REFUNDS)
    store.grant("bob", ("can_label", orders), ("can_get_metadata", orders), ("member", PROJECT_OBJ))
    store.grant("carol", ("can_get_metadata", refunds), ("member", PROJECT_OBJ))
    yield store


class _Announced:
    """The control emitter, as a list of the events the doors published."""

    def __init__(self) -> None:
        self.events: list[CatalogControlEvent] = []

    async def emit(self, event: CatalogControlEvent) -> None:
        self.events.append(event)


def _rows() -> bytes:
    table = pa.table({"id": pa.array([1, 2], pa.int64())})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def _object(client: TestClient, kind: str, name: str) -> str:
    """The FGA object the catalog governs ``name`` as, read where the catalog keeps its uuid (ADR 0173 §8)."""
    ns = client.app.dependency_overrides[get_namespace]()
    segments = name.split("$")
    if kind == "namespace":
        return fga.namespace_object(object_uuids.require_namespace_uuid(ns, segments))
    table_uuid = object_uuids.table_uuid(ns, table_claims.claim_store(client.app.dependency_overrides[get_settings]()), segments)
    assert table_uuid is not None, f"table {name} carries no uuid"
    return fga.table_object(table_uuid)


def _define(client: TestClient, body: Mapping[str, object]) -> httpx.Response:
    return client.post(f"/management/v1/projects/{PROJECT}/labels/define", json=body, headers=AUTH)


def _set(client: TestClient, kind: str, name: str, label: str, value: str | None = None) -> httpx.Response:
    return client.post(f"/management/v1/{kind}/{name}/labels/set", json={"name": label, "value": value}, headers=AUTH)


def _code(resp: httpx.Response) -> tuple[int, int]:
    assert resp.headers["content-type"] == "application/problem+json", resp.text
    return resp.status_code, resp.json()["code"]


DOMAIN = {"name": "domain", "scope": ["table"], "kind": "enumerated", "allowed_values": ["finance", "ops"]}
PII = {"name": "pii", "scope": ["namespace", "table"], "kind": "marker"}


def test_only_the_projects_admin_defines_a_label_and_a_definition_only_grows(real_ns_client: TestClient, estate: Grants) -> None:
    """bob, a member, is refused PermissionDenied (15); alice defines ``domain``, and applying it again as it stands
    announces nothing, as a producer declaring its labels on every reconcile does. Evolving it may add a value and a kind
    of object, never drop one or change its kind (13); every member reads the definitions, and an outsider is answered
    the missing project's 404."""
    client = real_ns_client
    act_as(client, "bob")
    assert _code(_define(client, DOMAIN)) == (403, 15)

    act_as(client, "alice")
    announced = _Announced()
    client.app.dependency_overrides[get_control_emitter] = lambda: announced
    defined = _define(client, DOMAIN)
    assert defined.status_code == 200, defined.text
    assert _define(client, DOMAIN).json() == defined.json()
    assert [event.action for event in announced.events] == ["label_defined"]
    grown = _define(client, {**DOMAIN, "scope": ["namespace", "table"], "allowed_values": ["finance", "ops", "hr"]})
    assert (grown.json()["uuid"], grown.json()["scope"], grown.json()["allowed_values"]) == (
        defined.json()["uuid"],
        ["namespace", "table"],
        ["finance", "ops", "hr"],
    )
    assert _code(_define(client, {**DOMAIN, "allowed_values": ["finance"]})) == (400, 13)
    assert _code(_define(client, {**DOMAIN, "scope": ["table"], "allowed_values": ["finance", "ops", "hr"]})) == (400, 13)
    assert _code(_define(client, {**DOMAIN, "kind": "free_text", "allowed_values": None})) == (400, 13)
    assert _code(_define(client, {**PII, "name": "rask.tier"})) == (400, 13)

    act_as(client, "carol")
    assert [d["name"] for d in client.get(f"/management/v1/projects/{PROJECT}/labels", headers=AUTH).json()] == ["domain"]
    act_as(client, "dave")
    assert _code(client.get(f"/management/v1/projects/{PROJECT}/labels", headers=AUTH)) == (404, 4)


def test_an_owner_attaches_a_label_the_project_defines_with_a_value_it_allows(real_ns_client: TestClient, estate: Grants) -> None:
    """bob owns ``orders``: he attaches ``domain=finance``, is refused a value the label does not allow, a label the
    project does not define and a marker with a value (13), and ``refunds``, which he does not own (15). A rename keeps
    the labels, since they are kept under the table's uuid."""
    client = real_ns_client
    act_as(client, "alice")
    assert _define(client, DOMAIN).status_code == 200
    assert _define(client, PII).status_code == 200

    act_as(client, "bob")
    attached = _set(client, "table", ORDERS, "domain", "finance")
    assert (attached.status_code, attached.json()["labels"]) == (200, {"domain": "finance"}), attached.text
    assert _code(_set(client, "table", ORDERS, "domain", "legal")) == (400, 13)
    assert _code(_set(client, "table", ORDERS, "tier", "silver")) == (400, 13)
    assert _code(_set(client, "table", ORDERS, "pii", "yes")) == (400, 13)
    assert _code(_set(client, "table", REFUNDS, "pii")) == (403, 15)
    assert _set(client, "table", ORDERS, "pii").json()["labels"] == {"domain": "finance", "pii": None}

    act_as(client, "alice")
    estate.grant("alice", ("can_drop", _object(client, "table", ORDERS)))
    renamed = client.post(f"/v1/table/{ORDERS}/rename", json={"new_table_name": "ledger", "new_namespace_id": [WAREHOUSE, "sales"]}, headers=AUTH)
    assert renamed.status_code == 200, renamed.text
    described = client.post(f"/management/v1/table/{SALES}$ledger/labels/describe", json={}, headers=AUTH)
    assert described.json()["labels"] == {"domain": "finance", "pii": None}
    # An owner who is no member of the project still reads what it may attach, on the object it reads.
    estate.grant("erin", ("can_label", _object(client, "table", f"{SALES}$ledger")), ("can_get_metadata", _object(client, "table", f"{SALES}$ledger")))
    act_as(client, "erin")
    offered = client.post(f"/management/v1/table/{SALES}$ledger/labels/describe", json={}, headers=AUTH).json()["available"]
    assert sorted(d["name"] for d in offered) == ["domain", "pii"]


def test_a_member_finds_the_objects_carrying_a_label_among_those_it_may_see(real_ns_client: TestClient, estate: Grants) -> None:
    """alice labels both tables and the namespace ``pii``. carol, who sees only ``refunds``, is listed only it; alice is
    listed all three, and filtering by ``domain`` narrows to the table carrying it. A table dropped to the trash leaves
    the list."""
    client = real_ns_client
    act_as(client, "alice")
    assert _define(client, DOMAIN).status_code == 200
    assert _define(client, PII).status_code == 200
    for kind, name in (("table", ORDERS), ("table", REFUNDS), ("namespace", SALES)):
        assert _set(client, kind, name, "pii").status_code == 200
    assert _set(client, "table", ORDERS, "domain", "ops").status_code == 200

    def listed(user: str, query: str = "") -> list[tuple[str, str]]:
        act_as(client, user)
        resp = client.get(f"/management/v1/projects/{PROJECT}/labels/objects{query}", headers=AUTH)
        assert resp.status_code == 200, resp.text
        return sorted((o["kind"], o["id"]) for o in resp.json()["objects"])

    assert listed("carol") == [("table", REFUNDS)]
    assert listed("alice") == [("namespace", SALES), ("table", ORDERS), ("table", REFUNDS)]
    assert listed("alice", "?label=domain") == [("table", ORDERS)]

    # A table in the trash is addressed by nobody, so it is listed to nobody.
    act_as(client, "alice")
    estate.grant("alice", ("can_drop", _object(client, "table", REFUNDS)))
    assert client.post(f"/v1/table/{REFUNDS}/drop", json={}, headers=AUTH).status_code == 200
    assert listed("carol") == []
    assert listed("alice") == [("namespace", SALES), ("table", ORDERS)]


def test_a_label_is_deleted_once_no_standing_object_carries_it_and_never_comes_back(real_ns_client: TestClient, estate: Grants) -> None:
    """Deleting ``pii`` while a table carries it is refused NamespaceNotEmpty (409, 3) naming the table as it is named
    now, and a member is refused the delete (15). A table in the trash does not hold the definition: once the standing
    table's label is removed the delete lands, and the trashed table, restored after ``pii`` is defined again, shows no
    label, since the one it kept was attached under the definition that is gone."""
    client = real_ns_client
    act_as(client, "alice")
    assert _define(client, PII).status_code == 200
    assert _set(client, "table", REFUNDS, "pii").status_code == 200
    act_as(client, "bob")
    assert _set(client, "table", ORDERS, "pii").status_code == 200
    assert _code(client.post(f"/management/v1/projects/{PROJECT}/labels/pii/delete", headers=AUTH)) == (403, 15)

    act_as(client, "alice")
    estate.grant("alice", ("can_drop", _object(client, "table", ORDERS)), ("can_drop", _object(client, "table", REFUNDS)))
    renamed = client.post(f"/v1/table/{ORDERS}/rename", json={"new_table_name": "ledger", "new_namespace_id": [WAREHOUSE, "sales"]}, headers=AUTH)
    assert renamed.status_code == 200, renamed.text
    assert client.post(f"/v1/table/{REFUNDS}/drop", json={}, headers=AUTH).status_code == 200
    refused = client.post(f"/management/v1/projects/{PROJECT}/labels/pii/delete", headers=AUTH)
    assert _code(refused) == (409, 3)
    assert refused.json()["detail"] == f"label 'pii' is attached to 1 object(s) ({SALES}$ledger); remove it from each first"

    act_as(client, "bob")
    removed = client.post(f"/management/v1/table/{SALES}$ledger/labels/remove", json={"name": "pii"}, headers=AUTH)
    assert removed.json()["labels"] == {}
    act_as(client, "alice")
    assert client.post(f"/management/v1/projects/{PROJECT}/labels/pii/delete", headers=AUTH).status_code == 200
    assert client.get(f"/management/v1/projects/{PROJECT}/labels", headers=AUTH).json() == []

    assert _define(client, PII).status_code == 200
    assert client.post(f"/management/v1/table/{REFUNDS}/undrop", json={}, headers=AUTH).status_code == 200
    assert client.post(f"/management/v1/table/{REFUNDS}/labels/describe", json={}, headers=AUTH).json()["labels"] == {}
