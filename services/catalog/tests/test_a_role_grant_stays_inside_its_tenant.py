"""A role from one tenant cannot be granted a rung on another tenant's namespace.

[[LH-062]]. A role name was estate-global: `role:analyst#assignee` minted in one project could be
granted on any other project's namespace, and nothing downstream could tell. The tuple is
well-formed, the rung is real, and the grantor holds the owner rung on the object they are granting —
so every layer below honours it.

`type role` now carries `define project: [project]`, which is the fact the door reads. The model
grants nothing through that edge; the refusal is the door's.

FAIL-OPEN FOR AN UNSCOPED ROLE IS THE SUBJECT, NOT AN OVERSIGHT. A role with no project edge is
estate-wide and keeps exactly the reach it has today. Refusing every role whose tenant is unknown
would have broken every existing grant on the day the edge landed — which is how a control gets
reverted rather than adopted — so the guard refuses only where the model carries the fact to refuse
on. Both halves are asserted here, because the fail-open half is the one that would rot silently.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from lance_namespace import InvalidInputError
from openfga_sdk import OpenFgaClient

from catalog.api.v1.endpoints import access
from catalog.core.config import Settings


class _Tuple:
    def __init__(self, user: str, relation: str) -> None:
        self.user = user
        self.relation = relation


class _Settings:
    registry_root = "s3://registry"

    def storage_options(self) -> dict[str, str]:
        return {}


async def _refuse(monkeypatch: pytest.MonkeyPatch, *, role_tuples: list[_Tuple], namespace_project: str | None, grantee: str = "role:analyst#assignee") -> None:
    async def _read(_client: Any, _obj: str) -> list[_Tuple]:
        return role_tuples

    monkeypatch.setattr(access.fga, "read_object_tuples", _read)
    monkeypatch.setattr(access.warehouses, "project_for_namespace", lambda *_a, **_k: namespace_project)
    await access._refuse_a_cross_tenant_role_grant(  # noqa: SLF001 — the guard is the unit
        cast(OpenFgaClient, object()), grantee, fga_type="namespace", segments=["beta_bronze"], settings=cast(Settings, _Settings())
    )


@pytest.mark.asyncio
async def test_a_role_owned_by_another_project_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE DEFECT: acme's role granted on beta's namespace."""
    with pytest.raises(InvalidInputError) as refusal:
        await _refuse(monkeypatch, role_tuples=[_Tuple("project:acme", "project")], namespace_project="beta")

    assert "acme" in str(refusal.value) and "beta" in str(refusal.value), (
        f"the refusal does not name both tenants, so the caller cannot see what disagreed: {refusal.value}"
    )


@pytest.mark.asyncio
async def test_a_role_owned_by_the_same_project_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The control: the guard must not refuse the ordinary in-tenant grant."""
    await _refuse(monkeypatch, role_tuples=[_Tuple("project:beta", "project")], namespace_project="beta")


@pytest.mark.parametrize(
    ("shape", "role_tuples", "namespace_project"),
    [
        ("an estate-wide role carrying no project", [_Tuple("user:alice", "assignee")], "beta"),
        ("a namespace whose tenant cannot be established", [_Tuple("project:acme", "project")], None),
    ],
)
@pytest.mark.asyncio
async def test_the_guard_fails_open_where_it_cannot_know(
    monkeypatch: pytest.MonkeyPatch, shape: str, role_tuples: list[_Tuple], namespace_project: str | None
) -> None:
    """Both are deliberate: refusing on absent knowledge breaks working grants and a registry blip."""
    await _refuse(monkeypatch, role_tuples=role_tuples, namespace_project=namespace_project)


@pytest.mark.asyncio
async def test_a_plain_user_grantee_is_not_touched(monkeypatch: pytest.MonkeyPatch) -> None:
    """A `user:` grantee has no tenant edge, so the guard must not read tuples for it at all."""
    called = False

    async def _read(_client: Any, _obj: str) -> list[_Tuple]:
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(access.fga, "read_object_tuples", _read)
    await access._refuse_a_cross_tenant_role_grant(  # noqa: SLF001
        cast(OpenFgaClient, object()), "user:alice", fga_type="namespace", segments=["beta_bronze"], settings=cast(Settings, _Settings())
    )

    assert not called, "the guard read FGA tuples for a plain user grantee — a read on every grant it cannot refuse"
