"""``_authorize_grant`` — the per-rung gate on grant/revoke.

Granting used to clear the owner bar (``access/grant`` mapped to ``can_drop``/``can_delete``), which
welded handing out access to owning the data. The rung being handed out lives in the BODY, so a
path-suffix gate cannot express "may grant reader" — this dependency reads the body, exactly as
``_authorize_batch`` already does for the batch routes.

It runs BEFORE Pydantic on client-controlled JSON, so every malformed shape must be a 403 rather than
a 500 — and, more importantly, a rung with no ``can_grant_*`` on the type must be refused HERE rather
than reaching OpenFGA, where an undefined relation is a 400 that fails closed to a 503 for every
caller: an outage wearing a permission error's clothes.

Sync via ``asyncio.run`` — no async-plugin dependency (this repo does not set ``asyncio_mode``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from catalog.api import fga_deps
from catalog.core.config import Settings
from lance_namespace import PermissionDeniedError


def _run(
    monkeypatch: pytest.MonkeyPatch,
    body: Any,
    *,
    fga_type: str = "namespace",
    segments: list[str] | None = None,
    allow: bool = True,
    revoking: bool = False,
) -> list[tuple[str, str, str]]:
    """Drive the gate against a fake Check. Returns every (user, relation, object) it asked about —
    an empty list proves the gate refused WITHOUT consulting OpenFGA."""
    asked: list[tuple[str, str, str]] = []

    async def fake_check(_client: object, *, user: str, relation: str, obj: str, **_kw: Any) -> bool:
        asked.append((user, relation, obj))
        return allow

    monkeypatch.setattr(fga_deps.fga, "check", fake_check)
    request = cast(fga_deps.Request, SimpleNamespace(json=lambda: _as_coro(body)))
    settings = cast(Settings, SimpleNamespace(delimiter="$"))
    asyncio.run(
        fga_deps._authorize_grant(
            request,
            cast(Any, object()),
            settings,
            user="alice",
            fga_type=fga_type,
            segments=segments if segments is not None else ["gold"],
            revoking=revoking,
        )
    )
    return asked


async def _as_coro(value: Any) -> Any:
    return value


def test_gates_on_the_rung_in_the_body_not_a_blanket_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _run(monkeypatch, {"user": "bob", "relation": "reader"})
    assert asked == [("alice", "can_grant_reader", "namespace:gold")]


def test_each_rung_maps_to_its_own_action(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the split: granting `owner` and granting `reader` are different privileges."""
    for rung in ("owner", "writer", "reader", "validator", "manage_grants", "pass_grants"):
        asked = _run(monkeypatch, {"user": "bob", "relation": rung})
        assert asked == [("alice", f"can_grant_{rung}", "namespace:gold")]


def test_the_table_surface_gates_on_the_table_object(monkeypatch: pytest.MonkeyPatch) -> None:
    asked = _run(monkeypatch, {"user": "bob", "relation": "writer"}, fga_type="table", segments=["gold", "catalog"])
    assert asked == [("alice", "can_grant_writer", "table:gold$catalog")]


def test_a_denied_check_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(PermissionDeniedError):
        _run(monkeypatch, {"user": "bob", "relation": "owner"}, allow=False)


# --------------------------------------------------------------------------- #
# Fail-closed on client-controlled input — each of these must deny WITHOUT a Check
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        pytest.param([], id="list-body"),
        pytest.param("reader", id="string-body"),
        pytest.param(None, id="null-body"),
    ],
)
def test_a_non_dict_body_is_denied_not_crashed(monkeypatch: pytest.MonkeyPatch, body: Any) -> None:
    with pytest.raises(PermissionDeniedError):
        _run(monkeypatch, body)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"user": "bob"}, id="missing-relation"),
        pytest.param({"user": "bob", "relation": 7}, id="non-string-relation"),
        pytest.param({"user": "bob", "relation": ""}, id="empty-relation"),
        pytest.param({"user": "bob", "relation": None}, id="null-relation"),
    ],
)
def test_a_malformed_relation_is_denied(monkeypatch: pytest.MonkeyPatch, body: Any) -> None:
    with pytest.raises(PermissionDeniedError):
        _run(monkeypatch, body)


@pytest.mark.parametrize(
    "relation",
    [
        pytest.param("parent", id="structural-edge"),
        pytest.param("child", id="inverse-edge"),
        pytest.param("can_read_data", id="derived-action"),
        pytest.param("admin", id="rung-of-another-type"),
        pytest.param("nonsense", id="unknown"),
    ],
)
def test_a_rung_with_no_grant_action_is_refused_before_reaching_openfga(monkeypatch: pytest.MonkeyPatch, relation: str) -> None:
    """The phantom-relation guard. Passing these through would build ``can_grant_parent`` and ask
    OpenFGA for a relation the model does not define — a 400 that fails closed to a 503 for EVERY
    caller, owners included. The assertion that matters is the empty ask-list: refused locally."""
    asked: list[tuple[str, str, str]] = []

    async def fake_check(_c: object, *, user: str, relation: str, obj: str, **_kw: Any) -> bool:
        asked.append((user, relation, obj))
        return True

    monkeypatch.setattr(fga_deps.fga, "check", fake_check)
    request = cast(fga_deps.Request, SimpleNamespace(json=lambda: _as_coro({"user": "bob", "relation": relation})))
    settings = cast(Settings, SimpleNamespace(delimiter="$"))
    with pytest.raises(PermissionDeniedError):
        asyncio.run(fga_deps._authorize_grant(request, cast(Any, object()), settings, user="alice", fga_type="namespace", segments=["gold"], revoking=False))
    assert asked == [], f"{relation!r} reached OpenFGA as a phantom relation"


def test_the_grant_actions_come_from_the_compiled_model() -> None:
    """Never a hand-kept list — a renamed rung must drop out here the same turn it changes in the DSL."""
    for fga_type in ("warehouse", "namespace", "table"):
        actions = fga_deps._grant_actions(fga_type)
        assert actions, f"no can_grant_* enumerated for {fga_type}"
        assert all(a.startswith("can_grant_") for a in actions)
        # Every rung the grant API will accept must have a gate, or granting it would be unreachable.
        for rung in ("owner", "writer", "reader", "validator", "manage_grants", "pass_grants"):
            assert f"can_grant_{rung}" in actions, f"{fga_type} can grant {rung} with no gate"


# --------------------------------------------------------------------------- #
# REVOKE is not the mirror of GRANT
# --------------------------------------------------------------------------- #


def test_revoke_is_gated_on_manage_grants_not_on_the_rung(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE REGRESSION. `access/revoke` used to run through `can_grant_<rung>`, which is
    `manage_grants or (<rung> and pass_grants)` — so a delegate holding `writer` + `pass_grants` and
    nothing else could DELETE any reader/writer/validator tuple on the object, including ones the
    owner issued. `can_revoke_grant` is `manage_grants` alone."""
    asked = _run(monkeypatch, {"user": "carol", "relation": "reader"}, revoking=True)

    assert asked == [("alice", "can_revoke_grant", "namespace:gold")]


@pytest.mark.parametrize("rung", ["owner", "writer", "reader", "validator", "manage_grants", "pass_grants"])
def test_every_rung_revokes_through_the_same_single_door(rung: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """One authority for the whole revoke surface. A per-rung revoke bar is what let the delegate in:
    the rung says WHAT is being taken away, never WHO may take it."""
    asked = _run(monkeypatch, {"user": "carol", "relation": rung}, revoking=True)

    assert [relation for _u, relation, _o in asked] == ["can_revoke_grant"]


def test_revoke_still_refuses_a_phantom_rung_before_reaching_openfga(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shape check survives the authority change. A rung the model cannot grant is not a rung the
    API may be asked to revoke, and it must be refused HERE — an undefined relation reaching OpenFGA
    is a 400 that fails closed to a 503 for every caller."""
    asked: list[tuple[str, str, str]] = []

    async def fake_check(_client: object, *, user: str, relation: str, obj: str, **_kw: Any) -> bool:
        asked.append((user, relation, obj))
        return True

    monkeypatch.setattr(fga_deps.fga, "check", fake_check)
    request = cast(fga_deps.Request, SimpleNamespace(json=lambda: _as_coro({"user": "bob", "relation": "parent"})))
    settings = cast(Settings, SimpleNamespace(delimiter="$"))

    with pytest.raises(PermissionDeniedError):
        asyncio.run(fga_deps._authorize_grant(request, cast(Any, object()), settings, user="alice", fga_type="namespace", segments=["gold"], revoking=True))

    assert asked == []


def test_can_revoke_grant_exists_on_every_type_the_grant_api_serves() -> None:
    """The gate names this relation directly rather than deriving it, so a model that lost it would
    fail closed to a 503 on every revoke — an outage wearing a permission error's clothes, which is
    the exact failure `_grant_actions` was written to prevent for the grant half."""
    from service_kit.governed import fga as fga_lib

    compiled = {td["type"]: set((td.get("relations") or {}).keys()) for td in fga_lib.load_model()["type_definitions"]}

    for fga_type in ("warehouse", "namespace", "table"):
        assert "can_revoke_grant" in compiled[fga_type], f"{fga_type} has no can_revoke_grant — every revoke on it 503s"


# --------------------------------------------------------------------------- #
# A grant may not raise the CALLER's own access
# --------------------------------------------------------------------------- #


def test_a_manage_grants_holder_cannot_grant_themselves_a_rung(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE ESCALATION. `can_grant_owner` is `manage_grants`, so the C2 headline persona —
    "administers access, cannot read the data" — was one authorized call away from `can_read_data`,
    leaving an audit row identical to a legitimate delegation."""
    checks: list[tuple[str, str]] = []

    async def fake_check(_client: object, *, user: str, relation: str, obj: str, **_kw: Any) -> bool:
        checks.append((relation, user))
        return relation.startswith("can_grant_")  # may grant owner; does NOT hold owner

    monkeypatch.setattr(fga_deps.fga, "check", fake_check)
    request = cast(fga_deps.Request, SimpleNamespace(json=lambda: _as_coro({"user": "judy", "relation": "owner"})))
    settings = cast(Settings, SimpleNamespace(delimiter="$"))

    with pytest.raises(PermissionDeniedError, match="may not raise your own access"):
        asyncio.run(fga_deps._authorize_grant(request, cast(Any, object()), settings, user="judy", fga_type="namespace", segments=["gold"], revoking=False))

    assert ("owner", "user:judy") in checks, "the guard must ask whether the caller ALREADY holds the rung"


def test_granting_someone_else_is_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """The capability C2 exists for. An access admin assigning an owner is the normal case and must
    not cost an extra round-trip either."""
    asked = _run(monkeypatch, {"user": "bob", "relation": "owner"})

    # ONE check — the `can_grant_owner` gate. The guard short-circuits before asking anything when
    # the grantee is plainly somebody else, so the ordinary path is not slowed by the rule.
    assert asked == [("alice", "can_grant_owner", "namespace:gold")]


def test_re_granting_yourself_what_you_already_hold_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rule is about ELEVATION, not about self-reference. An owner re-granting themselves `owner`
    changes nothing, and refusing it would break an idempotent re-seed."""

    async def fake_check(_client: object, *, user: str, relation: str, obj: str, **_kw: Any) -> bool:
        return True  # may grant it, and already holds it

    monkeypatch.setattr(fga_deps.fga, "check", fake_check)
    request = cast(fga_deps.Request, SimpleNamespace(json=lambda: _as_coro({"user": "alice", "relation": "owner"})))
    settings = cast(Settings, SimpleNamespace(delimiter="$"))

    asyncio.run(fga_deps._authorize_grant(request, cast(Any, object()), settings, user="alice", fga_type="namespace", segments=["gold"], revoking=False))


def test_the_userset_indirection_is_closed_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refusing only a literal `user:<sub>` would leave the same escalation one indirection away:
    judy grants `owner` to `role:validators#assignee`, a userset she is already inside."""
    checks: list[tuple[str, str, str]] = []

    async def fake_check(_client: object, *, user: str, relation: str, obj: str, **_kw: Any) -> bool:
        checks.append((user, relation, obj))
        if relation == "assignee":
            return True  # the caller IS inside the userset being granted
        return relation.startswith("can_grant_")  # may grant; does not hold the rung

    monkeypatch.setattr(fga_deps.fga, "check", fake_check)
    request = cast(fga_deps.Request, SimpleNamespace(json=lambda: _as_coro({"user": "role:validators#assignee", "relation": "owner"})))
    settings = cast(Settings, SimpleNamespace(delimiter="$"))

    with pytest.raises(PermissionDeniedError, match="may not raise your own access"):
        asyncio.run(fga_deps._authorize_grant(request, cast(Any, object()), settings, user="judy", fga_type="namespace", segments=["gold"], revoking=False))

    assert ("user:judy", "assignee", "role:validators") in checks, "membership of the granted userset was never checked"


def test_a_userset_the_caller_is_outside_is_a_normal_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard must not turn every userset grant into a refusal — that would break the role-based
    delegation `role:validators#assignee` exists for."""

    async def fake_check(_client: object, *, user: str, relation: str, obj: str, **_kw: Any) -> bool:
        if relation == "assignee":
            return False  # the caller is NOT inside it
        return relation.startswith("can_grant_")

    monkeypatch.setattr(fga_deps.fga, "check", fake_check)
    request = cast(fga_deps.Request, SimpleNamespace(json=lambda: _as_coro({"user": "role:validators#assignee", "relation": "owner"})))
    settings = cast(Settings, SimpleNamespace(delimiter="$"))

    asyncio.run(fga_deps._authorize_grant(request, cast(Any, object()), settings, user="judy", fga_type="namespace", segments=["gold"], revoking=False))


def test_the_self_grant_rule_never_runs_on_revoke(monkeypatch: pytest.MonkeyPatch) -> None:
    """Revoking your OWN rung is a de-escalation. Applying the elevation rule there would refuse a
    grant-manager dropping their own access, which is the one self-directed act that is always safe."""
    asked = _run(monkeypatch, {"user": "alice", "relation": "owner"}, revoking=True)

    assert asked == [("alice", "can_revoke_grant", "namespace:gold")], "revoke consulted the self-grant guard"
