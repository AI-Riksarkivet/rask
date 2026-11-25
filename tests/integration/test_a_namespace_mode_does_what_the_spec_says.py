"""The namespace doors' `mode` field, as spec.yaml defines it, on the real catalog app over real pylance.

CreateNamespaceRequest.mode (spec.yaml): "Overwrite: the existing namespace is dropped and a new empty
namespace with this name is created." The catalog drops with Restrict semantics, so an empty namespace is
replaced with fresh ownership and a non-empty one is refused 409 code 3 (NamespaceNotEmpty).

DropNamespaceRequest.mode: "Skip: the server must return 204 indicating the drop operation has
succeeded." Skip is the retry lever for a drop that removed the namespace and died before its trailer
ran, so a Skip over an absent namespace finishes that trailer: no tuple, protection record or policy survives for the
next namespace created at the id.

Against the catalog as deployed, a loopback Dex signing callers in and a real OpenFGA deciding on a store of the test's
own: alice and bob may each create a namespace in the warehouse (``writer`` on it), and only a namespace's owner may
drop it, so the seed and revoke paths run as written and every refusal is the model's.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from lance_namespace import DropNamespaceRequest, LanceNamespace

from catalog.api.dependencies import get_namespace
from catalog.core.config import get_settings
from catalog.services import warehouses
from lakehouse.naming import UUID_PROPERTY
from lakehouse.records import maintenance_policies
from lakehouse.records.warehouse_records import WarehouseRecord
from lakehouse.storage.options import StorageOptions
from tests.app_state import app_of
from tests.governed_catalog import GovernedCatalog


WAREHOUSE = "acme-bucket"
WAREHOUSE_OBJECT = f"warehouse:{WAREHOUSE}"

type Triple = tuple[str, str, str]


@pytest.fixture
def governed(governed_catalog: GovernedCatalog, tmp_path: Path) -> GovernedCatalog:
    """The catalog as deployed, acting as alice, with warehouse ``WAREHOUSE`` rooted at the catalog's own root and its own
    row there ([[LH-593]]): its record is written where the warehouse door writes it, which provisions a bucket a ``dir``
    estate has none of. alice may create a namespace in it."""
    settings = get_settings()
    record = {
        "id": WAREHOUSE,
        "bucket": WAREHOUSE,
        "root_uri": str(tmp_path),
        "project": "acme",
        "status": "active",
        "created_at": datetime.now(UTC).isoformat(),
    }
    warehouses.create_warehouse_record(settings.registry_root, settings.storage_options(), WarehouseRecord.model_validate(record))
    warehouses.ensure_catalog_row(_ns(governed_catalog.client), WAREHOUSE)
    governed_catalog.store.write((governed_catalog.person("alice"), "writer", WAREHOUSE_OBJECT))
    governed_catalog.act_as("alice")
    return governed_catalog


def _naming(catalog: GovernedCatalog, obj: str) -> set[Triple]:
    """Every tuple the store holds that names ``obj`` as its object or its user."""
    return {t for t in catalog.store.tuples() if obj in (t[0], t[2])}


def _create_top_level(client: TestClient, name: str) -> None:
    """A namespace ``name`` directly in the warehouse, through the namespace door."""
    resp = client.post(f"/v1/namespace/{WAREHOUSE}${name}/create", json={})
    assert resp.status_code == 200, resp.text


def _ns(client: TestClient) -> LanceNamespace:
    return app_of(client).dependency_overrides[get_namespace]()


def test_skip_over_a_half_dropped_namespace_is_refused_as_any_destructive_door_on_an_absent_name(governed: GovernedCatalog, tmp_path: Path) -> None:
    """A forced drop removed the namespace and died before its trailer; with FGA on, the Skip retry is refused.

    The namespace's grants are held by its uuid ([[LH-524]]), and once it is gone nothing maps its name to that uuid,
    so no rung can be asked: a destructive door refuses an absent name 403, as it refuses a forbidden one, and so
    discloses no existence. What the dead namespace leaves is grants and records on a uuid no namespace will hold again.
    """
    _create_top_level(governed.client, "ghost")
    _ns(governed.client).drop_namespace(DropNamespaceRequest(id=[WAREHOUSE, "ghost"]))

    resp = governed.client.post(f"/v1/namespace/{WAREHOUSE}$ghost/drop?force=true", json={"mode": "Skip"})

    assert (resp.status_code, resp.json()["code"]) == (403, 15), resp.text


def _properties(client: TestClient, namespace: str) -> dict[str, str]:
    """The properties of ``namespace``, a name inside the warehouse."""
    described = client.post(f"/v1/namespace/{WAREHOUSE}${namespace}/describe", json={})
    assert described.status_code == 200, described.text
    return described.json().get("properties") or {}


def test_overwrite_replaces_an_empty_namespace_with_fresh_ownership_and_a_new_uuid(governed: GovernedCatalog, tmp_path: Path) -> None:
    """The old owner, a later grant and the old policy die with the old namespace; the overwriter owns the new one,
    which is a new namespace under a new uuid ([[LH-519]], ADR 0173 §2).

    A namespace directly in a warehouse: the replacement hangs off the same warehouse, the parent its name names.
    """
    _create_top_level(governed.client, "ghost")
    assert governed.client.post(f"/management/v1/namespace/{WAREHOUSE}$ghost/policy/set", json={"retention_days": 7}).status_code == 200
    replaced = _properties(governed.client, "ghost")[UUID_PROPERTY]
    old = f"namespace:{replaced}"
    governed.store.write(
        (governed.person("carol"), "reader", old), (governed.person("bob"), "owner", old), (governed.person("bob"), "writer", WAREHOUSE_OBJECT)
    )
    edges = {t for t in _naming(governed, old) if t[1] in ("parent", "child")}
    assert edges, "the create seeded no edge to the warehouse"
    governed.act_as("bob")

    resp = governed.client.post(f"/v1/namespace/{WAREHOUSE}$ghost/create", json={"mode": "Overwrite", "properties": {"team": "b"}})

    assert resp.status_code == 200, resp.text
    properties = _properties(governed.client, "ghost")
    assert properties.get(UUID_PROPERTY) not in (None, replaced), f"the replacement answers the replaced namespace's uuid: {properties}"
    assert {key: value for key, value in properties.items() if key != UUID_PROPERTY} == {"team": "b"}
    new = f"namespace:{properties[UUID_PROPERTY]}"
    assert _naming(governed, old) == set(), "a grant on the replaced namespace survived it"
    policy = governed.client.post(f"/management/v1/namespace/{WAREHOUSE}$ghost/policy/describe")
    assert (policy.status_code, policy.json()) == (200, {"kind": "namespace", "id": f"{WAREHOUSE}$ghost", "uuid": properties[UUID_PROPERTY], "set": False})
    assert _naming(governed, new) == {(governed.person("bob"), "owner", new), *{tuple(new if part == old else part for part in edge) for edge in edges}}
    assert maintenance_policies.get_policy(str(tmp_path), StorageOptions(), "namespace", key=replaced) is None


@pytest.mark.parametrize(("mode", "status"), [pytest.param("ExistOk", 200, id="exist-ok-keeps-it"), pytest.param("Create", 409, id="create-conflicts")])
def test_a_create_over_a_standing_namespace_keeps_it_and_grants_its_caller_nothing(governed: GovernedCatalog, mode: str, status: int) -> None:
    """[[LH-416]] ExistOk over a namespace directly in a warehouse keeps it, and Create answers the conflict. The
    namespace is alice's either way: bob, who may create one, gains no tuple by asking for hers."""
    _create_top_level(governed.client, "kept")
    kept = _properties(governed.client, "kept")[UUID_PROPERTY]
    governed.store.write((governed.person("bob"), "writer", WAREHOUSE_OBJECT))
    held = governed.store.tuples()
    governed.act_as("bob")

    resp = governed.client.post(f"/v1/namespace/{WAREHOUSE}$kept/create", json={"mode": mode})

    assert resp.status_code == status, resp.text
    assert _properties(governed.client, "kept")[UUID_PROPERTY] == kept
    assert governed.store.tuples() == held, f"the create changed who holds what: {governed.store.tuples() ^ held}"


def test_overwrite_by_a_caller_who_may_not_delete_the_namespace_is_refused(governed: GovernedCatalog) -> None:
    """[[LH-416]] Overwrite drops the namespace, so it asks the drop's `can_delete` first: bob, who may create a
    namespace in the warehouse but owns none of alice's, is refused 403 (code 15), and the namespace and every grant stay
    as they were."""
    _create_top_level(governed.client, "kept")
    kept = _properties(governed.client, "kept")[UUID_PROPERTY]
    governed.store.write((governed.person("bob"), "writer", WAREHOUSE_OBJECT))
    held = governed.store.tuples()
    governed.act_as("bob")

    resp = governed.client.post(f"/v1/namespace/{WAREHOUSE}$kept/create", json={"mode": "Overwrite"})

    assert (resp.status_code, resp.json()["code"]) == (403, 15), resp.text
    governed.act_as("alice")
    assert _properties(governed.client, "kept")[UUID_PROPERTY] == kept, "a refused overwrite replaced the namespace"
    assert governed.store.tuples() == held, "a refused overwrite changed a grant"


def test_overwrite_refuses_a_namespace_that_holds_anything(governed: GovernedCatalog) -> None:
    """Overwrite drops with Restrict semantics: a namespace with a child answers NamespaceNotEmpty naming it."""
    _create_top_level(governed.client, "full")
    assert governed.client.post(f"/v1/namespace/{WAREHOUSE}$full$inner/create", json={}).status_code == 200
    held = governed.store.tuples()

    resp = governed.client.post(f"/v1/namespace/{WAREHOUSE}$full/create", json={"mode": "Overwrite"})

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == 3
    assert "inner" in body["detail"]
    assert governed.client.post(f"/v1/namespace/{WAREHOUSE}$full$inner/exists", json={}).status_code == 200
    assert governed.store.tuples() == held, "a refused overwrite revoked grants"
