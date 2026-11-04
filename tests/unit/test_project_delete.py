"""Unit tests for ``DELETE /management/v1/projects/{id}`` (endpoints/projects.py) — retiring a tenant.

Drives the handler directly (no TestClient) with fake registry records + settings, the repo's direct-call
style (``test_projects_endpoint.py``), handing it the record its gate (``administered_project``) resolved. Pins the
lifecycle contract (`open_hierarchy_lifecycle.md` Decision 3): deletes are bottom-up, so a project holding warehouses
refuses 409 and NAMES them. The gate and deletion protection are pinned over HTTP on a real OpenFGA
(`services/catalog/tests/test_a_projects_protection_is_set_and_cleared_by_its_admins.py` and
`test_a_door_the_router_leaves_open_decides_for_itself.py`).

One invariant is pinned from the failed side: an OpenFGA outage during the revoke aborts the whole delete rather than
leaving a record-less-or-tuple-less half state behind — the reason the revoke is ordered first.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from lance_namespace import NamespaceNotEmptyError
from pydantic import SecretStr

from catalog.api.v1.endpoints import projects as ep
from catalog.core.config import Settings
from service_kit.governed.idp_identity import PersonPrincipal
from service_kit.governed.oidc import IDToken
from service_kit.governed.principal import Principal


class _Emitter:
    """An in-memory ``ControlEmitter`` — records what the handler announced."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


def _settings(*, fga_enabled: bool = False) -> Settings:
    """The catalog's settings with authorization on or off, which decides whether the revoke runs at all; the registry
    it names is never reached, since every test patches the registry's reads and writes."""
    return Settings(
        fga_enabled=fga_enabled,
        oidc_enabled=fga_enabled,
        oidc_issuer="https://issuer.invalid",
        oidc_audience="lance",
        oidc_connectors={"dex": "dex"},
        control_root="file:///unused",
        s3_access_key_id="test",
        s3_secret_access_key=SecretStr("test"),
    )


_TOKEN = PersonPrincipal(principal=Principal(idp_id="dex", subject="alice"), claims=IDToken(iss="i", sub="alice", aud="lance", exp=1, iat=1))

#: A live tenant record, as ``POST /management/v1/projects`` writes it.
_RECORD: dict[str, str] = {"id": "acme", "created_at": "2026-08-04T00:00:00+00:00", "created_by": "dex~alice"}

#: One warehouse belonging to ANOTHER tenant — present in every world, so "empty" is proven to mean
#: "holds no warehouse OF ITS OWN", not "the estate holds no warehouses".
_OTHER_WAREHOUSE: dict[str, str] = {"id": "wh-g", "bucket": "bkt-g", "root_uri": "s3://bkt-g", "project": "globex"}


def _world(
    monkeypatch: pytest.MonkeyPatch,
    *,
    warehouse_records: list[dict[str, str]] | None = None,
    tuples: list[Any] | None = None,
    revoke_error: Exception | None = None,
) -> Any:
    """Patch the registry + FGA seams the delete touches, recording WHAT it did and IN WHICH ORDER."""
    calls: list[str] = []
    deleted: list[str] = []
    revoked: list[dict[str, Any]] = []

    monkeypatch.setattr(ep.warehouses, "list_warehouses", lambda _root, _so: [_OTHER_WAREHOUSE, *(warehouse_records or [])])

    def _delete_record(_root: str, _so: Any, project_id: str) -> None:
        calls.append("delete_record")
        deleted.append(project_id)

    async def _revoke(_client: Any, obj: str, *, actor: str, origin: str, **_kw: Any) -> list[Any]:
        calls.append("revoke")
        revoked.append({"obj": obj, "actor": actor, "origin": origin})
        if revoke_error is not None:
            raise revoke_error
        return list(tuples or [])

    monkeypatch.setattr(ep.project_registry, "delete_project_record", _delete_record)
    monkeypatch.setattr(ep.fga, "revoke_object_tuples", _revoke)
    return SimpleNamespace(calls=calls, deleted=deleted, revoked=revoked)


def _grant(user: str, relation: str, obj: str = "project:acme") -> Any:
    """One revoked tuple, in the shape `revoke_object_tuples` now hands back."""
    return SimpleNamespace(user=user, relation=relation, object=obj)


def _delete(
    project_id: str,
    settings: Settings,
    *,
    token: Any = _TOKEN,
    client: Any = None,
    control: Any = None,
) -> ep.DeleteProjectResponse:
    return asyncio.run(ep.delete_project(project_id, record=_RECORD, settings=settings, token=token, client=client, control=control or _Emitter()))


# ── the refusal: a tenant's contents ─────────────────────────────────────────────────────────────────


def test_project_holding_warehouses_refuses_409_naming_every_one(monkeypatch: pytest.MonkeyPatch) -> None:
    # A refusal that does not say what blocks it just moves the search to the user: the 409 lists the
    # blocking warehouses AND the route that empties them, one rung at a time.
    world = _world(
        monkeypatch,
        warehouse_records=[
            {"id": "wh-b", "bucket": "bkt-b", "root_uri": "s3://bkt-b", "project": "acme"},
            {"id": "wh-a", "bucket": "bkt-a", "root_uri": "s3://bkt-a", "project": "acme"},
        ],
    )
    with pytest.raises(NamespaceNotEmptyError) as excinfo:
        _delete("acme", _settings())
    message = str(excinfo.value)
    assert "wh-a" in message and "wh-b" in message
    assert "wh-g" not in message  # another tenant's warehouse neither blocks nor leaks into the refusal
    assert "DELETE /management/v1/warehouses/" in message
    assert world.calls == []  # a refusal removes NOTHING — not a tuple, not the record


# ── the happy path: tuples revoked, record gone, event announced ──────────────────────────────────────


def test_empty_project_is_deleted_and_its_tuples_revoked(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(monkeypatch, tuples=[_grant("user:a", "admin"), _grant("user:b", "member"), _grant("role:r#assignee", "member")])
    control = _Emitter()
    result = _delete("acme", _settings(fga_enabled=True), client=object(), control=control)

    assert result == ep.DeleteProjectResponse(project="acme", tuples_revoked=3)
    assert world.deleted == ["acme"]
    # Revoked on the PROJECT object (the admin/member tuples), stamped as a lifecycle delete so
    # the tuple-level audit rows say why the grants went away.
    assert world.revoked == [{"obj": "project:acme", "actor": "dex~alice", "origin": "lifecycle_delete"}]
    event = control.events[0]
    assert (event.action, event.object_type, event.object_id, event.actor) == ("project_deleted", "project", "project:acme", "user:dex~alice")


def test_fga_off_still_deletes_the_record_and_reports_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(
        monkeypatch,
        tuples=[
            _grant("user:u0", "member"),
            _grant("user:u1", "member"),
            _grant("user:u2", "member"),
            _grant("user:u3", "member"),
            _grant("user:u4", "member"),
            _grant("user:u5", "member"),
            _grant("user:u6", "member"),
        ],
    )
    result = _delete("acme", _settings(fga_enabled=False))
    assert result.tuples_revoked == 0  # a fact about an auth-off stack, never a fabricated success
    assert world.calls == ["delete_record"]
