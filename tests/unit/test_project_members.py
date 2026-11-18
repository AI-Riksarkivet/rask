"""P6 — the tenant membership API (``/management/v1/projects/{id}/members``).

The gap: ``project#admin``/``member`` were writable only by ``seed_project_admin`` at create time and
by the estate-gated raw tuple editor, so a project admin could not invite anyone into their own
tenant. These pin how the grant door writes and how the door lists. The property that makes
``can_grant_admin: admin`` safe to ship, that a project always keeps an administrator, is driven against a real
OpenFGA in ``services/catalog/tests/test_a_project_always_keeps_an_administrator.py``.

Sync via ``asyncio.run`` — no async-plugin dependency (this repo does not set ``asyncio_mode``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from unfiled_announcer import unfiled

from catalog.api.v1.endpoints import members
from catalog.core.config import Settings
from lakehouse.control_emit import NoopControlEmitter
from lakehouse.exceptions import ForbiddenError
from lakehouse.governed.idp_identity import PersonPrincipal
from lakehouse.governed.oidc import IDToken
from lakehouse.governed.principal import Principal


def _tuple(user: str, relation: str, obj: str) -> SimpleNamespace:
    return SimpleNamespace(user=user, relation=relation, object=obj)


#: The project's live creation as OpenFGA names it (ADR 0245).
ACME = f"project:{'a' * 32}"


class _Harness:
    """Answers the gate and records the writes the endpoint issues."""

    def __init__(self, existing: list[SimpleNamespace] | None = None, *, allow: bool = True) -> None:
        self.existing = list(existing or [])
        self.allow = allow
        self.written: list[Any] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from catalog.api import fga_deps

        acme = members.tenancy.Tenant(kind="project", id="acme", obj=ACME, record={"id": "acme", "uuid": "a" * 32, "state": "live"})

        async def fake_resolve(_s: object, _kind: str, _id: str, **_kw: object) -> members.tenancy.Tenant:
            return acme

        async def fake_still(_s: object, _tenant: object) -> None:
            return None

        async def fake_require(_c: object, _s: object, _t: object, *, relation: str, obj: str, label: str | None = None) -> None:
            if not self.allow:
                raise ForbiddenError(f"{relation} required on {label or obj}")

        async def fake_read(_c: object, _obj: str, **_kw: object) -> list[SimpleNamespace]:
            return list(self.existing)

        async def fake_holds(_c: object, t: Any) -> bool:  # noqa: ANN401 — a ClientTuple
            return any((e.user, e.relation, e.object) == (t.user, t.relation, t.object) for e in self.existing)

        async def fake_write(_c: object, t: Any, **_kw: object) -> bool:  # noqa: ANN401 — a ClientTuple
            self.written.append(t)
            self.existing.append(_tuple(t.user, t.relation, t.object))
            return True

        monkeypatch.setattr(members.tenancy, "resolved", fake_resolve)
        monkeypatch.setattr(members.tenancy, "require_still", fake_still)
        monkeypatch.setattr(fga_deps, "require_relation", fake_require)
        monkeypatch.setattr(members.fga, "read_object_tuples", fake_read)
        monkeypatch.setattr(members.fga, "holds", fake_holds)
        monkeypatch.setattr(members.project_admins, "confer", fake_write)
        monkeypatch.setattr(members.project_admins, "write_absent", fake_write)


def _client() -> Any:
    """The injected OpenFGA client (catalog-api-09): the doors take it as a dependency now rather than
    reaching into ``request.app.state``. Loosely typed — every test that needs behaviour monkeypatches
    ``members.fga.*``."""
    return object()


def _control() -> Any:
    """The injected announcer, announcing into the void."""
    return unfiled(NoopControlEmitter())


def _settings(*, fga_enabled: bool = True) -> Settings:
    return cast(Settings, SimpleNamespace(fga_enabled=fga_enabled, fga_default_warehouse_object="warehouse:lance_catalog"))


def _token() -> PersonPrincipal:
    return PersonPrincipal(principal=Principal(idp_id="dex", subject="alice"), claims=IDToken(iss="i", sub="alice", aud="lance", exp=1, iat=1))


def _grant(h: _Harness, user: str, relation: str) -> members.MemberList:
    body = members.GrantRequest(user=user, relation=cast(Any, relation))
    return asyncio.run(members.grant_member("acme", body, _client(), _control(), _settings(), _token()))


def test_a_denied_gate_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(allow=False)
    h.install(monkeypatch)
    with pytest.raises(ForbiddenError):
        _grant(h, "dex~bob", "admin")
    assert h.written == []


# --------------------------------------------------------------------------- #
# The gate, on the project object
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Grant
# --------------------------------------------------------------------------- #


def test_grant_writes_the_tuple_and_normalises_a_bare_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness()
    h.install(monkeypatch)
    out = _grant(h, "dex~bob", "member")
    assert len(h.written) == 1
    assert (h.written[0].user, h.written[0].relation, h.written[0].object) == ("user:dex~bob", "member", ACME)
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
    h = _Harness([_tuple("user:dex~bob", "member", ACME)])
    h.install(monkeypatch)
    _grant(h, "dex~bob", "member")
    assert h.written == []
