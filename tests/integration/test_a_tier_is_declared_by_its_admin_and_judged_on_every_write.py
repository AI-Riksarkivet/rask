"""A table's tier is the `rask.tier` its namespace declares, only an admin declares it, and every write is judged under it.

ADR 0156 C8 and ADR 0160, on the real catalog app over real pylance with OIDC, FGA and warehouses on. The
OpenFGA store is an in-memory double behind the real `service_kit.governed.fga` functions that answers a
check from an explicit grant set, so who may act IS the claim here. Namespaces are named against their
tier on purpose (`sales` is silver, `gold` is untiered): a judge reading names would answer both wrong.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from openfga_sdk.client.models import ClientCheckRequest, ClientWriteRequest
from openfga_sdk.models import CheckResponse, ReadRequestTupleKey, ReadResponse

from catalog.core.config import Settings, get_settings
from catalog.services import warehouses
from service_kit.governed.audit import AUDIT_LOGGER
from service_kit.governed.oidc import IDToken


AUTH = {"Authorization": "Bearer t"}
ARROW = {**AUTH, "content-type": "application/vnd.apache.arrow.stream"}
WAREHOUSE = "acme-bucket"
PROJECT = "acme"

type Triple = tuple[str, str, str]


class _Grants:
    """An OpenFGA store held in memory that allows exactly the checks in ``allowed``."""

    def __init__(self) -> None:
        self.allowed: set[Triple] = set()

    async def check(self, body: ClientCheckRequest, options: dict[str, int | str | dict[str, int | str]] | None = None) -> CheckResponse:
        del options
        return CheckResponse(allowed=(body.user, body.relation, body.object) in self.allowed)

    async def read(self, body: ReadRequestTupleKey, options: dict[str, int | str | dict[str, int | str]] | None = None) -> ReadResponse:
        del body, options
        return ReadResponse(tuples=[], continuation_token="")

    async def write(self, body: ClientWriteRequest, options: dict[str, int | str | dict[str, int | str]] | None = None) -> None:
        del body, options

    async def close(self) -> None:
        return None

    def grant(self, user: str, *checks: tuple[str, str]) -> None:
        self.allowed |= {(f"user:{user}", relation, obj) for relation, obj in checks}


def _act_as(client: TestClient, sub: str) -> None:
    client.app.state.oidc.verify.return_value = IDToken(iss="i", sub=sub, aud="lance", exp=1, iat=1)


@pytest.fixture
def grants(real_ns_client: TestClient, tmp_path: Path) -> Iterator[_Grants]:
    """The real catalog as the chart deploys it, with warehouse ``WAREHOUSE`` of project ``PROJECT`` at its root.

    alice administers the project; bob may only create namespaces in its warehouse.
    """
    root = str(tmp_path)
    settings = Settings.model_validate(
        {
            "root": root,
            "warehouses_enabled": True,
            "oidc_enabled": True,
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
    store = _Grants()
    real_ns_client.app.state.fga = store
    warehouses.put_warehouse(root, {}, {"id": WAREHOUSE, "bucket": WAREHOUSE, "root_uri": root, "project": PROJECT, "status": "active", "created_at": "t"})
    for user in ("alice", "bob"):
        store.grant(user, ("can_create_namespace", f"warehouse:{WAREHOUSE}"))
    store.grant("alice", ("can_administer", f"project:{PROJECT}"))
    yield store


def _top_level(client: TestClient, name: str, properties: dict[str, str] | None = None) -> int:
    body: dict[str, object] = {"namespace": name} | ({"properties": properties} if properties is not None else {})
    return client.post(f"/v1/warehouses/{WAREHOUSE}/namespaces", json=body, headers=AUTH).status_code


def _tier(client: TestClient, namespace: str) -> str | None:
    described = client.post(f"/v1/namespace/{namespace}/describe", json={}, headers=AUTH)
    assert described.status_code == 200, described.text
    return (described.json().get("properties") or {}).get("rask.tier")


def _refused(resp: httpx.Response, code: int) -> None:
    assert resp.status_code in (400, 403), resp.text
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["code"] == code, resp.json()


def test_only_the_projects_admin_declares_or_drops_a_tier(real_ns_client: TestClient, grants: _Grants) -> None:
    """On the warehouse door, the nested door and an Overwrite: bob is refused PermissionDenied (15), alice lands it."""
    client = real_ns_client
    _act_as(client, "bob")
    _refused(client.post(f"/v1/warehouses/{WAREHOUSE}/namespaces", json={"namespace": "sales", "properties": {"rask.tier": "silver"}}, headers=AUTH), 15)
    assert _top_level(client, "sales") == 200, "bob may still create the namespace without declaring a tier"
    grants.grant("bob", ("can_create_namespace", "namespace:sales"))
    _refused(client.post("/v1/namespace/sales$lane/create", json={"properties": {"rask.tier": "silver"}}, headers=AUTH), 15)

    _act_as(client, "alice")
    assert _top_level(client, "ledger", {"rask.tier": "gold"}) == 200
    for user in ("alice", "bob"):
        grants.grant(user, ("can_get_metadata", "namespace:ledger"))
    assert _tier(client, "ledger") == "gold"

    _act_as(client, "bob")
    grants.grant("bob", ("can_create_namespace", "namespace:ledger"), ("can_delete", "namespace:ledger"))
    _refused(client.post("/v1/namespace/ledger/create", json={"mode": "Overwrite", "properties": {}}, headers=AUTH), 15)
    assert _tier(client, "ledger") == "gold", "a refused Overwrite dropped the tier"


def test_a_declaration_names_a_tier_its_ancestors_agree_with(real_ns_client: TestClient, grants: _Grants) -> None:
    """A value outside bronze/silver/gold, or a deeper namespace re-tiering its ancestor's, is InvalidInput (13);
    an ExistOk naming a tier the kept namespace does not have is NamespaceAlreadyExists (2)."""
    client = real_ns_client
    _act_as(client, "alice")
    _refused(client.post(f"/v1/warehouses/{WAREHOUSE}/namespaces", json={"namespace": "sales", "properties": {"rask.tier": "platinum"}}, headers=AUTH), 13)
    assert _top_level(client, "sales", {"rask.tier": "silver"}) == 200
    grants.grant("alice", ("can_create_namespace", "namespace:sales"))

    _refused(client.post("/v1/namespace/sales$lane/create", json={"properties": {"rask.tier": "bronze"}}, headers=AUTH), 13)
    assert client.post("/v1/namespace/sales$lane/create", json={"properties": {"rask.tier": "silver"}}, headers=AUTH).status_code == 200
    assert _top_level(client, "plain") == 200
    grants.grant("alice", ("can_create_namespace", "namespace:plain"))
    assert client.post("/v1/namespace/plain$sub/create", json={"properties": {"rask.tier": "bronze"}}, headers=AUTH).status_code == 200
    kept = client.post("/v1/namespace/plain$sub/create", json={"mode": "ExistOk", "properties": {"rask.tier": "gold"}}, headers=AUTH)
    assert (kept.status_code, kept.json()["code"]) == (409, 2), "ExistOk kept the namespace, so the gold it named never landed"


def _rows() -> bytes:
    table = pa.table({"id": pa.array([1, 2], pa.int64())})
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as writer:
        writer.write_table(table)
    return sink.getvalue().to_pybytes()


def test_a_write_is_judged_under_the_tier_its_namespace_declares(real_ns_client: TestClient, grants: _Grants, caplog: pytest.LogCaptureFixture) -> None:
    """Without the writer rung the write is PermissionDenied (15); with it the decision is audited under the
    declared tier, and a namespace whose NAME is a tier but which declares none is judged untiered."""
    client = real_ns_client
    _act_as(client, "alice")
    assert _top_level(client, "sales", {"rask.tier": "silver"}) == 200
    assert _top_level(client, "gold") == 200
    grants.grant(
        "alice",
        *[("can_create_table", f"namespace:{ns}") for ns in ("sales", "gold")],
        *[("can_write_data", f"table:{ns}$t") for ns in ("sales", "gold")],
    )
    for ns in ("sales", "gold"):
        assert client.post(f"/v1/table/{ns}$t/create?mode=create", content=_rows(), headers=ARROW).status_code == 200

    _act_as(client, "dave")
    _refused(client.post("/v1/table/sales$t/insert", content=_rows(), headers=ARROW), 15)

    _act_as(client, "alice")
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    for ns in ("sales", "gold"):
        assert client.post(f"/v1/table/{ns}$t/insert", content=_rows(), headers=ARROW).status_code == 200
    judged = [
        (r.__dict__["audit.resource"], r.__dict__["audit.tier"])
        for r in caplog.records
        if r.name == AUDIT_LOGGER and r.__dict__.get("audit.action") == "tier_judge" and r.__dict__.get("audit.op") == "insert"
    ]
    assert judged == [("table:sales$t", "silver"), ("table:gold$t", "untiered")]


def test_a_recoverable_drop_and_its_undrop_keep_the_declared_tier(real_ns_client: TestClient, grants: _Grants) -> None:
    """An owner who may drop and undrop is not an admin, so the round trip must bring the tier back with the namespace."""
    client = real_ns_client
    _act_as(client, "alice")
    assert _top_level(client, "ledger", {"rask.tier": "gold"}) == 200
    _act_as(client, "bob")
    grants.grant("bob", ("can_delete", "namespace:ledger"), ("can_get_metadata", "namespace:ledger"))

    assert client.post("/v1/namespace/ledger/drop", json={"behavior": "Cascade"}, headers=AUTH).status_code == 200
    assert client.post("/management/v1/namespace/ledger/undrop", json={}, headers=AUTH).status_code == 200

    assert _tier(client, "ledger") == "gold"
