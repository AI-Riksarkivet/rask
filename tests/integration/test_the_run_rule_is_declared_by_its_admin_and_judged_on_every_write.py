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
from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import lance
import pyarrow as pa
import pytest
from fastapi.concurrency import run_in_threadpool
from fastapi.testclient import TestClient

from catalog.api.dependencies import get_namespace
from catalog.core.config import Settings, get_settings
from catalog.services import object_uuids, projects, table_claims, warehouses
from lakehouse.governed import fga
from lakehouse.governed.audit import AUDIT_LOGGER
from lakehouse.storage_options import StorageOptions
from tests.integration.openfga_grants import Grants, act_as


AUTH = {"Authorization": "Bearer t"}
ARROW = {**AUTH, "content-type": "application/vnd.apache.arrow.stream"}
WAREHOUSE = "acme-bucket"
PROJECT = "acme"
#: The project and its warehouse as OpenFGA names them, by the creations their records name (ADR 0245).
PROJECT_OBJ, WAREHOUSE_OBJ = f"project:{'a' * 32}", f"warehouse:{'b' * 32}"
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
    projects.put_project(root, StorageOptions(), {"id": PROJECT, "uuid": "a" * 32, "created_at": "t"})
    warehouses.put_warehouse(
        root,
        StorageOptions(),
        {"id": WAREHOUSE, "uuid": "b" * 32, "bucket": WAREHOUSE, "root_uri": root, "project": PROJECT, "status": "active", "created_at": "t"},
    )
    warehouses.ensure_catalog_row(real_ns_client.app.dependency_overrides[get_namespace](), WAREHOUSE)
    for user in ("alice", "bob"):
        store.grant(user, ("can_create_namespace", WAREHOUSE_OBJ))
    store.grant("alice", ("can_administer", PROJECT_OBJ))
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
    for ns in (SALES, f"{SALES}$lane"):
        _refused(client.post(f"/v1/table/{ns}$t/insert", content=_rows(), headers=ARROW), 13)
    assert client.post(f"/v1/table/{WAREHOUSE}$gold$t/insert", content=_rows(), headers=ARROW).status_code == 200
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    caplog.clear()
    for ns in spaces:
        assert client.post(f"/v1/table/{ns}$t/insert", content=_rows(), headers={**ARROW, **_RUN}).status_code == 200
    judged = [
        (r.__dict__["audit.resource"], r.__dict__["audit.requires_run"])
        for r in caplog.records
        if r.name == AUDIT_LOGGER and r.__dict__.get("audit.action") == "write_judge" and r.__dict__.get("audit.op") == "insert"
    ]
    # The judgement is audited under the written table's name (``Write.obj``), as a refusal names it.
    assert judged == [(f"table:{SALES}$t", "true"), (f"table:{SALES}$lane$t", "true"), (f"table:{WAREHOUSE}$gold$t", "false")]


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


_CREATE = "/v1/table/lance_catalog$plain$t/create?mode=create"
_REGISTER = "/v1/table/lance_catalog$plain$t/register"
_RENAME = "/v1/table/lance_catalog$src$s/rename"
_INTO_PLAIN = {"new_namespace_id": ["lance_catalog", "plain"], "new_table_name": "t"}
_OVERWRITE = {"mode": "Overwrite", "properties": RULE}


#: Where a door is held right before it takes its add fence, after every check the door makes before it.
_AT_ITS_FENCE = ("holding", "")


class _Placement:
    """A door placing a table holding data no run produced into namespace `plain`, how the namespace is made afresh under
    the rule beside it, and where the door is held: ``before`` its add fence, after the write judge, and ``inside`` it,
    after it read the rule again (each a backend call, ``(method, last segment)``, or :data:`_AT_ITS_FENCE`)."""

    def __init__(self, send: Callable[[TestClient], httpx.Response], *, remade: str, before: tuple[str, str] | None, inside: tuple[str, str] | None) -> None:
        self.send, self.remade, self.before, self.inside = send, remade, before, inside


def _create_existok(client: TestClient) -> httpx.Response:
    return client.post("/v1/table/lance_catalog$plain$t/create?mode=ExistOk", content=_rows())


def _register(client: TestClient) -> httpx.Response:
    return client.post(_REGISTER, json={"location": "outside/registered"})


def _rename(client: TestClient) -> httpx.Response:
    return client.post(_RENAME, json=_INTO_PLAIN)


#: ``remade`` is "overwrite", the namespace Overwrite run whole while the door is held; "gap", the Overwrite held between
#: its drop and its re-create while the door, held before its fence until then, takes it; or "redeclared", the drop door
#: while the door is held before its fence, and then a create door declaring the rule while it is held inside it. An
#: ExistOk create is held before its fence at its probe for a standing table and inside it at its write's look.
_PLACEMENTS: dict[str, _Placement] = {
    "create": _Placement(lambda client: client.post(_CREATE, content=_rows()), remade="overwrite", before=None, inside=("describe_table", "t")),
    "create-before-its-fence": _Placement(_create_existok, remade="overwrite", before=("describe_table", "t"), inside=None),
    "register": _Placement(_register, remade="overwrite", before=("namespace_exists", "plain"), inside=None),
    "rename": _Placement(_rename, remade="overwrite", before=("namespace_exists", "plain"), inside=None),
    "create-between-drop-and-recreate": _Placement(
        lambda client: client.post(_CREATE, content=_rows()), remade="gap", before=_AT_ITS_FENCE, inside=("describe_table", "t")
    ),
    "register-between-drop-and-recreate": _Placement(_register, remade="gap", before=_AT_ITS_FENCE, inside=("register_table", "t")),
    "rename-between-drop-and-recreate": _Placement(_rename, remade="gap", before=_AT_ITS_FENCE, inside=("deregister_table", "s")),
    "create-after-a-drop-and-a-create": _Placement(_create_existok, remade="redeclared", before=("describe_table", "t"), inside=("describe_table", "t")),
}


def _gated(holding: Callable[..., AbstractAsyncContextManager[None]], gate: Any) -> Callable[..., AbstractAsyncContextManager[None]]:  # noqa: ANN401 — the lifecycle test's gate
    """``holding`` (a namespace fence's take) held at ``gate`` before it takes the fence."""

    @asynccontextmanager
    async def _holding(*args: object, **kwargs: object) -> AsyncGenerator[None]:
        await run_in_threadpool(gate.hold)
        async with holding(*args, **kwargs):
            yield

    return _holding


@pytest.mark.parametrize("door", list(_PLACEMENTS))
def test_a_table_placed_beside_the_overwrite_that_declares_the_rule_does_not_stand_under_it(
    real_ns_client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, door: str
) -> None:
    """ADR 0244 R1 and W1: a door placing a table into a namespace judges the namespace's rule while it holds the
    namespace's add fence, and refuses there a namespace that does not stand, so the namespace is not made afresh under
    the rule between that judgement and the add; and the Overwrite holds the namespace's drop fence from its drop through
    its re-create, so no placement takes the add fence while the namespace is missing and about to be remade. Whichever
    way the namespace is made afresh declaring the rule beside the door, one of the two lands, and no table holding data
    no run produced stands under the rule. A door sent while the Overwrite is between its drop and its re-create is
    refused for the Overwrite's hold (409, code 14), and one whose namespace was dropped under it for the namespace
    (404, code 1)."""
    from catalog.core import fenced_namespace
    from tests.integration.test_a_lifecycle_door_keeps_what_it_answered_beside_a_concurrent_door import _Gate, _holding, _Request

    client = real_ns_client
    for namespace in ("lance_catalog$plain", "lance_catalog$src"):
        assert client.post(f"/v1/namespace/{namespace}/create", json={}).status_code == 200
    assert client.post("/v1/table/lance_catalog$src$s/create?mode=create", content=_rows()).status_code == 200
    outside = tmp_path / "outside" / "registered"
    lance.write_dataset(pa.table({"id": pa.array([0], pa.int64())}), str(outside), data_storage_version="2.2", enable_stable_row_ids=True)
    case = _PLACEMENTS[door]
    held = _holding()
    before = held.at(*case.before, _Gate()) if case.before not in (None, _AT_ITS_FENCE) else None
    if case.before == _AT_ITS_FENCE:
        before = _Gate()
        monkeypatch.setattr(fenced_namespace, "holding", _gated(fenced_namespace.holding, before))
    inside = held.at(*case.inside, _Gate()) if case.inside is not None else None
    # The Overwrite's first create of the namespace is refused (it stands); its second is the re-create after the drop.
    if case.remade == "gap":
        held.at("create_namespace", "plain", _Gate()).release.set()
    recreate = held.at("create_namespace", "plain", _Gate()) if case.remade == "gap" else None
    placing = _Request(lambda: case.send(client))
    first = before or inside
    assert first is not None
    placing.reaches(first)
    assert first.arrived.is_set(), "the door was not held after its judgement"

    remade = 0
    remaking: _Request | None = None
    if case.remade == "overwrite":
        remade = client.post("/v1/namespace/lance_catalog$plain/create", json=_OVERWRITE).status_code
    elif case.remade == "gap":
        assert recreate is not None
        remaking = _Request(lambda: client.post("/v1/namespace/lance_catalog$plain/create", json=_OVERWRITE))
        remaking.reaches(recreate)
        assert recreate.arrived.is_set(), "the Overwrite was not held between its drop and its re-create"
    else:
        assert client.post("/v1/namespace/lance_catalog$plain/drop", json={}).status_code == 200
    if before is not None and inside is not None:
        before.release.set()
        placing.reaches(inside)
    if remaking is not None and recreate is not None:
        recreate.release.set()
        remade = remaking.answer() or 0
    elif case.remade == "redeclared":
        remade = client.post("/v1/namespace/lance_catalog$plain/create", json={"properties": RULE}).status_code
    for gate in (before, inside):
        if gate is not None:
            gate.release.set()
    placed = placing.answer()

    rule = client.post("/v1/namespace/lance_catalog$plain/describe", json={}).json()["properties"].get("rask.requires_run")
    stands = client.post("/v1/table/lance_catalog$plain$t/describe", json={}).status_code == 200
    assert not (rule == "true" and stands), (
        f"{door} answered {placed} and the namespace was made afresh {remade}: a table no run produced stands under the rule"
    )
    assert (remade == 200) != (placed == 200), f"one of the two lands: {door} {placed}, the namespace made afresh {remade}"
    if case.remade == "gap":
        assert (placed, placing.body.get("code")) == (409, 14), f"{door} was not refused for the Overwrite's hold: {placed} {placing.body}"
    elif case.remade == "redeclared":
        assert (placed, placing.body.get("code")) == (404, 1), f"{door} was not refused for the namespace it was placed in: {placed} {placing.body}"
