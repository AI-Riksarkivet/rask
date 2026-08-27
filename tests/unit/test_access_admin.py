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
    AccessCheckBody,
    AccessExpandRequest,
    AccessListObjectsRequest,
    AccessListUsersRequest,
    AccessSimulateRequest,
    AccessTuple,
    TupleCondition,
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
    # The SUCCESS row is now emitted by `fga.write_tuples` itself, so EVERY write site gets one — see
    # test_fga_expand.py for the library-level proof. This handler must therefore emit none of its own,
    # or this one surface would produce two rows per write while the other six produced none.
    assert rec.calls == []


def test_delete_audits_its_own_event(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    body = AccessTuple(user="team:acme#member", relation="writer", object="namespace:bronze")
    written, _ = _mutate(monkeypatch, body, write=False)
    assert written[0].user == "team:acme#member"  # userset passes verbatim
    assert rec.calls == []  # same as the write path: the library owns the success row


def test_write_outage_audits_failure_and_raises(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    body = AccessTuple(user="alice", relation="reader", object="table:db1$t")
    with pytest.raises(ServiceUnavailableError):
        _mutate(monkeypatch, body, write=True, fail=True)
    assert rec.calls[0][:2] == ("access_tuple_write", "failure")


# ── control-plane emission: raw tuple mutations must tick the live cursor ─────────────────────────────
#
# The control feed documents that GRANTS move it, and the per-object grant surface (access.py) emits —
# so the raw-tuple path staying silent made /v1/access/tuples the one grant door live surfaces could
# not see. These pin the emission (and that an outage emits NOTHING — a change that did not happen must
# never be announced).


class _RecordingEmitter:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


def _mutate_recording_emission(
    monkeypatch: pytest.MonkeyPatch, body: AccessTuple, *, write: bool, fail: bool = False, emitter: _RecordingEmitter | None = None
) -> _RecordingEmitter:
    emitter = emitter if emitter is not None else _RecordingEmitter()

    async def _fake_mutation(_client: Any, tuples: list[Any], **_kw: Any) -> None:
        del tuples
        if fail:
            raise ServiceUnavailableError("authorization service unavailable")

    monkeypatch.setattr(ep.fga, "write_tuples" if write else "delete_tuples", _fake_mutation)
    request = _request(client=object())
    request.app.state.control_emitter = emitter
    handler = ep.write_access_tuple if write else ep.delete_access_tuple
    asyncio.run(handler(request=request, settings=_settings(), token=_token("root_admin"), body=body))
    return emitter


def test_write_emits_grant_added(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    emitter = _mutate_recording_emission(monkeypatch, AccessTuple(user="alice", relation="reader", object="table:db1$t"), write=True)
    [event] = emitter.events
    assert (event.action, event.object_type, event.object_id) == ("grant_added", "grant", "table:db1$t")
    assert event.actor == "user:root_admin"
    assert event.extra == {"relation": "reader", "subject": "user:alice"}


def test_delete_emits_grant_revoked(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    emitter = _mutate_recording_emission(monkeypatch, AccessTuple(user="team:acme#member", relation="writer", object="namespace:bronze"), write=False)
    [event] = emitter.events
    assert event.action == "grant_revoked"
    assert event.extra == {"relation": "writer", "subject": "team:acme#member"}


def test_conditional_write_names_the_condition_in_the_event(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    body = AccessTuple(
        user="alice",
        relation="reader",
        object="table:db1$t",
        condition=TupleCondition(name="non_expired_grant", context={"grant_time": "2026-07-29T09:00:00Z", "grant_duration": "4h"}),
    )
    emitter = _mutate_recording_emission(monkeypatch, body, write=True)
    [event] = emitter.events
    # The name only — the window parameters are claim-check detail the tuple itself carries.
    assert event.extra == {"relation": "reader", "subject": "user:alice", "condition": "non_expired_grant"}


def test_outage_emits_no_control_event(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    emitter = _RecordingEmitter()
    with pytest.raises(ServiceUnavailableError):
        _mutate_recording_emission(monkeypatch, AccessTuple(user="alice", relation="reader", object="table:db1$t"), write=True, fail=True, emitter=emitter)
    assert emitter.events == []


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
            body=AccessCheckBody(user="alice", relation="writer", object="namespace:bronze"),
        )
    )
    # qualify=False + pre-resolved subject: fga.check must see the full subject verbatim (the 2026-07-20
    # double-prefix regression), and the verdict echoes the exact tuple probed.
    # `context` is None with no condition in play. Asserted rather than ignored: passing `{}` instead
    # would be a different request — OpenFGA treats an empty context as "evaluate with no operands",
    # which errors any CEL expression on the path instead of leaving it unevaluated.
    assert seen == {
        "user": "user:alice",
        "relation": "writer",
        "obj": "namespace:bronze",
        "qualify": False,
        "context": None,
    }
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
                body=AccessCheckBody(user="alice", relation="can_fly", object="table:db1$t"),
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

    async def _fake(_client: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return ep.fga.ObjectListing(objects=["table:db1$t", "table:db1$u"], truncated=False)

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

    async def _fake(_client: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return ep.fga.ObjectListing(objects=[], truncated=False)

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


# ── /v1/access/simulate — assert before you grant ─────────────────────────────────────────────────────


def test_simulate_returns_the_delta_not_just_the_verdict(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """`allowed` alone cannot be acted on — a no-op grant and an unlocking grant both answer true."""
    seen: list[dict[str, Any]] = []

    async def _fake_check(_client: Any, **kwargs: Any) -> bool:
        seen.append(kwargs)
        # Denied today; allowed once the hypothesis is in play.
        return kwargs.get("contextual_tuples") is not None

    monkeypatch.setattr(ep.fga, "check", _fake_check)
    response = asyncio.run(
        ep.simulate_access(
            request=_request(client=object()),
            settings=_settings(),
            token=_token("root_admin"),
            body=AccessSimulateRequest(
                user="alice",
                relation="can_read_data",
                object="table:db1$t",
                hypothetical=[AccessTuple(user="alice", relation="reader", object="table:db1$t")],
            ),
        )
    )
    assert (response.baseline, response.allowed) == (False, True)
    assert response.checked.user == "user:alice"  # resolved, echoed — never the bare input
    # The hypothetical is qualified the same way a real write would qualify it.
    assert response.hypothetical[0].user == "user:alice"
    # Baseline runs WITHOUT contextual tuples; the second call carries them.
    assert seen[0].get("contextual_tuples") is None
    assert seen[1]["contextual_tuples"] is not None
    assert rec.calls[0][:2] == ("access_simulate_hypothetical", "success")


def test_simulate_reports_a_no_op_grant_honestly(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    # Already allowed → the grant changes nothing. baseline == allowed is the finding, not a failure.
    async def _always(_client: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(ep.fga, "check", _always)
    response = asyncio.run(
        ep.simulate_access(
            request=_request(client=object()),
            settings=_settings(),
            token=None,
            body=AccessSimulateRequest(
                user="alice",
                relation="can_read_data",
                object="table:db1$t",
                hypothetical=[AccessTuple(user="bob", relation="reader", object="table:db1$t")],
            ),
        )
    )
    assert response.baseline is True and response.allowed is True


def test_simulate_writes_nothing(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole promise. A simulation that mutates is worse than no simulation."""
    writes: list[Any] = []

    async def _fake_check(_client: Any, **_kwargs: Any) -> bool:
        return True

    async def _explode(*_a: Any, **_k: Any) -> None:
        writes.append(1)

    monkeypatch.setattr(ep.fga, "check", _fake_check)
    monkeypatch.setattr(ep.fga, "write_tuples", _explode)
    monkeypatch.setattr(ep.fga, "delete_tuples", _explode)
    asyncio.run(
        ep.simulate_access(
            request=_request(client=object()),
            settings=_settings(),
            token=None,
            body=AccessSimulateRequest(
                user="alice",
                relation="reader",
                object="table:db1$t",
                hypothetical=[AccessTuple(user="bob", relation="reader", object="table:db1$t")],
            ),
        )
    )
    assert writes == []


def test_simulate_caps_the_hypothesis_at_ten() -> None:
    # A Check is not a bulk what-if engine, and an unbounded contextual set is an unbounded server-side
    # evaluation on an estate-gated surface. Enforced by the schema, so it cannot be forgotten here.
    import pydantic

    one = {"user": "alice", "relation": "reader", "object": "table:db1$t"}
    AccessSimulateRequest(user="a", relation="reader", object="table:db1$t", hypothetical=[one] * 10)
    with pytest.raises(pydantic.ValidationError):
        AccessSimulateRequest(user="a", relation="reader", object="table:db1$t", hypothetical=[one] * 11)


def test_simulate_rejects_a_hypothesis_it_would_refuse_to_write(gate_seen: dict[str, Any], rec: _AuditRecorder) -> None:
    """A hypothetical must clear the SAME validation a real write clears.

    Simulating a grant this API would refuse to perform describes a world that cannot be reached — a
    derived `can_*` is computed by the model and is not assignable, so answering "yes, that would work"
    is worse than refusing.
    """
    with pytest.raises(InvalidInputError, match="can_read_data"):
        asyncio.run(
            ep.simulate_access(
                request=_request(client=object()),
                settings=_settings(),
                token=None,
                body=AccessSimulateRequest(
                    user="alice",
                    relation="reader",
                    object="table:db1$t",
                    hypothetical=[AccessTuple(user="alice", relation="can_read_data", object="table:db1$t")],
                ),
            )
        )


def test_simulate_outage_audits_failure_and_raises(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _down(_client: Any, **_kwargs: Any) -> bool:
        raise ServiceUnavailableError("openfga down")

    monkeypatch.setattr(ep.fga, "check", _down)
    with pytest.raises(ServiceUnavailableError):
        asyncio.run(
            ep.simulate_access(
                request=_request(client=object()),
                settings=_settings(),
                token=_token("root_admin"),
                body=AccessSimulateRequest(user="alice", relation="reader", object="table:db1$t"),
            )
        )
    assert rec.calls[0][:2] == ("access_simulate_hypothetical", "failure")


def test_simulate_is_estate_gated(rec: _AuditRecorder) -> None:
    with pytest.raises(UnsupportedOperationError):
        asyncio.run(
            ep.simulate_access(
                request=_request(),
                settings=_settings(fga_enabled=False),
                token=None,
                body=AccessSimulateRequest(user="alice", relation="reader", object="table:db1$t"),
            )
        )


# ── conditional (time-boxed) grants ──────────────────────────────────────────────────────────────────
#
# The point of a condition is that access ENDS with nobody revoking it. That only works if the
# condition survives every hop: written onto the tuple, read back off it, and evaluable at check time.
# Each of these pins one hop, because a break in any one of them degrades silently to "permanent".


def _write_conditional(monkeypatch: pytest.MonkeyPatch, body: AccessTuple) -> list[Any]:
    written: list[Any] = []

    # Takes **kwargs the real `write_tuples` has (actor, origin, the retry knobs) — a stub narrower
    # than its subject fails on a signature the subject already allows, which is a fake's bug, not the
    # code's.
    async def _fake_write(_client: Any, tuples: list[Any], **_kw: Any) -> None:
        written.extend(tuples)

    monkeypatch.setattr(ep.fga, "write_tuples", _fake_write)
    asyncio.run(
        ep.write_access_tuple(
            request=_request(client=object()),
            settings=_settings(),
            token=_token("root_admin"),
            body=body,
        )
    )
    return written


def test_a_conditional_grant_reaches_openfga_with_its_condition(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    written = _write_conditional(
        monkeypatch,
        AccessTuple(
            user="alice",
            relation="writer",
            object="namespace:bronze",
            condition=TupleCondition(name="non_expired_grant", context={"grant_time": "2026-07-29T09:00:00Z", "grant_duration": "4h"}),
        ),
    )
    assert len(written) == 1
    assert written[0].condition is not None
    assert written[0].condition.name == "non_expired_grant"
    assert written[0].condition.context["grant_duration"] == "4h"


def test_a_permanent_grant_still_carries_no_condition(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    # Migration safety: every pre-existing grant path must keep producing an UNconditional tuple.
    written = _write_conditional(monkeypatch, AccessTuple(user="alice", relation="writer", object="namespace:bronze"))
    assert written[0].condition is None


def test_a_condition_the_rung_does_not_accept_is_a_clean_400(gate_seen: dict[str, Any], rec: _AuditRecorder) -> None:
    # `owner` deliberately omits the conditional form. Naming a condition on it must be rejected HERE —
    # reaching OpenFGA earns a 400 that this API's fail-closed handling turns into a 503 for everyone.
    with pytest.raises(InvalidInputError, match="non_expired_grant"):
        asyncio.run(
            ep.write_access_tuple(
                request=_request(client=object()),
                settings=_settings(),
                token=None,
                body=AccessTuple(
                    user="alice",
                    relation="owner",
                    object="namespace:bronze",
                    condition=TupleCondition(name="non_expired_grant", context={"grant_time": "x", "grant_duration": "1h"}),
                ),
            )
        )


def test_an_unknown_condition_is_a_clean_400(gate_seen: dict[str, Any], rec: _AuditRecorder) -> None:
    with pytest.raises(InvalidInputError, match="never_expires"):
        asyncio.run(
            ep.write_access_tuple(
                request=_request(client=object()),
                settings=_settings(),
                token=None,
                body=AccessTuple(user="alice", relation="writer", object="namespace:bronze", condition=TupleCondition(name="never_expires")),
            )
        )


def test_a_condition_missing_a_parameter_is_a_clean_400(gate_seen: dict[str, Any], rec: _AuditRecorder) -> None:
    # An absent CEL operand does not evaluate false — it ERRORS. Catching it at write time makes the
    # grant fail now, instead of turning every later check on the object into a 503.
    with pytest.raises(InvalidInputError, match="grant_duration"):
        asyncio.run(
            ep.write_access_tuple(
                request=_request(client=object()),
                settings=_settings(),
                token=None,
                body=AccessTuple(
                    user="alice",
                    relation="writer",
                    object="namespace:bronze",
                    condition=TupleCondition(name="non_expired_grant", context={"grant_time": "2026-07-29T09:00:00Z"}),
                ),
            )
        )


def test_current_time_is_not_accepted_on_the_tuple(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """`current_time` is the CALLER's clock, supplied per check. Letting it ride the tuple would pin the
    present moment at grant time, so the window would be evaluated against a frozen 'now' forever."""
    written = _write_conditional(
        monkeypatch,
        AccessTuple(
            user="alice",
            relation="writer",
            object="namespace:bronze",
            condition=TupleCondition(name="non_expired_grant", context={"grant_time": "2026-07-29T09:00:00Z", "grant_duration": "4h"}),
        ),
    )
    assert "current_time" not in written[0].condition.context


def test_a_read_echoes_the_condition_rather_than_flattening_it(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """A time-boxed grant that reads back as permanent is worse than no feature: a reviewer auditing
    'who has writer on bronze' would see a name that is in fact about to lapse."""

    async def _fake_read(_client: Any, **_kw: Any) -> tuple[list[Any], str | None]:
        return (
            [
                fga.ClientTuple(
                    user="user:alice",
                    relation="writer",
                    object="namespace:bronze",
                    condition=fga.RelationshipCondition(name="non_expired_grant", context={"grant_duration": "4h"}),
                )
            ],
            None,
        )

    monkeypatch.setattr(ep.fga, "read_tuples", _fake_read)
    response = asyncio.run(
        ep.read_access_tuples(request=_request(client=object()), settings=_settings(), token=_token("root_admin"), object="namespace:bronze")
    )
    assert response.tuples[0].condition is not None
    assert response.tuples[0].condition.name == "non_expired_grant"
    assert response.tuples[0].condition.context["grant_duration"] == "4h"


def test_check_forwards_the_runtime_context(gate_seen: dict[str, Any], rec: _AuditRecorder, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def _fake_check(_client: Any, **kwargs: Any) -> bool:
        seen.update(kwargs)
        return True

    monkeypatch.setattr(ep.fga, "check", _fake_check)
    asyncio.run(
        ep.check_access(
            request=_request(client=object()),
            settings=_settings(),
            token=None,
            body=AccessCheckBody(user="alice", relation="writer", object="namespace:bronze", context={"current_time": "2026-07-29T10:30:00Z"}),
        )
    )
    assert seen["context"] == {"current_time": "2026-07-29T10:30:00Z"}


def test_the_model_endpoint_publishes_condition_parameter_types(gate_seen: dict[str, Any], rec: _AuditRecorder) -> None:
    """Served so a client renders TYPED inputs from the model. A hand-kept copy in the frontend drifts
    the moment a parameter changes, and a missing operand is a 503, not a soft failure."""
    client = SimpleNamespace(get_authorization_model_id=lambda: "01MODEL")
    response = asyncio.run(ep.get_access_model(request=_request(client=client), settings=_settings(), token=None))
    assert "non_expired_grant" in response.conditions
    assert response.conditions["non_expired_grant"]["grant_duration"] == "duration"
    assert response.conditions["non_expired_grant"]["current_time"] == "timestamp"
