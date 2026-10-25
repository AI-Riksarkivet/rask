"""P6 — the tenant membership API (``/management/v1/projects/{id}/members``).

The gap: ``project#admin``/``member`` were writable only by ``seed_project_admin`` at create time and
by the estate-gated raw tuple editor, so a project admin could not invite anyone into their own
tenant. These pin how the grant door writes and how the door lists. The property that makes
``can_grant_admin: admin`` safe to ship, that a project always keeps a direct admin, is driven against a real
OpenFGA in ``services/catalog/tests/test_a_project_always_keeps_a_direct_admin.py``.

Sync via ``asyncio.run`` — no async-plugin dependency (this repo does not set ``asyncio_mode``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from catalog.api.v1.endpoints import members
from catalog.core.config import Settings
from service_kit.control_emit import NoopControlEmitter
from service_kit.exceptions import ForbiddenError
from service_kit.governed.oidc import IDToken
from service_kit.governed.person_identity import PersonPrincipal
from service_kit.governed.principal import Principal


def _tuple(user: str, relation: str, obj: str) -> SimpleNamespace:
    return SimpleNamespace(user=user, relation=relation, object=obj)


class _Harness:
    """Answers the gate and records the writes the endpoint issues."""

    def __init__(self, existing: list[SimpleNamespace] | None = None, *, allow: bool = True) -> None:
        self.existing = existing if existing is not None else []
        self.allow = allow
        self.written: list[Any] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from catalog.api import fga_deps

        async def fake_require(_c: object, _s: object, _t: object, *, relation: str, obj: str) -> None:
            if not self.allow:
                raise ForbiddenError(f"{relation} required on {obj}")

        async def fake_read(_c: object, _obj: str, **_kw: object) -> list[SimpleNamespace]:
            return list(self.existing)

        async def fake_write(_c: object, tuples: list[Any], **_kw: object) -> None:
            self.written.extend(tuples)
            self.existing.extend(_tuple(t.user, t.relation, t.object) for t in tuples)

        monkeypatch.setattr(fga_deps, "require_relation", fake_require)
        monkeypatch.setattr(members.fga, "read_object_tuples", fake_read)
        monkeypatch.setattr(members.fga, "write_tuples", fake_write)


def _client() -> Any:
    """The injected OpenFGA client (catalog-api-09): the doors take it as a dependency now rather than
    reaching into ``request.app.state``. Loosely typed — every test that needs behaviour monkeypatches
    ``members.fga.*``."""
    return object()


def _control() -> Any:
    """The injected control emitter, announcing into the void."""
    return NoopControlEmitter()


def _settings(*, fga_enabled: bool = True) -> Settings:
    return cast(Settings, SimpleNamespace(fga_enabled=fga_enabled))


def _token() -> PersonPrincipal:
    return PersonPrincipal(principal=Principal(idp_id="dex", subject="alice"), claims=IDToken(iss="i", sub="alice", aud="lance", exp=1, iat=1))


def _grant(h: _Harness, user: str, relation: str) -> members.MemberList:
    body = members.GrantRequest(user=user, relation=cast(Any, relation))
    return asyncio.run(members.grant_member("acme", body, _client(), _control(), _settings(), _token()))


# --------------------------------------------------------------------------- #
# The gate, on the project object
# --------------------------------------------------------------------------- #


def test_a_denied_gate_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(allow=False)
    h.install(monkeypatch)
    with pytest.raises(ForbiddenError):
        _grant(h, "dex~bob", "admin")
    assert h.written == []


# --------------------------------------------------------------------------- #
# Grant
# --------------------------------------------------------------------------- #


def test_grant_writes_the_tuple_and_normalises_a_bare_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness()
    h.install(monkeypatch)
    out = _grant(h, "dex~bob", "member")
    assert len(h.written) == 1
    assert (h.written[0].user, h.written[0].relation, h.written[0].object) == ("user:dex~bob", "member", "project:acme")
    assert members.Member(user="user:dex~bob", relation="member") in out.members


def test_a_qualified_userset_passes_through_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """`role:x#assignee` must NOT become `user:role:x#assignee` — that subject matches no tuple."""
    h = _Harness()
    h.install(monkeypatch)
    _grant(h, "role:curators#assignee", "member")
    assert h.written[0].user == "role:curators#assignee"


def test_regranting_what_someone_already_holds_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not an optimisation: an OpenFGA Write is transactional and REJECTS an existing tuple, so without
    the pre-read the second call would 400."""
    h = _Harness([_tuple("user:dex~bob", "member", "project:acme")])
    h.install(monkeypatch)
    _grant(h, "dex~bob", "member")
    assert h.written == []


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #


def test_the_team_edge_is_never_listed_as_a_member(monkeypatch: pytest.MonkeyPatch) -> None:
    """`team` makes every member of a team an admin (`admin: … or member from team`). Listing it as a
    row would invite someone to revoke the thing that makes the tenant resolvable at all."""
    h = _Harness(
        [
            _tuple("user:dex~alice", "admin", "project:acme"),
            _tuple("team:eng", "team", "project:acme"),
        ]
    )
    h.install(monkeypatch)
    out = asyncio.run(members.list_members("acme", _client(), _settings(), _token()))
    assert [m.relation for m in out.members] == ["admin"]
