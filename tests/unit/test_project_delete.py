"""Unit tests for ``DELETE /v1/projects/{id}`` (endpoints/projects.py) — retiring a tenant.

Drives the handler directly (no TestClient) with fake registry records + settings, the repo's direct-call
style (``test_projects_endpoint.py``). Pins the lifecycle contract (`open_hierarchy_lifecycle.md`
Decision 3/5): deletes are bottom-up, so a project holding warehouses refuses 409 and NAMES them; the gate
is the tenant's own ``project:<id>#can_administer`` and NOT the estate-observer bar the read routes use;
deletion protection refuses 409 and ``force=true`` overrides the protection.

One invariant is pinned from the DENIED side as well, because it cannot be observed from the allowed
one: that 409 names a tenant's warehouses, so authorization is decided BEFORE it or the door becomes a
storage-enumeration oracle for callers who may not administer the tenant.
And one from the failed side: an OpenFGA outage during the revoke aborts the whole delete rather than
leaving a record-less-or-tuple-less half state behind — the reason the revoke is ordered first.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from lance_namespace import (
    InvalidInputError,
    NamespaceNotEmptyError,
    PermissionDeniedError,
    ServiceUnavailableError,
    TableNotFoundError,
)

from catalog.api.v1.endpoints import projects as ep
from service_kit.governed.audit import AUDIT_LOGGER, configure_audit


class _Emitter:
    """An in-memory ``ControlEmitter`` — records what the handler announced."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def emit(self, event: Any) -> None:
        self.events.append(event)


def _settings(*, fga_enabled: bool = False) -> Any:
    # registry_root/storage_options feed the (patched) registry reads; fga_enabled drives whether the
    # revoke path runs at all. The gate itself is patched per test, so its args are asserted, not guessed.
    return SimpleNamespace(
        fga_enabled=fga_enabled,
        fga_root_object="warehouse:lance_catalog",
        registry_root="file:///unused",
        storage_options=lambda: {},
    )


_TOKEN = SimpleNamespace(sub="alice")

#: A live tenant record, as ``POST /v1/projects`` writes it.
_RECORD: dict[str, str] = {"id": "acme", "created_at": "2026-08-04T00:00:00+00:00", "created_by": "alice", "protected": "false"}

#: One warehouse belonging to ANOTHER tenant — present in every world, so "empty" is proven to mean
#: "holds no warehouse OF ITS OWN", not "the estate holds no warehouses".
_OTHER_WAREHOUSE: dict[str, str] = {"id": "wh-g", "bucket": "bkt-g", "root_uri": "s3://bkt-g", "project": "globex"}


def _world(
    monkeypatch: pytest.MonkeyPatch,
    *,
    record: dict[str, str] | None = _RECORD,
    warehouse_records: list[dict[str, str]] | None = None,
    tuples: list[Any] | None = None,
    revoke_error: Exception | None = None,
) -> Any:
    """Patch the registry + FGA seams the delete touches, recording WHAT it did and IN WHICH ORDER."""
    calls: list[str] = []
    deleted: list[str] = []
    revoked: list[dict[str, Any]] = []

    monkeypatch.setattr(ep.project_registry, "get_project", lambda _root, _so, _pid: record)
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


def _allow_gate(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Allow every authz check, capturing the (relation, object) the handler asked for."""
    seen: list[dict[str, str]] = []

    async def _fake(_client: Any, _settings: Any, _token: Any, *, relation: str, obj: str) -> None:
        seen.append({"relation": relation, "obj": obj})

    monkeypatch.setattr(ep.fga_deps, "require_relation", _fake)
    return seen


def _deny_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(*_a: Any, **_kw: Any) -> None:
        raise PermissionDeniedError("can_administer required on project:acme")

    monkeypatch.setattr(ep.fga_deps, "require_relation", _fake)


def _delete(
    project_id: str,
    settings: Any,
    *,
    token: Any = _TOKEN,
    client: Any = None,
    force: bool = False,
    control: Any = None,
) -> ep.DeleteProjectResponse:
    return asyncio.run(ep.delete_project(project_id, settings=settings, token=token, client=client, control=control or _Emitter(), force=force))


@pytest.fixture
def audited() -> Iterator[list[logging.LogRecord]]:
    """Capture the compliance rows on the dedicated audit logger (mirrors ``test_audit.py``)."""

    class _Capture(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    handler = _Capture()
    logger = logging.getLogger(AUDIT_LOGGER)
    previous = logger.level
    logger.addHandler(handler)
    configure_audit(enabled=True)
    try:
        yield handler.records
    finally:  # restore the level this test found, so the audit stream's state never leaks between tests
        logger.removeHandler(handler)
        logger.setLevel(previous)


# ── the refusals: shape, existence, contents ─────────────────────────────────────────────────────────


def test_malformed_id_is_rejected_before_the_registry_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    def _never(*_a: Any, **_kw: Any) -> dict[str, str]:
        raise AssertionError("a malformed id must never reach the registry")

    monkeypatch.setattr(ep.project_registry, "get_project", _never)
    _allow_gate(monkeypatch)
    with pytest.raises(InvalidInputError):  # the DNS-safe id shape, the spec's code 13 → 400
        _delete("Bad_ID!", _settings())


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
    _allow_gate(monkeypatch)
    with pytest.raises(NamespaceNotEmptyError) as excinfo:
        _delete("acme", _settings())
    message = str(excinfo.value)
    assert "wh-a" in message and "wh-b" in message
    assert "wh-g" not in message  # another tenant's warehouse neither blocks nor leaks into the refusal
    assert "DELETE /v1/warehouses/" in message
    assert world.calls == []  # a refusal removes NOTHING — not a tuple, not the record


# ── the gate: the tenant's own can_administer, never the estate-observer bar ──────────────────────────


def test_gate_is_can_administer_on_the_project_not_the_estate_observer_relation(monkeypatch: pytest.MonkeyPatch) -> None:
    _world(monkeypatch)
    seen = _allow_gate(monkeypatch)
    settings = _settings()
    _delete("acme", settings)
    # Retiring a tenant is an act INSIDE it: its own admins may do it without estate-wide privilege, and an
    # estate observer (who may only WATCH) must not be able to delete a tenant they do not administer.
    assert seen == [{"relation": "can_administer", "obj": "project:acme"}]
    assert seen[0]["relation"] != "can_observe_events" and seen[0]["obj"] != settings.fga_root_object


def test_a_denied_caller_is_never_told_which_warehouses_block_the_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    # The 409 NAMES the tenant's warehouses, so it is a disclosure the refusal has to beat to: whoever may
    # not administer this project must not be able to enumerate its storage by probing the delete door.
    # Without this test the gate can be moved below the emptiness check and every other test still passes.
    #
    # The refusal is the missing-tenant 404, not a 403 — NO EXISTENCE ORACLE (audit #4, the rule
    # `_set_warehouse_status` established and both delete doors follow): a denial and an absent tenant are
    # made indistinguishable, so the door cannot be used to enumerate tenants either.
    world = _world(monkeypatch, warehouse_records=[{"id": "wh-a", "bucket": "bkt-a", "root_uri": "s3://bkt-a", "project": "acme"}])
    _deny_gate(monkeypatch)
    control = _Emitter()
    with pytest.raises(TableNotFoundError) as excinfo:
        _delete("acme", _settings(fga_enabled=True), client=object(), control=control)
    assert "wh-a" not in str(excinfo.value)
    assert world.calls == [] and control.events == []


# ── deletion protection (Decision 5) ─────────────────────────────────────────────────────────────────


def test_protected_project_refuses_409(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(monkeypatch, record={**_RECORD, "protected": "true"})
    _allow_gate(monkeypatch)
    with pytest.raises(NamespaceNotEmptyError) as excinfo:
        _delete("acme", _settings())
    assert "force=true" in str(excinfo.value)
    assert world.calls == []


def test_force_overrides_protection(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(monkeypatch, record={**_RECORD, "protected": "true"})
    _allow_gate(monkeypatch)
    result = _delete("acme", _settings(), force=True)
    assert result.project == "acme" and world.deleted == ["acme"]


# ── the happy path: tuples revoked, record gone, event announced ──────────────────────────────────────


def test_empty_project_is_deleted_and_its_tuples_revoked(monkeypatch: pytest.MonkeyPatch) -> None:
    world = _world(monkeypatch, tuples=[_grant("user:a", "admin"), _grant("user:b", "member"), _grant("team:t", "team")])
    _allow_gate(monkeypatch)
    control = _Emitter()
    result = _delete("acme", _settings(fga_enabled=True), client=object(), control=control)

    assert result == ep.DeleteProjectResponse(project="acme", tuples_revoked=3)
    assert world.deleted == ["acme"]
    # Revoked on the PROJECT object (the admin/member/team-edge tuples), stamped as a lifecycle delete so
    # the tuple-level audit rows say why the grants went away.
    assert world.revoked == [{"obj": "project:acme", "actor": "alice", "origin": "lifecycle_delete"}]
    event = control.events[0]
    assert (event.action, event.object_type, event.object_id, event.actor) == ("project_deleted", "project", "project:acme", "user:alice")


def test_an_openfga_outage_aborts_the_delete_instead_of_reporting_a_half_success(monkeypatch: pytest.MonkeyPatch, audited: list[logging.LogRecord]) -> None:
    # The reason the revoke goes first: when it fails there is nothing to half-report. The 503 propagates,
    # the record survives (so the tenant is still named, still administered, still deletable on a retry),
    # nothing is announced on the bus and no compliance row claims a revocation that never happened.
    world = _world(
        monkeypatch,
        tuples=[_grant("user:u0", "member"), _grant("user:u1", "member"), _grant("user:u2", "member"), _grant("user:u3", "member")],
        revoke_error=ServiceUnavailableError("openfga is unavailable"),
    )
    _allow_gate(monkeypatch)
    control = _Emitter()
    with pytest.raises(ServiceUnavailableError):
        _delete("acme", _settings(fga_enabled=True), client=object(), control=control)
    assert world.calls == ["revoke"] and world.deleted == []
    assert control.events == []
    assert [r for r in audited if r.__dict__.get("audit.action") == "access_revoke"] == []


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
    _allow_gate(monkeypatch)
    result = _delete("acme", _settings(fga_enabled=False))
    assert result.tuples_revoked == 0  # a fact about an auth-off stack, never a fabricated success
    assert world.calls == ["delete_record"]


# ── the compliance trail ─────────────────────────────────────────────────────────────────────────────


def test_an_unknown_tenant_and_a_forbidden_one_are_indistinguishable(monkeypatch: pytest.MonkeyPatch) -> None:
    """NO EXISTENCE ORACLE (audit #4): the two refusals must be the SAME error with the SAME message.

    Asserted as an equality between the two paths rather than as two separate "raises 404" checks,
    because that is the actual property: any difference at all — status, wording, an id echoed in one
    and not the other — turns this door into a tenant enumerator for every authenticated caller, which
    is exactly what estate-observer gating `GET /v1/projects` was for.
    """
    _world(monkeypatch, record=None)  # the tenant does not exist
    with pytest.raises(TableNotFoundError) as absent:
        _delete("acme", _settings(fga_enabled=True), client=object())

    _world(monkeypatch)  # it exists, and the caller may not administer it
    _deny_gate(monkeypatch)
    with pytest.raises(TableNotFoundError) as forbidden:
        _delete("acme", _settings(fga_enabled=True), client=object())

    assert str(absent.value) == str(forbidden.value)
