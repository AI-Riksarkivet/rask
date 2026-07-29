"""Unit tests for the estate-admin FGA administration API (``/v1/access``, endpoints/access_admin.py).

Drives the handlers directly (no TestClient) with fake settings + monkeypatched ``fga`` wrappers,
mirroring ``test_events_endpoint.py``. Pins the frozen contract: the ``/v1/events``-style estate gate
(``can_observe_events`` on the fixed root object), the OpenFGA Read filter combos (object type REQUIRED
whenever any tuple filter is sent — a user-only filter is a clean 400), pagination pass-through, the
model-true write validation (only directly-assignable relations; a derived ``can_*`` write must never
reach OpenFGA where its 400 would be swallowed as idempotent success), and the write/delete audit on top
of the gate's allow. Relation/type validation runs against the REAL compiled model.json (never a fake),
the ``test_fga_model_contract`` posture.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from catalog.api.v1.endpoints import access_admin as ep
from catalog.schemas import (
    AccessExpandRequest,
    AccessListObjectsRequest,
    AccessListUsersRequest,
    AccessTuple,
)
from lance_namespace import (
    InvalidInputError,
    ServiceUnavailableError,
    UnsupportedOperationError,
)

from service_kit.governed import fga


def _settings(*, fga_enabled: bool = True) -> Any:
    return SimpleNamespace(fga_enabled=fga_enabled, fga_root_object="warehouse:lance_catalog", fga_model_id=None)


def _request(client: Any = None) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fga=client)))


def _token(sub: str) -> Any:
    return SimpleNamespace(sub=sub)


class _AuditRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, action: str, outcome: str, **fields: Any) -> None:
        self.calls.append((action, outcome, fields))


@pytest.fixture
def gate_seen(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub the estate gate's require_relation, recording what it was asked to check."""
    seen: dict[str, Any] = {}

    async def _fake_require(_client: Any, _settings: Any, _token: Any, *, relation: str, obj: str) -> None:
        seen["relation"] = relation
        seen["obj"] = obj

    monkeypatch.setattr(ep.fga_deps, "require_relation", _fake_require)
    return seen


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> _AuditRecorder:
    recorder = _AuditRecorder()
    monkeypatch.setattr(ep, "audit", recorder)
    return recorder


def _read(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> tuple[dict[str, Any], Any]:
    """Call the tuple-read handler with a recording fake ``fga.read_tuples``; return (seen, response)."""
    seen: dict[str, Any] = {}

    async def _fake_read_tuples(_client: Any, **read_kwargs: Any) -> tuple[list[Any], str | None]:
        seen.update(read_kwargs)
        return (
            [fga.ClientTuple(user="user:alice", relation="reader", object="table:db1$t")],
            "tok-next",
        )

    monkeypatch.setattr(ep.fga, "read_tuples", _fake_read_tuples)
    response = asyncio.run(
        ep.read_access_tuples(
            request=_request(client=object()),
            settings=_settings(),
            token=_token("root_admin"),
            **kwargs,
        )
    )
    return seen, response


# ── the estate gate (mirrors /v1/events) ─────────────────────────────────────────────────────────────


def test_gate_is_can_observe_events_on_the_root_object(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    _read(monkeypatch, object_type="table")
    # Estate-wide surface → platform privilege on the FIXED root object, never a caller-supplied one.
    assert gate_seen == {"relation": "can_observe_events", "obj": "warehouse:lance_catalog"}


def test_fga_off_is_unsupported(rec: _AuditRecorder) -> None:
    with pytest.raises(UnsupportedOperationError):
        asyncio.run(ep.read_access_tuples(request=_request(client=object()), settings=_settings(fga_enabled=False), token=None))


def test_enabled_but_unwired_client_fails_closed(gate_seen: dict[str, Any], rec: _AuditRecorder) -> None:
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(ep.read_access_tuples(request=_request(client=None), settings=_settings(), token=None))
    assert gate_seen == {}  # 503'd before any check could run


# ── read filters (OpenFGA Read: object type required whenever a tuple filter is sent) ─────────────────


def test_object_type_only_scans_the_whole_type(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    seen, response = _read(monkeypatch, object_type="table")
    assert seen["obj"] == "table:" and seen["user"] is None
    assert [t.user for t in response.tuples] == ["user:alice"]
    assert response.continuation == "tok-next"


def test_full_object_filter_passes_verbatim(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    seen, _ = _read(monkeypatch, object="table:db1$t")
    assert seen["obj"] == "table:db1$t" and seen["user"] is None


def test_user_plus_object_type_qualifies_the_bare_subject(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    seen, _ = _read(monkeypatch, user="alice", object_type="namespace")
    assert seen["user"] == "user:alice" and seen["obj"] == "namespace:"


def test_qualified_userset_filter_passes_verbatim(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    # An already-qualified subject/userset must NOT be double-prefixed (user:team:… would never match).
    seen, _ = _read(monkeypatch, user="team:acme#member", object_type="table")
    assert seen["user"] == "team:acme#member"


def test_user_only_filter_is_a_400_explaining_why(gate_seen: dict[str, Any], rec: _AuditRecorder) -> None:
    with pytest.raises(InvalidInputError, match="object type"):
        asyncio.run(ep.read_access_tuples(request=_request(client=object()), settings=_settings(), token=None, user="alice"))


def test_no_filter_reads_the_whole_store(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    seen, _ = _read(monkeypatch)
    assert seen["obj"] is None and seen["user"] is None


def test_unknown_object_type_is_a_400(gate_seen: dict[str, Any], rec: _AuditRecorder) -> None:
    with pytest.raises(InvalidInputError, match="gizmo"):
        asyncio.run(ep.read_access_tuples(request=_request(client=object()), settings=_settings(), token=None, object_type="gizmo"))


# ── pagination ────────────────────────────────────────────────────────────────────────────────────────


def test_pagination_params_pass_through_and_token_returns(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    seen, response = _read(monkeypatch, object_type="table", page_size=25, continuation="tok-prev")
    assert seen["page_size"] == 25 and seen["continuation_token"] == "tok-prev"
    assert response.continuation == "tok-next"


def test_read_audits_the_disclosure(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    _read(monkeypatch, object_type="table")
    assert rec.calls == [("access_tuples_read", "success", {"subject": "root_admin", "resource": "table:", "delivered": 1})]


# ── tuple write/delete: model-true validation + the access_grant audit pattern ────────────────────────


def _mutate(monkeypatch: pytest.MonkeyPatch, body: AccessTuple, *, write: bool, fail: bool = False) -> tuple[list[Any], Any]:
    written: list[Any] = []

    async def _fake_mutation(_client: Any, tuples: list[Any], **_kw: Any) -> None:
        if fail:
            raise ServiceUnavailableError("authorization service unavailable")
        written.extend(tuples)

    monkeypatch.setattr(ep.fga, "write_tuples" if write else "delete_tuples", _fake_mutation)
    handler = ep.write_access_tuple if write else ep.delete_access_tuple
    response = asyncio.run(
        handler(
            request=_request(client=object()),
            settings=_settings(),
            token=_token("root_admin"),
            body=body,
        )
    )
    return written, response


def test_write_qualifies_bare_user_and_audits_success(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    body = AccessTuple(user="alice", relation="reader", object="table:db1$t")
    written, response = _mutate(monkeypatch, body, write=True)
    assert len(written) == 1
    stored = (written[0].user, written[0].relation, written[0].object)
    assert stored == ("user:alice", "reader", "table:db1$t")
    assert response.user == "user:alice"  # echoes the tuple as stored, not the bare input
    # #41: the gate's allow alone would be byte-identical to a read — the write carries WHAT was planted.
    assert rec.calls == [
        (
            "access_tuple_write",
            "success",
            {
                "subject": "root_admin",
                "resource": "table:db1$t",
                "grantee": "user:alice",
                "relation": "reader",
            },
        )
    ]


def test_delete_audits_its_own_event(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    body = AccessTuple(user="team:acme#member", relation="writer", object="namespace:bronze")
    written, _ = _mutate(monkeypatch, body, write=False)
    assert written[0].user == "team:acme#member"  # userset passes verbatim
    assert rec.calls[0][0] == "access_tuple_delete" and rec.calls[0][1] == "success"


def test_write_outage_audits_failure_and_raises(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    body = AccessTuple(user="alice", relation="reader", object="table:db1$t")
    with pytest.raises(ServiceUnavailableError):
        _mutate(monkeypatch, body, write=True, fail=True)
    assert rec.calls[0][:2] == ("access_tuple_write", "failure")


def test_write_rejects_a_derived_can_relation(gate_seen: dict[str, Any], rec: _AuditRecorder) -> None:
    # can_read_data is DEFINED on table but not directly assignable — OpenFGA would 400 it with the same
    # error class the idempotent-duplicate handling swallows, so it must never leave this process.
    body = AccessTuple(user="alice", relation="can_read_data", object="table:db1$t")
    with pytest.raises(InvalidInputError, match="can_read_data"):
        asyncio.run(ep.write_access_tuple(request=_request(client=object()), settings=_settings(), token=None, body=body))


def test_write_rejects_unknown_type_and_bare_object(gate_seen: dict[str, Any], rec: _AuditRecorder) -> None:
    for bad in ("gizmo:x", "table", "table:"):
        with pytest.raises(InvalidInputError):
            asyncio.run(
                ep.write_access_tuple(
                    request=_request(client=object()),
                    settings=_settings(),
                    token=None,
                    body=AccessTuple(user="alice", relation="reader", object=bad),
                )
            )


# ── the model + check surfaces ────────────────────────────────────────────────────────────────────────


def test_model_returns_the_checked_in_dsl_and_pinned_id(gate_seen: dict[str, Any], rec: _AuditRecorder) -> None:
    client = SimpleNamespace(get_authorization_model_id=lambda: "01MODEL")
    response = asyncio.run(ep.get_access_model(request=_request(client=client), settings=_settings(), token=None))
    assert response.authorization_model_id == "01MODEL"
    assert response.dsl == fga.load_model_dsl() and "type table" in response.dsl


def test_check_probes_any_defined_relation_with_qualified_subject(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def _fake_check(_client: Any, **kwargs: Any) -> bool:
        seen.update(kwargs)
        return True

    monkeypatch.setattr(ep.fga, "check", _fake_check)
    response = asyncio.run(
        ep.check_access(
            request=_request(client=object()),
            settings=_settings(),
            token=_token("root_admin"),
            body=AccessTuple(user="alice", relation="writer", object="namespace:bronze"),
        )
    )
    # qualify=False + pre-resolved subject: fga.check must see the full subject verbatim (the 2026-07-20
    # double-prefix regression), and the verdict echoes the exact tuple probed.
    assert seen == {"user": "user:alice", "relation": "writer", "obj": "namespace:bronze", "qualify": False}
    assert response.allowed is True
    assert (response.checked.user, response.checked.relation) == ("user:alice", "writer")
    assert rec.calls == [("access_simulate", "success", {"subject": "root_admin", "resource": "namespace:bronze"})]


def test_check_rejects_a_phantom_relation(gate_seen: dict[str, Any], rec: _AuditRecorder) -> None:
    # A relation the compiled model does not define would be an OpenFGA 400 → 503 for the caller; it must
    # be a clean 400 here instead (the test_fga_model_contract posture).
    with pytest.raises(InvalidInputError, match="can_fly"):
        asyncio.run(
            ep.check_access(
                request=_request(client=object()),
                settings=_settings(),
                token=None,
                body=AccessTuple(user="alice", relation="can_fly", object="table:db1$t"),
            )
        )


# ── the derivation surfaces: list-objects / list-users / expand ───────────────────────────────────────
#
# These three exist because the tuple browser structurally cannot answer the questions an operator
# actually asks. Read needs an object type per call and returns only STORED tuples, so "what can alice
# reach" meant one audited read per model type and still missed every derived grant; and nothing at all
# could say WHY a grant resolves. Each is gated, model-validated and audited exactly like its siblings —
# these pin that, because a surface that discloses the whole estate's ACLs without an audit row is worse
# than one that does not exist.


def test_list_objects_clears_the_estate_gate_and_qualifies_a_bare_subject(
    gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def _fake(_client: Any, **kwargs: Any) -> list[str]:
        seen.update(kwargs)
        return ["table:db1$t", "table:db1$u"]

    monkeypatch.setattr(ep.fga, "list_objects", _fake)
    response = asyncio.run(
        ep.list_access_objects(
            request=_request(client=object()),
            settings=_settings(),
            token=_token("root_admin"),
            body=AccessListObjectsRequest(user="alice", relation="can_read_data", type="table"),
        )
    )
    assert (gate_seen["relation"], gate_seen["obj"]) == ("can_observe_events", "warehouse:lance_catalog")
    # qualify=False with a pre-resolved subject — the same double-prefix guard `check` carries.
    assert seen == {"user": "user:alice", "relation": "can_read_data", "object_type": "table", "qualify": False}
    assert response.objects == ["table:db1$t", "table:db1$u"]
    assert (response.user, response.type) == ("user:alice", "table")
    assert rec.calls[0][:2] == ("access_list_objects", "success")
    assert rec.calls[0][2]["delivered"] == 2


def test_list_objects_sends_a_userset_verbatim(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    # The model refuses `team#member` on resource rungs by design — a team reaches data through a role —
    # so the userset question is the one that explains a real grant, and it must not be re-prefixed.
    seen: dict[str, Any] = {}

    async def _fake(_client: Any, **kwargs: Any) -> list[str]:
        seen.update(kwargs)
        return []

    monkeypatch.setattr(ep.fga, "list_objects", _fake)
    asyncio.run(
        ep.list_access_objects(
            request=_request(client=object()),
            settings=_settings(),
            token=None,
            body=AccessListObjectsRequest(user="role:validators#assignee", relation="can_promote", type="namespace"),
        )
    )
    assert seen["user"] == "role:validators#assignee"


def test_list_objects_rejects_an_unknown_type_and_a_phantom_relation(gate_seen: dict[str, Any], rec: _AuditRecorder) -> None:
    with pytest.raises(InvalidInputError, match="dragon"):
        asyncio.run(
            ep.list_access_objects(
                request=_request(client=object()),
                settings=_settings(),
                token=None,
                body=AccessListObjectsRequest(user="alice", relation="reader", type="dragon"),
            )
        )
    with pytest.raises(InvalidInputError, match="can_fly"):
        asyncio.run(
            ep.list_access_objects(
                request=_request(client=object()),
                settings=_settings(),
                token=None,
                body=AccessListObjectsRequest(user="alice", relation="can_fly", type="table"),
            )
        )


def test_list_objects_outage_audits_failure_and_raises(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _down(_client: Any, **_kwargs: Any) -> list[str]:
        raise ServiceUnavailableError("openfga down")

    monkeypatch.setattr(ep.fga, "list_objects", _down)
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(
            ep.list_access_objects(
                request=_request(client=object()),
                settings=_settings(),
                token=_token("root_admin"),
                body=AccessListObjectsRequest(user="alice", relation="can_read_data", type="table"),
            )
        )
    assert rec.calls[0][:2] == ("access_list_objects", "failure")


def test_list_users_returns_qualified_subjects_and_flags_truncation(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def _fake(_client: Any, **kwargs: Any) -> list[str]:
        seen.update(kwargs)
        return [f"user:u{i}" for i in range(fga.LIST_USERS_SERVER_CAP)]

    monkeypatch.setattr(ep.fga, "list_users", _fake)
    response = asyncio.run(
        ep.list_access_users(
            request=_request(client=object()),
            settings=_settings(),
            token=_token("root_admin"),
            body=AccessListUsersRequest(object="table:db1$t", relation="can_read_data"),
        )
    )
    assert seen["qualified"] is True and seen["user_type"] == "user"
    # ListUsers has no pagination; a result at the server ceiling is likely truncated, and an
    # under-reported access review must never render as a complete one.
    assert response.truncated is True
    assert rec.calls[0][:2] == ("access_list_users", "success")


def test_list_users_passes_a_userset_filter_through(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def _fake(_client: Any, **kwargs: Any) -> list[str]:
        seen.update(kwargs)
        return ["role:validators#assignee"]

    monkeypatch.setattr(ep.fga, "list_users", _fake)
    response = asyncio.run(
        ep.list_access_users(
            request=_request(client=object()),
            settings=_settings(),
            token=None,
            body=AccessListUsersRequest(object="namespace:bronze", relation="validator", user_type="role", user_relation="assignee"),
        )
    )
    assert (seen["user_type"], seen["user_relation"]) == ("role", "assignee")
    assert response.users == ["role:validators#assignee"]
    assert response.truncated is False


def test_list_users_rejects_a_phantom_subject_filter(gate_seen: dict[str, Any], rec: _AuditRecorder) -> None:
    with pytest.raises(InvalidInputError, match="dragon"):
        asyncio.run(
            ep.list_access_users(
                request=_request(client=object()),
                settings=_settings(),
                token=None,
                body=AccessListUsersRequest(object="table:db1$t", relation="reader", user_type="dragon"),
            )
        )
    with pytest.raises(InvalidInputError, match="can_fly"):
        asyncio.run(
            ep.list_access_users(
                request=_request(client=object()),
                settings=_settings(),
                token=None,
                body=AccessListUsersRequest(object="table:db1$t", relation="reader", user_type="role", user_relation="can_fly"),
            )
        )


def test_expand_returns_the_tree_and_records_the_depth(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def _fake(_client: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"name": "table:db1$t#reader", "leaf": {"users": ["user:alice"], "computed": None, "tuple_to_userset": None}}

    monkeypatch.setattr(ep.fga, "expand_tree", _fake)
    response = asyncio.run(
        ep.expand_access(
            request=_request(client=object()),
            settings=_settings(),
            token=_token("root_admin"),
            body=AccessExpandRequest(object="table:db1$t", relation="reader", depth=3),
        )
    )
    assert (seen["relation"], seen["obj"], seen["max_depth"]) == ("reader", "table:db1$t", 3)
    assert response.tree is not None and response.tree.leaf is not None
    assert response.tree.leaf.users == ["user:alice"]
    assert response.depth == 3
    assert rec.calls[0][:2] == ("access_expand", "success")


def test_expand_clamps_depth_to_the_library_ceiling(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def _fake(_client: Any, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {}

    monkeypatch.setattr(ep.fga, "expand_tree", _fake)
    # The request model caps at 6 too, but the handler must not depend on that: a caller reaching the
    # function directly (or a later schema loosening) still cannot ask for an unbounded fan-out.
    response = asyncio.run(
        ep.expand_access(
            request=_request(client=object()),
            settings=_settings(),
            token=None,
            body=AccessExpandRequest.model_construct(object="table:db1$t", relation="reader", depth=9999),
        )
    )
    assert seen["max_depth"] == fga.MAX_EXPAND_TREE_DEPTH
    assert response.depth == fga.MAX_EXPAND_TREE_DEPTH


def test_expand_empty_tree_is_null_not_an_empty_object(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    # "resolves to nothing" and "could not ask" must stay distinguishable on the wire — the second is
    # the 503 below, and neither may be rendered as the other.
    async def _empty(_client: Any, **_kwargs: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(ep.fga, "expand_tree", _empty)
    response = asyncio.run(
        ep.expand_access(
            request=_request(client=object()),
            settings=_settings(),
            token=None,
            body=AccessExpandRequest(object="table:db1$t", relation="reader"),
        )
    )
    assert response.tree is None


def test_expand_outage_audits_failure_and_raises(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _down(_client: Any, **_kwargs: Any) -> dict[str, Any]:
        raise ServiceUnavailableError("openfga down")

    monkeypatch.setattr(ep.fga, "expand_tree", _down)
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(
            ep.expand_access(
                request=_request(client=object()),
                settings=_settings(),
                token=_token("root_admin"),
                body=AccessExpandRequest(object="table:db1$t", relation="reader"),
            )
        )
    assert rec.calls[0][:2] == ("access_expand", "failure")


def test_expand_rejects_a_phantom_relation(gate_seen: dict[str, Any], rec: _AuditRecorder) -> None:
    with pytest.raises(InvalidInputError, match="can_fly"):
        asyncio.run(
            ep.expand_access(
                request=_request(client=object()),
                settings=_settings(),
                token=None,
                body=AccessExpandRequest(object="table:db1$t", relation="can_fly"),
            )
        )


def test_every_derivation_surface_is_fga_gated(rec: _AuditRecorder) -> None:
    """FGA off → 501 on all three, like every other /v1/access handler. A surface that answers "who can
    do what" while the store is unconfigured would be inventing an answer."""
    off = _settings(fga_enabled=False)
    with pytest.raises(UnsupportedOperationError):
        asyncio.run(
            ep.list_access_objects(
                request=_request(),
                settings=off,
                token=None,
                body=AccessListObjectsRequest(user="alice", relation="can_read_data", type="table"),
            )
        )
    with pytest.raises(UnsupportedOperationError):
        asyncio.run(
            ep.list_access_users(
                request=_request(),
                settings=off,
                token=None,
                body=AccessListUsersRequest(object="table:db1$t", relation="can_read_data"),
            )
        )
    with pytest.raises(UnsupportedOperationError):
        asyncio.run(
            ep.expand_access(
                request=_request(),
                settings=off,
                token=None,
                body=AccessExpandRequest(object="table:db1$t", relation="reader"),
            )
        )
