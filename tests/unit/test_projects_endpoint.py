"""Unit tests for the derived-tenant endpoints (``GET /v1/projects[/{id}]``, endpoints/projects.py).

Drives the handlers directly (no TestClient) with fake registry records + settings, mirroring the repo's
direct-call style (``test_events_endpoint.py``). Pins the design decisions: the estate gate is
``can_observe_events`` on the fixed root object (the estate-OBSERVER action shared with ``/v1/events`` —
tenant enumeration is a whole-estate disclosure); grouping tolerates a corrupt record (skip-with-warning,
never a 500); ``admins`` degrades to ``[]`` when FGA is off or the ``list_users`` lookup is unavailable;
an unknown project is a 404 domain error.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from catalog.api.v1.endpoints import projects as ep
from lance_namespace import InvalidInputError, ServiceUnavailableError, TableNotFoundError


def _settings(*, fga_enabled: bool = False) -> Any:
    # fga_enabled=False makes require_relation a genuine no-op (its own guard) AND _admins_for degrade to
    # []; the gate's args are asserted separately below. registry_root/storage_options feed the (patched)
    # list_warehouses call.
    return SimpleNamespace(
        fga_enabled=fga_enabled,
        fga_root_object="warehouse:lance_catalog",
        registry_root="file:///unused",
        storage_options=lambda: {},
    )


_RECORDS: list[dict[str, str]] = [
    {"id": "wh-b", "bucket": "bkt-b", "root_uri": "s3://bkt-b", "project": "acme", "status": "deactivated"},
    {"id": "wh-a", "bucket": "bkt-a", "root_uri": "s3://bkt-a", "project": "acme"},  # no status → "active"
    {"id": "wh-g", "bucket": "bkt-g", "root_uri": "s3://bkt-g", "project": "globex", "status": "active"},
]


def _patch_records(monkeypatch: pytest.MonkeyPatch, records: list[dict[str, str]]) -> None:
    monkeypatch.setattr(ep.warehouses, "list_warehouses", lambda _root, _so: records)


def _list(settings: Any, *, token: Any = None, client: Any = None) -> list[ep.ProjectResponse]:
    return asyncio.run(ep.list_projects(settings=settings, token=token, client=client))


def _get(project_id: str, settings: Any, *, token: Any = None, client: Any = None) -> ep.ProjectResponse:
    return asyncio.run(ep.get_project(project_id, settings=settings, token=token, client=client))


# ── grouping: registry records → derived tenants ─────────────────────────────────────────────────────


def test_grouping_from_registry_records(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_records(monkeypatch, _RECORDS)
    result = _list(_settings())
    # Deterministic: projects sorted by name, warehouses by id; a status-less record reads "active".
    assert [p.project for p in result] == ["acme", "globex"]
    acme, globex = result
    assert [(w.id, w.bucket, w.status) for w in acme.warehouses] == [
        ("wh-a", "bkt-a", "active"),
        ("wh-b", "bkt-b", "deactivated"),
    ]
    assert [(w.id, w.bucket, w.status) for w in globex.warehouses] == [("wh-g", "bkt-g", "active")]
    assert acme.admins == [] and globex.admins == []  # FGA off → degraded, never fabricated


def test_corrupt_record_is_skipped_not_500(monkeypatch: pytest.MonkeyPatch) -> None:
    # A record missing its project (or id) is one tenant's corruption — it must be skipped with a warning,
    # never allowed to void the whole estate listing (the list_warehouses posture, extended here).
    corrupt: list[dict[str, str]] = [
        {"id": "wh-orphan", "bucket": "bkt-o", "root_uri": "s3://bkt-o"},  # no project
        {"bucket": "bkt-n", "project": "acme"},  # no id
        {"id": "wh-a", "bucket": "bkt-a", "root_uri": "s3://bkt-a", "project": "acme"},
    ]
    _patch_records(monkeypatch, corrupt)
    result = _list(_settings())
    assert [p.project for p in result] == ["acme"]
    assert [w.id for w in result[0].warehouses] == ["wh-a"]


def test_unknown_project_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_records(monkeypatch, _RECORDS)
    with pytest.raises(TableNotFoundError):  # the domain error the problem+json handler maps to 404
        _get("initech", _settings())


def test_malformed_project_id_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_records(monkeypatch, _RECORDS)
    with pytest.raises(InvalidInputError):  # DNS-safe id shape enforced before any lookup
        _get("Bad_ID!", _settings())


def test_get_project_returns_list_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_records(monkeypatch, _RECORDS)
    result = _get("acme", _settings())
    assert result.project == "acme"
    assert [w.id for w in result.warehouses] == ["wh-a", "wh-b"] and result.admins == []


# ── the estate gate: can_observe_events on the fixed root object, on BOTH routes ─────────────────────


@pytest.mark.parametrize("call", ["list", "get"])
def test_gate_uses_can_observe_events_on_root_object(monkeypatch: pytest.MonkeyPatch, call: str) -> None:
    seen: dict[str, Any] = {}

    async def _fake_require(_client: Any, _settings: Any, _token: Any, *, relation: str, obj: str) -> None:
        seen["relation"] = relation
        seen["obj"] = obj

    monkeypatch.setattr(ep.fga_deps, "require_relation", _fake_require)
    _patch_records(monkeypatch, _RECORDS)
    settings = _settings(fga_enabled=False)  # keep _admins_for a no-op; the gate call itself is captured
    if call == "list":
        _list(settings)
    else:
        _get("acme", settings)
    # The estate-OBSERVER action on the fixed root object — NOT a per-project can_administer: enumerating
    # every tenant is a whole-estate disclosure, so authz scope == data scope (the /v1/events #12 rule).
    assert seen == cast(Any, {"relation": "can_observe_events", "obj": "warehouse:lance_catalog"})


# ── admins: fga.list_users on project:<id>#can_administer, degrading — never a 500 ───────────────────


def _no_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _allow(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(ep.fga_deps, "require_relation", _allow)


def test_admins_from_fga_list_users(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_gate(monkeypatch)
    _patch_records(monkeypatch, _RECORDS)
    calls: list[tuple[str, str]] = []

    async def _fake_list_users(_client: Any, *, relation: str, obj: str, **kw: Any) -> list[str]:
        calls.append((relation, obj))
        # Display-only path: a single attempt under the tight per-call budget — the wrapper's default
        # bounded-retry ladder alone could exceed it, so the endpoint must pin retry_attempts=1.
        assert kw == {"retry_attempts": 1}
        return ["alice"] if obj == "project:acme" else []

    monkeypatch.setattr(ep.fga, "list_users", _fake_list_users)
    result = _list(_settings(fga_enabled=True), client=object())
    assert [(p.project, p.admins) for p in result] == [("acme", ["alice"]), ("globex", [])]
    assert sorted(calls) == [("can_administer", "project:acme"), ("can_administer", "project:globex")]


def test_admins_empty_when_fga_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_records(monkeypatch, _RECORDS)

    async def _boom(*_a: Any, **_k: Any) -> list[str]:  # FGA off → list_users must not even be called
        raise AssertionError("list_users must not be called when FGA is off")

    monkeypatch.setattr(ep.fga, "list_users", _boom)
    result = _list(_settings(fga_enabled=False))
    assert all(p.admins == [] for p in result)


def test_admins_degrade_on_fga_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    # An OpenFGA outage during the admins lookup degrades to [] — the registry-derived facts still serve;
    # it must never surface as a 500/503 (the estate gate already ran fail-closed before this point).
    _no_gate(monkeypatch)
    _patch_records(monkeypatch, _RECORDS)

    async def _outage(*_a: Any, **_k: Any) -> list[str]:
        raise ServiceUnavailableError("authorization service unavailable")

    monkeypatch.setattr(ep.fga, "list_users", _outage)
    result = _list(_settings(fga_enabled=True), client=object())
    assert [p.project for p in result] == ["acme", "globex"]
    assert all(p.admins == [] for p in result)


def test_admins_degrade_on_a_slow_fga(monkeypatch: pytest.MonkeyPatch) -> None:
    # A BROWNOUT (slow, not down): a lookup that outlives the per-call budget must degrade to [] on
    # expiry — never ride the wrapper's retry ladder toward the BFF's 8s request deadline.
    _no_gate(monkeypatch)
    _patch_records(monkeypatch, _RECORDS)
    monkeypatch.setattr(ep, "_ADMINS_TIMEOUT_SECONDS", 0.05)

    async def _slow(*_a: Any, **_k: Any) -> list[str]:
        await asyncio.sleep(0.5)  # well past the (shrunk) budget
        return ["alice"]

    monkeypatch.setattr(ep.fga, "list_users", _slow)
    result = _list(_settings(fga_enabled=True), client=object())
    assert [p.project for p in result] == ["acme", "globex"]
    assert all(p.admins == [] for p in result)  # degraded per project, response still whole


def test_admins_lookups_run_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    # The per-project lookups are gathered, not sequential: worst case is ONE per-call budget regardless
    # of tenant count. Both fakes must be in flight before either finishes.
    _no_gate(monkeypatch)
    _patch_records(monkeypatch, _RECORDS)
    started, overlap = set(), []

    async def _tracking(_client: Any, *, obj: str, **_k: Any) -> list[str]:
        started.add(obj)
        await asyncio.sleep(0.05)  # long enough for the other call to have started
        overlap.append(len(started))
        return []

    monkeypatch.setattr(ep.fga, "list_users", _tracking)
    _list(_settings(fga_enabled=True), client=object())
    assert overlap and all(n == 2 for n in overlap)  # both projects' lookups overlapped in time
