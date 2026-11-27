"""A project defines its labels; an object's owner attaches them; a member finds the objects by them (ADR 0211).

Against the catalog as deployed over real pylance, a loopback Dex signing callers in and a real OpenFGA deciding on a store
of the test's own, in a registered warehouse of project ``acme``. Who may act IS the claim here, so each caller holds
only the rungs named: alice administers the project and owns everything in it, bob owns one table and is a member,
carol is a member who reads one table.
"""

from collections.abc import Mapping
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient

from catalog.api.dependencies import get_control_emitter, get_namespace
from catalog.core.config import get_settings
from catalog.services import object_uuids, table_claims, warehouses
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE
from lakehouse.governed import fga_ids
from tests import seeding
from tests.app_state import app_of
from tests.control_emitter_double import ControlEmitterDouble
from tests.governed_catalog import ROWS, GovernedCatalog


ARROW = {"content-type": ARROW_STREAM_MEDIA_TYPE}
WAREHOUSE = "acme-bucket"
PROJECT = "acme"
SALES = f"{WAREHOUSE}$sales"
ORDERS = f"{SALES}$orders"
REFUNDS = f"{SALES}$refunds"


@pytest.fixture
def estate(governed_catalog: GovernedCatalog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GovernedCatalog:
    """Project ``acme`` with warehouse ``WAREHOUSE``, namespace ``sales`` holding ``orders`` and ``refunds``, drops kept a week.

    alice administers the project and owns everything in it, as the doors that made it seed her; bob owns ``orders``
    alone and is a member; carol is a member reading ``refunds`` alone. The project's and warehouse's records and the
    warehouse's project edge are written where their doors write them, since the warehouse door provisions a bucket a
    ``dir`` estate has none of.
    """
    monkeypatch.setenv("LANCE_TRASH_GRACE_DAYS", "7")
    get_settings.cache_clear()
    root, catalog, client = str(tmp_path), governed_catalog, governed_catalog.client
    seeding.put_project(root, get_settings().storage_options(), {"id": PROJECT, "created_at": "t"})
    seeding.put_warehouse(
        root,
        get_settings().storage_options(),
        {"id": WAREHOUSE, "bucket": WAREHOUSE, "root_uri": root, "project": PROJECT, "status": "active", "created_at": "t"},
    )
    warehouses.ensure_catalog_row(app_of(client).dependency_overrides[get_namespace](), WAREHOUSE)
    project = f"project:{PROJECT}"
    catalog.store.write(
        (project, "project", f"warehouse:{WAREHOUSE}"),
        (catalog.person("alice"), "admin", project),
        (catalog.person("alice"), "writer", f"warehouse:{WAREHOUSE}"),
    )
    catalog.act_as("alice")
    assert client.post(f"/v1/namespace/{SALES}/create", json={}).status_code == 200
    for table in (ORDERS, REFUNDS):
        assert client.post(f"/v1/table/{table}/create?mode=create", content=ROWS, headers=ARROW).status_code == 200
    orders, refunds = _object(client, "table", ORDERS), _object(client, "table", REFUNDS)
    catalog.store.write(
        (catalog.person("bob"), "owner", orders),
        (catalog.person("bob"), "member", project),
        (catalog.person("carol"), "reader", refunds),
        (catalog.person("carol"), "member", project),
    )
    return catalog


def _object(client: TestClient, kind: str, name: str) -> str:
    """The FGA object the catalog governs ``name`` as, read where the catalog keeps its uuid (ADR 0173 §8)."""
    ns = app_of(client).dependency_overrides[get_namespace]()
    segments = name.split("$")
    if kind == "namespace":
        return fga_ids.namespace_object(object_uuids.require_namespace_uuid(ns, segments))
    table_uuid = object_uuids.table_uuid(ns, table_claims.claim_store(get_settings()), segments)
    assert table_uuid is not None, f"table {name} carries no uuid"
    return fga_ids.table_object(table_uuid)


def _define(client: TestClient, body: Mapping[str, object]) -> httpx2.Response:
    return client.post(f"/management/v1/projects/{PROJECT}/labels/define", json=body)


def _set(client: TestClient, kind: str, name: str, label: str, value: str | None = None) -> httpx2.Response:
    return client.post(f"/management/v1/{kind}/{name}/labels/set", json={"name": label, "value": value})


def _code(resp: httpx2.Response) -> tuple[int, int]:
    assert resp.headers["content-type"] == "application/problem+json", resp.text
    return resp.status_code, resp.json()["code"]


DOMAIN = {"name": "domain", "scope": ["table"], "kind": "enumerated", "allowed_values": ["finance", "ops"]}
PII = {"name": "pii", "scope": ["namespace", "table"], "kind": "marker"}


def test_only_the_projects_admin_defines_a_label_and_a_definition_only_grows(estate: GovernedCatalog) -> None:
    """bob, a member, is refused PermissionDenied (15); alice defines ``domain``, and applying it again as it stands
    announces nothing, as a producer declaring its labels on every reconcile does. Evolving it may add a value and a kind
    of object, never drop one or change its kind (13); every member reads the definitions, and an outsider is answered
    the missing project's 404."""
    client = estate.client
    estate.act_as("bob")
    assert _code(_define(client, DOMAIN)) == (403, 15)

    estate.act_as("alice")
    announced = ControlEmitterDouble()
    app_of(client).dependency_overrides[get_control_emitter] = lambda: announced
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

    estate.act_as("carol")
    assert [d["name"] for d in client.get(f"/management/v1/projects/{PROJECT}/labels").json()] == ["domain"]
    estate.act_as("dave")
    assert _code(client.get(f"/management/v1/projects/{PROJECT}/labels")) == (404, 4)


def test_an_owner_attaches_a_label_the_project_defines_with_a_value_it_allows(estate: GovernedCatalog) -> None:
    """bob owns ``orders``: he attaches ``domain=finance``, is refused a value the label does not allow, a label the
    project does not define and a marker with a value (13), and ``refunds``, which he does not own (15). A rename keeps
    the labels, since they are kept under the table's uuid."""
    client = estate.client
    estate.act_as("alice")
    assert _define(client, DOMAIN).status_code == 200
    assert _define(client, PII).status_code == 200

    estate.act_as("bob")
    attached = _set(client, "table", ORDERS, "domain", "finance")
    assert (attached.status_code, attached.json()["labels"]) == (200, {"domain": "finance"}), attached.text
    assert _code(_set(client, "table", ORDERS, "domain", "legal")) == (400, 13)
    assert _code(_set(client, "table", ORDERS, "tier", "cleaned")) == (400, 13)
    assert _code(_set(client, "table", ORDERS, "pii", "yes")) == (400, 13)
    assert _code(_set(client, "table", REFUNDS, "pii")) == (403, 15)
    assert _set(client, "table", ORDERS, "pii").json()["labels"] == {"domain": "finance", "pii": None}

    estate.act_as("alice")
    renamed = client.post(f"/v1/table/{ORDERS}/rename", json={"new_table_name": "ledger", "new_namespace_id": [WAREHOUSE, "sales"]})
    assert renamed.status_code == 200, renamed.text
    described = client.post(f"/management/v1/table/{SALES}$ledger/labels/describe", json={})
    assert described.json()["labels"] == {"domain": "finance", "pii": None}
    # An owner who is no member of the project still reads what it may attach, on the object it reads.
    estate.store.write((estate.person("erin"), "owner", _object(client, "table", f"{SALES}$ledger")))
    estate.act_as("erin")
    offered = client.post(f"/management/v1/table/{SALES}$ledger/labels/describe", json={}).json()["available"]
    assert sorted(d["name"] for d in offered) == ["domain", "pii"]


def test_a_member_finds_the_objects_carrying_a_label_among_those_it_may_see(estate: GovernedCatalog) -> None:
    """alice labels both tables and the namespace ``pii``. carol, who reads only ``refunds``, is listed it and the namespace
    her grant on it lets her see (`model.fga`: `can_get_metadata from child`), never ``orders``; alice is listed all three, and filtering by ``domain`` narrows to the table carrying it. A table dropped to the trash leaves
    the list."""
    client = estate.client
    estate.act_as("alice")
    assert _define(client, DOMAIN).status_code == 200
    assert _define(client, PII).status_code == 200
    for kind, name in (("table", ORDERS), ("table", REFUNDS), ("namespace", SALES)):
        assert _set(client, kind, name, "pii").status_code == 200
    assert _set(client, "table", ORDERS, "domain", "ops").status_code == 200

    def listed(user: str, query: str = "") -> list[tuple[str, str]]:
        estate.act_as(user)
        resp = client.get(f"/management/v1/projects/{PROJECT}/labels/objects{query}")
        assert resp.status_code == 200, resp.text
        return sorted((o["kind"], o["id"]) for o in resp.json()["objects"])

    assert listed("carol") == [("namespace", SALES), ("table", REFUNDS)]
    assert listed("alice") == [("namespace", SALES), ("table", ORDERS), ("table", REFUNDS)]
    assert listed("alice", "?label=domain") == [("table", ORDERS)]

    # A table in the trash is addressed by nobody, so it is listed to nobody.
    estate.act_as("alice")
    assert client.post(f"/v1/table/{REFUNDS}/drop", json={}).status_code == 200
    # The table's grants are kept in the trash for its undrop, so carol still sees the namespace her grant on it lights.
    assert listed("carol") == [("namespace", SALES)]
    assert listed("alice") == [("namespace", SALES), ("table", ORDERS)]


def test_a_label_is_deleted_once_no_standing_object_carries_it_and_never_comes_back(estate: GovernedCatalog, tmp_path: Path) -> None:
    """Deleting ``pii`` while a table carries it is refused NamespaceNotEmpty (409, 3) naming the table as it is named
    now, and a member is refused the delete (15). A table in the trash does not hold the definition: once the standing
    table's label is removed the delete lands, and the trashed table, restored after ``pii`` is defined again, shows no
    label, since the one it kept was attached under the definition that is gone."""
    client = estate.client
    estate.act_as("alice")
    assert _define(client, PII).status_code == 200
    assert _set(client, "table", REFUNDS, "pii").status_code == 200
    estate.act_as("bob")
    assert _set(client, "table", ORDERS, "pii").status_code == 200
    assert _code(client.post(f"/management/v1/projects/{PROJECT}/labels/pii/delete")) == (403, 15)

    estate.act_as("alice")
    renamed = client.post(f"/v1/table/{ORDERS}/rename", json={"new_table_name": "ledger", "new_namespace_id": [WAREHOUSE, "sales"]})
    assert renamed.status_code == 200, renamed.text
    assert client.post(f"/v1/table/{REFUNDS}/drop", json={}).status_code == 200
    refused = client.post(f"/management/v1/projects/{PROJECT}/labels/pii/delete")
    assert _code(refused) == (409, 3)
    assert refused.json()["detail"] == f"label 'pii' is attached to 1 object(s) ({SALES}$ledger); remove it from each first"

    estate.act_as("bob")
    removed = client.post(f"/management/v1/table/{SALES}$ledger/labels/remove", json={"name": "pii"})
    assert removed.json()["labels"] == {}
    # A labels record that cannot be read may carry the label, so it is no evidence that nothing does: the delete is
    # refused as the record's own state (500, 18), and the definition stands until the record is read or written again.
    estate.act_as("alice")
    unreadable = tmp_path / "_labels" / "table-unreadable.json"
    unreadable.write_bytes(b"\xff not json")
    assert _code(client.post(f"/management/v1/projects/{PROJECT}/labels/pii/delete")) == (500, 18)
    assert [d["name"] for d in client.get(f"/management/v1/projects/{PROJECT}/labels").json()] == ["pii"]
    unreadable.unlink()
    assert client.post(f"/management/v1/projects/{PROJECT}/labels/pii/delete").status_code == 200
    assert client.get(f"/management/v1/projects/{PROJECT}/labels").json() == []

    assert _define(client, PII).status_code == 200
    assert client.post(f"/management/v1/table/{REFUNDS}/undrop", json={}).status_code == 200
    assert client.post(f"/management/v1/table/{REFUNDS}/labels/describe", json={}).json()["labels"] == {}
