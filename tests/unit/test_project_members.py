"""P6 — the tenant membership API (``/v1/projects/{id}/members``).

The gap: ``project#admin``/``member`` were writable only by ``seed_project_admin`` at create time and
by the estate-gated raw tuple editor, so a project admin could not invite anyone into their own
tenant. These pin the behaviour that closes it, and — more importantly — the two properties that make
``can_grant_admin: admin`` safe to ship:

1. **Per-rung gating.** Granting ``admin`` and granting ``member`` are separate authorities
   (``can_grant_admin`` / ``can_grant_member``), not one blanket "tenant admin" bar.
2. **The last admin cannot be revoked.** Without it, admin-grantable means admin-strandable: a tenant
   with no admin can be neither administered nor deleted from inside the product.

Sync via ``asyncio.run`` — no async-plugin dependency (this repo does not set ``asyncio_mode``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from catalog.api.v1.endpoints import members
from catalog.core.config import Settings

from service_kit.exceptions import ConflictError, ForbiddenError
from service_kit.governed.oidc import IDToken


def _tuple(user: str, relation: str, obj: str) -> SimpleNamespace:
    return SimpleNamespace(user=user, relation=relation, object=obj)


class _Harness:
    """Records the gate checks, the writes and the deletes the endpoint issues."""

    def __init__(self, existing: list[SimpleNamespace] | None = None, *, allow: bool = True) -> None:
        self.existing = existing if existing is not None else []
        self.allow = allow
        self.gated: list[tuple[str, str]] = []  # (relation, object)
        self.written: list[Any] = []
        self.deleted: list[Any] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from catalog.api import fga_deps

        async def fake_require(_c: object, _s: object, _t: object, *, relation: str, obj: str) -> None:
            self.gated.append((relation, obj))
            if not self.allow:
                raise ForbiddenError(f"{relation} required on {obj}")

        async def fake_read(_c: object, _obj: str, **_kw: object) -> list[SimpleNamespace]:
            return list(self.existing)

        async def fake_write(_c: object, tuples: list[Any], **_kw: object) -> None:
            self.written.extend(tuples)
            self.existing.extend(_tuple(t.user, t.relation, t.object) for t in tuples)

        async def fake_delete(_c: object, tuples: list[Any], **_kw: object) -> None:
            self.deleted.extend(tuples)

        monkeypatch.setattr(fga_deps, "require_relation", fake_require)
        monkeypatch.setattr(members.fga, "read_object_tuples", fake_read)
        monkeypatch.setattr(members.fga, "write_tuples", fake_write)
        monkeypatch.setattr(members.fga, "delete_tuples", fake_delete)


class _RecordingControl:
    """The control bus's seat. Holds the events a door actually announced."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


def _request(control: _RecordingControl | None = None) -> Any:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(fga=object(), control_emitter=control)))


def _settings(*, fga_enabled: bool = True) -> Settings:
    return cast(Settings, SimpleNamespace(fga_enabled=fga_enabled, delimiter="$"))


def _token() -> IDToken:
    return cast(IDToken, SimpleNamespace(sub="alice"))


def _grant(h: _Harness, user: str, relation: str, control: _RecordingControl | None = None) -> members.MemberList:
    body = members.GrantRequest(user=user, relation=cast(Any, relation))
    return asyncio.run(members.grant_member("acme", body, _request(control), _settings(), _token()))


def _revoke(h: _Harness, user: str, relation: str, control: _RecordingControl | None = None) -> members.MemberList:
    body = members.GrantRequest(user=user, relation=cast(Any, relation))
    return asyncio.run(members.revoke_member("acme", body, _request(control), _settings(), _token()))


# --------------------------------------------------------------------------- #
# The gate — per rung, on the project object
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("rung", ["admin", "member"])
def test_granting_is_gated_on_that_rungs_own_action(monkeypatch: pytest.MonkeyPatch, rung: str) -> None:
    h = _Harness()
    h.install(monkeypatch)
    _grant(h, "bob", rung)
    assert h.gated == [(f"can_grant_{rung}", "project:acme")]


def test_a_denied_gate_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(allow=False)
    h.install(monkeypatch)
    with pytest.raises(ForbiddenError):
        _grant(h, "bob", "admin")
    assert h.written == []


def test_listing_is_gated_on_can_read_assignments(monkeypatch: pytest.MonkeyPatch) -> None:
    """Who belongs to a tenant names the people worth phishing — a plain member must not enumerate it."""
    h = _Harness()
    h.install(monkeypatch)
    asyncio.run(members.list_members("acme", _request(), _settings(), _token()))
    assert h.gated == [("can_read_assignments", "project:acme")]


# --------------------------------------------------------------------------- #
# Grant
# --------------------------------------------------------------------------- #


def test_grant_writes_the_tuple_and_normalises_a_bare_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness()
    h.install(monkeypatch)
    out = _grant(h, "bob", "member")
    assert len(h.written) == 1
    assert (h.written[0].user, h.written[0].relation, h.written[0].object) == ("user:bob", "member", "project:acme")
    assert members.Member(user="user:bob", relation="member") in out.members


def test_a_qualified_userset_passes_through_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """`role:x#assignee` must NOT become `user:role:x#assignee` — that subject matches no tuple."""
    h = _Harness()
    h.install(monkeypatch)
    _grant(h, "role:curators#assignee", "member")
    assert h.written[0].user == "role:curators#assignee"


def test_regranting_what_someone_already_holds_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not an optimisation: an OpenFGA Write is transactional and REJECTS an existing tuple, so without
    the pre-read the second call would 400."""
    h = _Harness([_tuple("user:bob", "member", "project:acme")])
    h.install(monkeypatch)
    _grant(h, "bob", "member")
    assert h.written == []


# --------------------------------------------------------------------------- #
# Revoke — and the invariant that makes admin-grantable safe
# --------------------------------------------------------------------------- #


def test_revoking_the_last_admin_is_refused_and_named(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lock-out guard. Without it, admin-grantable means admin-STRANDABLE: `can_administer` gates
    membership AND delete, so a tenant with no admin can be neither administered nor retired from
    inside the product — recoverable only by estate intervention."""
    h = _Harness([_tuple("user:alice", "admin", "project:acme")])
    h.install(monkeypatch)
    with pytest.raises(ConflictError, match="only remaining admin"):
        _revoke(h, "alice", "admin")
    assert h.deleted == []


def test_revoking_an_admin_is_allowed_while_another_remains(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness([_tuple("user:alice", "admin", "project:acme"), _tuple("user:bob", "admin", "project:acme")])
    h.install(monkeypatch)
    _revoke(h, "bob", "admin")
    assert len(h.deleted) == 1
    assert h.deleted[0].user == "user:bob"


def test_revoking_the_last_MEMBER_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only `admin` is administrative. Stranding is about who can GRANT, not about who is present."""
    h = _Harness([_tuple("user:alice", "admin", "project:acme"), _tuple("user:gina", "member", "project:acme")])
    h.install(monkeypatch)
    _revoke(h, "gina", "member")
    assert len(h.deleted) == 1


def test_revoking_something_absent_is_a_noop_not_a_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """The caller asked for a state and that state already holds."""
    h = _Harness([_tuple("user:alice", "admin", "project:acme")])
    h.install(monkeypatch)
    out = _revoke(h, "nobody", "member")
    assert h.deleted == []
    assert members.Member(user="user:alice", relation="admin") in out.members


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #


def test_the_team_edge_is_never_listed_as_a_member(monkeypatch: pytest.MonkeyPatch) -> None:
    """`team` makes every member of a team an admin (`admin: … or member from team`). Listing it as a
    row would invite someone to revoke the thing that makes the tenant resolvable at all."""
    h = _Harness(
        [
            _tuple("user:alice", "admin", "project:acme"),
            _tuple("team:eng", "team", "project:acme"),
        ]
    )
    h.install(monkeypatch)
    out = asyncio.run(members.list_members("acme", _request(), _settings(), _token()))
    assert [m.relation for m in out.members] == ["admin"]


def test_team_is_not_grantable_through_this_api() -> None:
    """One request must not confer admin on a set of people the caller cannot enumerate."""
    assert "team" not in members.GRANTABLE
    with pytest.raises(ValueError, match="relation"):
        members.GrantRequest(user="eng", relation=cast(Any, "team"))


def test_the_response_advertises_the_grantable_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    """So a UI does not keep a second copy of the model's rungs."""
    h = _Harness()
    h.install(monkeypatch)
    out = _grant(h, "bob", "member")
    assert out.grantable == ["admin", "member"]


def test_authz_off_refuses_rather_than_reporting_a_grant_that_never_happened(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness()
    h.install(monkeypatch)
    body = members.GrantRequest(user="bob", relation=cast(Any, "member"))
    with pytest.raises(ConflictError, match="not configured"):
        asyncio.run(members.grant_member("acme", body, _request(), _settings(fga_enabled=False), _token()))


class TestTenantMembershipIsAnnounced:
    """Being added to or removed from a PROJECT must tell the person, exactly as a table grant does.

    The asymmetry this closes was invisible from either side. `access.py` announces every per-object
    grant it writes, so `grant_added` reaches the grantee's inbox — but the TENANT door, one rung up
    and strictly more consequential, wrote the same class of tuple and announced nothing. Being made a
    project admin arrived in silence, and being removed arrived as a 403 in the middle of work, which
    is the exact failure the control lane's docstring names as its reason to exist.

    Revocation matters more than the grant, for the reason `grant_revoked` already carries: after it
    the subject can no longer see the object, so no visibility-gated feed could ever tell them. Being
    NAMED is the targeting, which is why this lane runs no visibility check at all.
    """

    def test_a_tenant_grant_names_the_person_who_received_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        h = _Harness()
        h.install(monkeypatch)
        control = _RecordingControl()
        _grant(h, "bob", "member", control)

        assert len(control.events) == 1, "a membership grant must be announced exactly once"
        event = control.events[0]
        assert event.action == "grant_added"
        assert event.extra["subject"] == "user:bob", "the audience is the grantee, not the admin who granted"
        assert event.extra["relation"] == "member"
        assert event.object_id == "project:acme"

    def test_a_tenant_revoke_names_the_person_who_lost_access(self, monkeypatch: pytest.MonkeyPatch) -> None:
        h = _Harness()
        h.install(monkeypatch)
        _grant(h, "bob", "member")
        control = _RecordingControl()
        _revoke(h, "bob", "member", control)

        assert [(e.action, e.extra["subject"]) for e in control.events] == [("grant_revoked", "user:bob")]

    def test_a_no_op_regrant_announces_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Re-granting what someone already holds is already a no-op on the store — it must be one on
        the bus too. Announcing an unchanged state puts a row in their inbox for something that did not
        happen, which is how a bell stops being read."""
        h = _Harness()
        h.install(monkeypatch)
        _grant(h, "bob", "member")
        control = _RecordingControl()
        _grant(h, "bob", "member", control)

        assert control.events == []

    def test_a_no_op_revoke_announces_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The mirror: revoking what nobody holds changes nothing, so it tells nobody."""
        h = _Harness()
        h.install(monkeypatch)
        control = _RecordingControl()
        _revoke(h, "carol", "member", control)

        assert control.events == []
