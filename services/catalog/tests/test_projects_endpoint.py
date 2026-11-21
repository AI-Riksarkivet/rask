"""Unit tests for the derived-tenant endpoints (``GET /management/v1/projects[/{id}]``, endpoints/projects.py).

Drives the handlers directly (no TestClient) with fake registry records + settings, mirroring the repo's
direct-call style (``test_events_endpoint.py``). Pins the design decisions: the list is the caller's
projects (every tenant for an estate observer, ``can_list_all_projects`` on the fixed root object; otherwise
the registry's projects the caller is a ``member`` of); with FGA off one tenant's read answers the list's shape,
and a malformed id is refused before any lookup; grouping tolerates a corrupt record (skip-with-warning,
never a 500); ``admins`` degrades to ``[]`` when FGA is off or the ``list_users`` lookup is unavailable.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pytest
from lance_namespace import InvalidInputError, ServiceUnavailableError
from openfga_sdk import OpenFgaClient

from catalog.api.v1.endpoints import projects as ep
from lakehouse.governed import fga_provision
from lakehouse.governed.idp_identity import PersonPrincipal
from lakehouse.governed.oidc import IDToken
from lakehouse.governed.principal import Principal
from lakehouse.records.warehouse_records import WarehouseRecord
from lakehouse.storage.options import StorageOptions


def _settings(*, fga_enabled: bool = False) -> Any:
    # fga_enabled=False: every authorization decision allows (``decisions.authorization`` answers None) and
    # _admins_for degrades to []; the list's own check is asserted with FGA on below. registry_root/storage_options
    # feed the (patched) list_warehouses call.
    return SimpleNamespace(
        fga_enabled=fga_enabled,
        fga_root_object="warehouse:lance_catalog",
        registry_root="file:///unused",
        storage_options=lambda: StorageOptions(),
    )


_RECORDS: list[dict[str, str]] = [
    {"id": "wh-b", "bucket": "bkt-b", "root_uri": "s3://bkt-b", "project": "acme", "status": "deactivated"},
    {"id": "wh-a", "bucket": "bkt-a", "root_uri": "s3://bkt-a", "project": "acme"},  # no status → "active"
    {"id": "wh-g", "bucket": "bkt-g", "root_uri": "s3://bkt-g", "project": "globex", "status": "active"},
]


def _patch_records(monkeypatch: pytest.MonkeyPatch, records: list[dict[str, str]]) -> None:
    monkeypatch.setattr(ep.warehouses, "list_warehouses", lambda _root, _so: [WarehouseRecord.model_validate(record) for record in records])


_BOB = PersonPrincipal(principal=Principal(idp_id="dex", subject="bob"), claims=IDToken(iss="i", sub="bob", aud="lance", exp=1, iat=1))


def _client() -> OpenFgaClient:
    """A client carrying the real one's signature; the FGA calls these tests answer are replaced on `ep.fga`."""
    return create_autospec(OpenFgaClient, instance=True)


def _list(settings: Any, *, token: Any = None, client: Any = None) -> list[ep.ProjectResponse]:
    return asyncio.run(ep.list_projects(settings=settings, token=token, client=client))


def _get(project_id: str, settings: Any, *, token: Any = None, client: Any = None) -> ep.ProjectResponse:
    return asyncio.run(ep.get_project(project_id=project_id, settings=settings, token=token, client=client))


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


def test_malformed_project_id_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_records(monkeypatch, _RECORDS)
    with pytest.raises(InvalidInputError):  # DNS-safe id shape enforced before any lookup
        _get("Bad_ID!", _settings())


def test_get_project_returns_list_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_records(monkeypatch, _RECORDS)
    result = _get("acme", _settings())
    assert result.project == "acme"
    assert [w.id for w in result.warehouses] == ["wh-a", "wh-b"] and result.admins == []


# ── admins: fga_reads.list_users on project:<id>#can_administer, degrading — never a 500 ───────────────────


def _an_estate_observer(monkeypatch: pytest.MonkeyPatch) -> None:
    """bob holds ``can_list_all_projects`` on the estate root, so he lists every tenant: the check answers as a store
    holding that one tuple would, and no for anything else."""

    async def _observes(_client: OpenFgaClient, *, user: str, relation: str, obj: str, **_kw: Any) -> bool:
        return (user, relation, obj) == ("dex~bob", "can_list_all_projects", "warehouse:lance_catalog")

    monkeypatch.setattr(ep.fga_reads, "check", _observes)


def test_admins_are_the_projects_effective_administrators(monkeypatch: pytest.MonkeyPatch, openfga_store: Any) -> None:  # noqa: ANN401 — the root conftest's OpenFgaStore
    """``admins`` is ``can_administer`` on each project as the real model derives it: a direct admin and a role's
    assignee alike, and nobody who is only a member. bob observes the estate, so he lists every tenant."""
    _patch_records(monkeypatch, _RECORDS)
    openfga_store.write(
        ("user:dex~bob", "owner", "estate:rask"),
        ("user:dex~alice", "admin", "project:acme"),
        ("role:leads#assignee", "admin", "project:acme"),
        ("user:dex~carol", "assignee", "role:leads"),
        ("user:dex~dave", "member", "project:globex"),
    )
    settings: Any = SimpleNamespace(**{**vars(_settings(fga_enabled=True)), "fga_root_object": "estate:rask"})

    async def _listed() -> list[ep.ProjectResponse]:
        async with fga_provision.make_client(openfga_store.url, openfga_store.store_id, openfga_store.model_id, token_file=None) as client:
            return await ep.list_projects(settings=settings, token=_BOB, client=client)

    result = asyncio.run(_listed())

    assert [(p.project, sorted(p.admins)) for p in result] == [("acme", ["dex~alice", "dex~carol"]), ("globex", [])]


def test_admins_degrade_on_fga_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    # An OpenFGA outage during the admins lookup degrades to [] — the registry-derived facts still serve;
    # it must never surface as a 500/503 (the estate gate already ran fail-closed before this point).
    _an_estate_observer(monkeypatch)
    _patch_records(monkeypatch, _RECORDS)

    async def _outage(*_a: Any, **_k: Any) -> list[str]:
        raise ServiceUnavailableError("authorization service unavailable")

    monkeypatch.setattr(ep.fga_reads, "list_users", _outage)
    result = _list(_settings(fga_enabled=True), token=_BOB, client=_client())
    assert [p.project for p in result] == ["acme", "globex"]
    assert all(p.admins == [] for p in result)


def test_admins_degrade_on_a_slow_fga(monkeypatch: pytest.MonkeyPatch) -> None:
    # A BROWNOUT (slow, not down): a lookup that outlives the per-call budget must degrade to [] on
    # expiry — never ride the wrapper's retry ladder toward the caller's request deadline.
    _an_estate_observer(monkeypatch)
    _patch_records(monkeypatch, _RECORDS)
    monkeypatch.setattr(ep, "_ADMINS_TIMEOUT_SECONDS", 0.05)

    async def _slow(*_a: Any, **_k: Any) -> list[str]:
        await asyncio.sleep(0.5)  # well past the (shrunk) budget
        return ["alice"]

    monkeypatch.setattr(ep.fga_reads, "list_users", _slow)
    result = _list(_settings(fga_enabled=True), token=_BOB, client=_client())
    assert [p.project for p in result] == ["acme", "globex"]
    assert all(p.admins == [] for p in result)  # degraded per project, response still whole
