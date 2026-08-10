"""#72 grant/revoke — the MUTATE half of the access surface must be fail-closed and precise.

Pins: only a grantable BASE rung (owner/writer/reader/validator) may be written — a derived ``can_*`` action
or the structural ``parent`` edge is a 4xx, never a junk tuple; the grantee is resolved like access/check
(bare id → ``user:…``, a qualified userset verbatim); grant writes, revoke deletes; an OpenFGA outage
propagates (fail-closed), never a silent no-op. Sync via ``asyncio.run`` — no async-plugin dependency.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from catalog.api.v1.endpoints import access
from catalog.core.config import Settings
from lance_namespace import ServiceUnavailableError, UnsupportedOperationError

from service_kit.governed.oidc import IDToken


def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    user: str,
    relation: str,
    grant: bool,
    fga_type: str = "table",
    ident: str = "db1$users",
    outage: bool = False,
) -> tuple[access.AccessGrantResponse, list[Any]]:
    captured: list[Any] = []

    async def fake_write(_client: object, tuples: list[object], **_kw: object) -> None:
        if outage:
            raise ServiceUnavailableError("fga down")
        captured.extend(tuples)

    async def fake_delete(_client: object, tuples: list[object], **_kw: object) -> None:
        if outage:
            raise ServiceUnavailableError("fga down")
        captured.extend(tuples)

    monkeypatch.setattr(access.fga, "write_tuples", fake_write)
    monkeypatch.setattr(access.fga, "delete_tuples", fake_delete)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fga=object())))
    settings = cast(Settings, SimpleNamespace(fga_enabled=True, delimiter="$"))
    token = cast(IDToken, SimpleNamespace(sub="alice"))
    body = access.AccessGrantRequest(user=user, relation=relation)
    resp = asyncio.run(access._access_mutate(cast(access.Request, request), settings, token, fga_type, ident, body, grant=grant))
    return resp, captured


def test_grantable_relations_are_the_base_rungs_only() -> None:
    """The DIRECTLY ASSIGNABLE rungs — never a derived ``can_*`` action or a structural edge.

    The set grew when granting became its own axis: ``manage_grants`` and ``pass_grants`` are real
    assignable relations, and omitting them would define a delegation the API could not confer. They
    are not a widening of who may grant — each is reachable only through ``can_grant_manage_grants``
    / ``can_grant_pass_grants``, both ``manage_grants``-only, so a grant-option delegate can neither
    mint further delegates nor promote themselves.

    Asserted as an exact tuple on purpose: this list is what the grant API will accept, so a rung
    appearing here without a ``can_grant_*`` gate would be grantable and ungated. That pairing is
    proven in ``test_fga_model_contract`` — this half pins the membership.
    """
    for t in ("table", "namespace"):
        assert access._grantable_relations(t) == ("owner", "writer", "reader", "validator", "manage_grants", "pass_grants")


def test_grant_writes_the_tuple_and_reports_granted(monkeypatch: pytest.MonkeyPatch) -> None:
    resp, captured = _run(monkeypatch, user="bob", relation="reader", grant=True)
    assert resp.granted is True
    assert len(captured) == 1
    assert captured[0].user == "user:bob"
    assert captured[0].relation == "reader"
    assert captured[0].object == "table:db1$users"
    assert resp.object == "table:db1$users"


def test_revoke_deletes_the_tuple_and_reports_not_granted(monkeypatch: pytest.MonkeyPatch) -> None:
    resp, captured = _run(monkeypatch, user="bob", relation="writer", grant=False)
    assert resp.granted is False
    assert captured[0].relation == "writer"


def test_userset_grantee_passes_through_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    _, captured = _run(monkeypatch, user="role:project_admin#assignee", relation="reader", grant=True)
    assert captured[0].user == "role:project_admin#assignee"  # NOT double-prefixed to user:role:…


def test_derived_can_relation_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(UnsupportedOperationError):
        _run(monkeypatch, user="bob", relation="can_read_data", grant=True)


def test_structural_parent_edge_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(UnsupportedOperationError):
        _run(monkeypatch, user="bob", relation="parent", grant=True)


def test_fga_outage_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ServiceUnavailableError):
        _run(monkeypatch, user="bob", relation="reader", grant=True, outage=True)


def test_access_graph_builds_nodes_and_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    # #81: read_object_tuples → a one-hop graph. A grant tuple is an inbound edge subject→obj labelled with
    # the rung; the parent tuple is an outbound edge obj→parent.
    tuples: list[Any] = [
        access.fga.ClientTuple(user="user:alice", relation="owner", object="table:db1$t"),
        access.fga.ClientTuple(user="role:eng#assignee", relation="reader", object="table:db1$t"),
        access.fga.ClientTuple(user="namespace:db1", relation="parent", object="table:db1$t"),
    ]

    async def fake_read(_client: object, _obj: str) -> list[Any]:
        return tuples

    monkeypatch.setattr(access.fga, "read_object_tuples", fake_read)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fga=object())))
    settings = cast(Settings, SimpleNamespace(fga_enabled=True, delimiter="$"))
    token = cast(IDToken, SimpleNamespace(sub="alice"))
    resp = asyncio.run(access._access_graph(cast(access.Request, request), settings, token, "table", "db1$t"))

    assert resp.object == "table:db1$t"
    assert {n.id for n in resp.nodes} == {"table:db1$t", "user:alice", "role:eng#assignee", "namespace:db1"}
    # the table node is typed from its prefix
    assert next(n for n in resp.nodes if n.id == "table:db1$t").type == "table"
    edges = {(e.source, e.target, e.relation) for e in resp.edges}
    assert ("user:alice", "table:db1$t", "owner") in edges  # grant: subject → obj
    assert ("role:eng#assignee", "table:db1$t", "reader") in edges
    assert ("table:db1$t", "namespace:db1", "parent") in edges  # container: obj → parent
