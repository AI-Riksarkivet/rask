"""The namespace doors' `mode` field, as spec.yaml defines it, on the real catalog app over real pylance.

CreateNamespaceRequest.mode (spec.yaml): "Overwrite: the existing namespace is dropped and a new empty
namespace with this name is created." The catalog drops with Restrict semantics, so an empty namespace is
replaced with fresh ownership and a non-empty one is refused 409 code 3 (NamespaceNotEmpty).

DropNamespaceRequest.mode: "Skip: the server must return 204 indicating the drop operation has
succeeded." Skip is the retry lever for a drop that removed the namespace and died before its trailer
ran, so a Skip over an absent namespace finishes that trailer: no tuple, protection record, policy or
binding survives for the next namespace created at the id.

The OpenFGA store is an in-memory double behind the real `service_kit.governed.fga` functions, so the
seed and revoke paths run as written; every check is allowed, because who may act is not the claim here.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from lance_namespace import DropNamespaceRequest, LanceNamespace
from openfga_sdk.client.models import ClientCheckRequest, ClientWriteRequest
from openfga_sdk.models import CheckResponse, ReadRequestTupleKey, ReadResponse, Tuple, TupleKey

from catalog.api.dependencies import get_namespace
from catalog.core.config import Settings, get_settings
from catalog.services import warehouses
from service_kit.control_emit import CatalogControlEvent
from service_kit.lakehouse import maintenance_policies
from tests.integration.openfga_grants import act_as


AUTH = {"Authorization": "Bearer t"}
WAREHOUSE = "acme-bucket"

type Triple = tuple[str, str, str]


class _TupleStore:
    """An OpenFGA store held in memory, answering the three client calls the catalog's seed and revoke make."""

    def __init__(self) -> None:
        self.tuples: set[Triple] = set()

    async def check(self, body: ClientCheckRequest, options: dict[str, int | str | dict[str, int | str]] | None = None) -> CheckResponse:
        del body, options
        return CheckResponse(allowed=True)

    async def read(self, body: ReadRequestTupleKey, options: dict[str, int | str | dict[str, int | str]] | None = None) -> ReadResponse:
        del options
        held = [Tuple(key=TupleKey(user=u, relation=r, object=o), timestamp=datetime.now(UTC)) for u, r, o in sorted(self.tuples) if o == body.object]
        return ReadResponse(tuples=held, continuation_token="")

    async def write(self, body: ClientWriteRequest, options: dict[str, int | str | dict[str, int | str]] | None = None) -> None:
        del options
        self.tuples |= {(t.user, t.relation, t.object) for t in body.writes or []}
        self.tuples -= {(t.user, t.relation, t.object) for t in body.deletes or []}

    async def close(self) -> None:
        return None

    def naming(self, obj: str) -> set[Triple]:
        """Every held tuple that names ``obj`` as its object or its user."""
        return {t for t in self.tuples if obj in (t[0], t[2])}


class _Announced:
    """A control emitter that records each event's action."""

    def __init__(self) -> None:
        self.actions: list[str] = []

    async def emit(self, event: CatalogControlEvent) -> None:
        self.actions.append(event.action)


@pytest.fixture
def governed(real_ns_client: TestClient, tmp_path: Path) -> Iterator[_TupleStore]:
    """The real catalog with OIDC, FGA and warehouses on, as the chart deploys it (`catalog.warehouses.enabled:
    true` → `LANCE_WAREHOUSES_ENABLED`), acting as the person `dex~alice` over an in-memory tuple store, with warehouse
    ``WAREHOUSE`` rooted at the catalog's own root."""
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
            "s3_access_key_id": "x",
            "s3_secret_access_key": "x",
        }
    )
    real_ns_client.app.dependency_overrides[get_settings] = lambda: settings
    real_ns_client.app.state.oidc = MagicMock()
    act_as(real_ns_client, "alice")
    store = _TupleStore()
    real_ns_client.app.state.fga = store
    record = {"id": WAREHOUSE, "bucket": WAREHOUSE, "root_uri": root, "project": "acme", "status": "active", "created_at": "t"}
    warehouses.put_warehouse(root, {}, record)
    yield store


def _create_top_level(client: TestClient, name: str) -> None:
    """A top-level namespace, through the warehouse door the estate requires for one."""
    resp = client.post(f"/management/v1/warehouses/{WAREHOUSE}/namespaces", json={"namespace": name}, headers=AUTH)
    assert resp.status_code == 200, resp.text


def _ns(client: TestClient) -> LanceNamespace:
    return client.app.dependency_overrides[get_namespace]()


def test_skip_over_a_half_dropped_namespace_is_refused_as_any_destructive_door_on_an_absent_name(
    real_ns_client: TestClient, governed: _TupleStore, tmp_path: Path
) -> None:
    """A forced drop removed the namespace and died before its trailer; with FGA on, the Skip retry is refused.

    The namespace's grants are held by its uuid ([[LH-524]]), and once it is gone nothing maps its name to that uuid,
    so no rung can be asked: a destructive door refuses an absent name 403, as it refuses a forbidden one, and so
    discloses no existence. What the dead namespace leaves is a binding, which the warehouse door unbinds
    (`DELETE /management/v1/warehouses/{warehouse_id}/namespaces/{top_ns}`), and grants and records on a uuid no
    namespace will hold again.
    """
    root = str(tmp_path)
    _create_top_level(real_ns_client, "ghost")
    _ns(real_ns_client).drop_namespace(DropNamespaceRequest(id=["ghost"]))

    resp = real_ns_client.post("/v1/namespace/ghost/drop?force=true", json={"mode": "Skip"}, headers=AUTH)

    assert (resp.status_code, resp.json()["code"]) == (403, 15), resp.text
    assert warehouses.binding_for_namespace(root, {}, "ghost") is not None, "a refused retry changed the binding"


def _properties(client: TestClient, namespace: str) -> dict[str, str]:
    described = client.post(f"/v1/namespace/{namespace}/describe", json={}, headers=AUTH)
    assert described.status_code == 200, described.text
    return described.json().get("properties") or {}


def test_overwrite_replaces_an_empty_namespace_with_fresh_ownership_and_a_new_uuid(real_ns_client: TestClient, governed: _TupleStore, tmp_path: Path) -> None:
    """The old owner, a later grant and the old policy die with the old namespace; the overwriter owns the new one,
    which is a new namespace under a new uuid ([[LH-519]], ADR 0173 §2).

    A top-level namespace bound to a warehouse: the replacement keeps the binding and hangs off the same
    warehouse, so the generic door's warehouse requirement is met rather than refused.
    """
    _create_top_level(real_ns_client, "ghost")
    assert real_ns_client.post("/management/v1/namespace/ghost/policy/set", json={"retention_days": 7}, headers=AUTH).status_code == 200
    replaced = _properties(real_ns_client, "ghost")["rask.uuid"]
    old = f"namespace:{replaced}"
    governed.tuples.add(("user:dex~carol", "reader", old))
    edges = {t for t in governed.naming(old) if t[1] in ("parent", "child")}
    assert edges, "the create seeded no edge to the warehouse"
    act_as(real_ns_client, "bob")

    resp = real_ns_client.post("/v1/namespace/ghost/create", json={"mode": "Overwrite", "properties": {"team": "b"}}, headers=AUTH)

    assert resp.status_code == 200, resp.text
    properties = _properties(real_ns_client, "ghost")
    assert properties.get("rask.uuid") not in (None, replaced), f"the replacement answers the replaced namespace's uuid: {properties}"
    assert {key: value for key, value in properties.items() if key != "rask.uuid"} == {"team": "b"}
    new = f"namespace:{properties['rask.uuid']}"
    assert governed.naming(old) == set(), "a grant on the replaced namespace survived it"
    assert governed.naming(new) == {("user:dex~bob", "owner", new), *{tuple(new if part == old else part for part in edge) for edge in edges}}
    assert maintenance_policies.get_policy(str(tmp_path), {}, "namespace", replaced) is None
    assert (warehouses.binding_for_namespace(str(tmp_path), {}, "ghost") or {}).get("warehouse_id") == WAREHOUSE


def test_overwrite_refuses_a_namespace_that_holds_anything(real_ns_client: TestClient, governed: _TupleStore) -> None:
    """Overwrite drops with Restrict semantics: a namespace with a child answers NamespaceNotEmpty naming it."""
    _create_top_level(real_ns_client, "full")
    assert real_ns_client.post("/v1/namespace/full$inner/create", json={}, headers=AUTH).status_code == 200
    held = set(governed.tuples)

    resp = real_ns_client.post("/v1/namespace/full/create", json={"mode": "Overwrite"}, headers=AUTH)

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == 3
    assert "inner" in body["detail"]
    assert real_ns_client.post("/v1/namespace/full$inner/exists", json={}, headers=AUTH).status_code == 200
    assert governed.tuples == held, "a refused overwrite revoked grants"
