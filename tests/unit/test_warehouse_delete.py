"""``DELETE /management/v1/warehouses/{id}`` — the bottom-up delete door (`open_hierarchy_lifecycle.md` Decision 3).

The registry round-trips against a LOCAL filesystem control root (``service_kit.lakehouse.objectfs.fs_and_base``
falls back to the local FS for a non-``s3://`` uri), so no object storage is needed; the warehouse's
bucket-rooted connection is a recording stand-in pre-seeded into the request's connection cache, so a
cascade exercises the REAL ``native.call`` path without reaching S3, and the bucket purge is asserted by
whether the purge primitive was called at all.

The invariants under test are the ones a delete gets wrong silently: that the FGA gate is the PROJECT's
admin rung and runs before anything that could disclose what the warehouse holds, that ``force`` turns the
protection lock and NOT the authorization one, that a cascade survives a binding whose namespace is already
gone, and that the bucket's bytes are never removed by default. What a cascade removes and revokes, on real
pylance, is `tests/integration/test_a_warehouse_cascade_revokes_a_grant_only_once_its_object_is_gone.py`.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import NAMESPACE_URL, uuid5

import pytest
from lance_namespace import (
    DescribeNamespaceRequest,
    DescribeNamespaceResponse,
    InvalidInputError,
    ListNamespacesRequest,
    ListNamespacesResponse,
    ListTablesRequest,
    ListTablesResponse,
    NamespaceNotEmptyError,
    NamespaceNotFoundError,
    TableNotFoundError,
)

from catalog.core.config import Settings
from catalog.core.lineage_emit import NoopEmitter
from catalog.core.vending import ModeBVendor
from catalog.services import warehouses
from service_kit.control_emit import NoopControlEmitter
from service_kit.governed import fga as fga_module
from service_kit.governed.idp_identity import PersonPrincipal
from service_kit.governed.oidc import IDToken
from service_kit.governed.principal import Principal
from service_kit.lakehouse.naming import UUID_PROPERTY
from service_kit.lakehouse.objectfs import StorageOptions
from tests.unit.no_uuids import NO_OBJECT_UUIDS


def _root(tmp_path: Any) -> str:
    return f"file://{tmp_path}"


def _settings(tmp_path: Any, *, fga_enabled: bool = False) -> Settings:
    data: dict[str, object] = {
        "warehouses_enabled": True,
        "control_root": _root(tmp_path),
        "s3_access_key_id": "x",
        "s3_secret_access_key": "x",
    }
    if fga_enabled:
        data |= {"fga_enabled": True, "oidc_enabled": True, "oidc_issuer": "https://idp", "oidc_audience": "lance", "oidc_connectors": {"local": "dex"}}
    return Settings.model_validate(data)


def _caller(key: str) -> PersonPrincipal:
    principal = Principal.parse(key)
    return PersonPrincipal(principal=principal, claims=IDToken(iss="i", sub=principal.subject, aud="lance", exp=1, iat=1))


_TOKEN = _caller("dex~alice")


class _FakeNamespace:
    """The warehouse's bucket-rooted connection: empty namespaces, and the drops ``native.call`` routes to it.

    ``missing`` makes a namespace answer ``NamespaceNotFoundError`` — the drift case where a binding
    outlived the namespace it points at, which the cascade must survive rather than deadlock on.
    """

    def __init__(self, *, missing: frozenset[str] = frozenset()) -> None:
        self.dropped: list[list[str]] = []
        self._missing = missing

    def _require(self, segments: list[str] | None) -> None:
        if segments and segments[0] in self._missing:
            raise NamespaceNotFoundError(f"namespace not found: {segments}")

    def describe_namespace(self, request: DescribeNamespaceRequest) -> DescribeNamespaceResponse:
        """As the backend answers it: the namespace with the uuid its create minted (ADR 0173 §2)."""
        self._require(request.id)
        return DescribeNamespaceResponse(properties={UUID_PROPERTY: str(uuid5(NAMESPACE_URL, "$".join(request.id or [])))})

    def list_tables(self, request: ListTablesRequest) -> ListTablesResponse:
        self._require(request.id)
        return ListTablesResponse(tables=[])

    def list_namespaces(self, request: ListNamespacesRequest) -> ListNamespacesResponse:
        self._require(request.id)
        return ListNamespacesResponse(namespaces=[])

    def drop_namespace(self, request: Any) -> None:
        self._require(request.id)
        self.dropped.append(list(request.id))


class _Connections:
    """The catalog's connections, answering the fake for the warehouse's root, so no real backend is built."""

    def __init__(self, root_uri: str, ns: _FakeNamespace) -> None:
        self._root_uri = root_uri
        self._ns = ns

    def connection(self, _settings: Settings, *, root: str | None = None, endpoint: str | None = None, plane: str = "metadata") -> _FakeNamespace:
        assert (root, endpoint) == (self._root_uri, None), f"the delete reached {root!r} at {endpoint!r}, not its warehouse's root"
        return self._ns


def _request(root_uri: str = "s3://acme-bkt", ns: _FakeNamespace | None = None) -> Any:
    """A stand-in request carrying the two pieces of app state the delete touches: the positive binding
    cache (which it must evict) and the catalog's connections (answering a fake, so no real backend is built)."""
    state = SimpleNamespace(warehouse_binding_cache={}, connections=_Connections(root_uri, ns if ns is not None else _FakeNamespace()))
    return SimpleNamespace(app=SimpleNamespace(state=state))


def _seed(
    settings: Settings,
    *,
    warehouse_id: str = "acme-wh",
    project: str = "acme",
    bucket: str = "acme-bkt",
    namespaces: tuple[str, ...] = (),
    protected: bool = False,
) -> dict[str, str]:
    """Write a warehouse record (+ its namespace bindings) straight into the registry."""
    so = settings.storage_options()
    record = {
        "id": warehouse_id,
        "bucket": bucket,
        "root_uri": f"s3://{bucket}",
        "project": project,
        "status": "active",
        "created_at": "t",
    }
    if protected:
        record["protected"] = "true"
    warehouses.put_warehouse(settings.registry_root, so, record)
    for top_ns in namespaces:
        warehouses.bind_namespace(settings.registry_root, so, top_ns, warehouse_id, record["root_uri"])
    return record


def _delete(
    settings: Settings,
    request: Any,
    *,
    warehouse_id: str = "acme-wh",
    token: PersonPrincipal | None = None,
    client: Any = None,
    cascade: bool = False,
    purge_bucket: bool = False,
    force: bool = False,
) -> Any:
    from catalog.api.v1.endpoints import warehouses as wh_ep

    return asyncio.run(
        wh_ep.delete_warehouse(
            warehouse_id=warehouse_id,
            request=request,
            settings=settings,
            token=token,
            client=client,
            control=NoopControlEmitter(),
            emitter=NoopEmitter(),
            uuids=NO_OBJECT_UUIDS,
            cascade=cascade,
            purge_bucket=purge_bucket,
            force=force,
        )
    )


class _FakeStore:
    """A minimal OpenFGA stand-in resolving the ONE relation this door checks — ``project#can_administer``
    from a direct ``project#admin`` tuple, exactly as the compiled model derives it. Any other relation is
    an error, so a gate quietly re-pointed at the warehouse (or at a weaker rung) fails the test rather
    than passing it."""

    def __init__(self, *, project_admins: tuple[str, ...] = (), project: str = "acme") -> None:
        self.tuples: set[tuple[str, str, str]] = {(f"user:{sub}", "admin", f"project:{project}") for sub in project_admins}
        self.checks: list[tuple[str, str, str]] = []
        self.revoked: list[str] = []

    async def check(self, _client: object, *, user: str, relation: str, obj: str, **_kw: object) -> bool:
        self.checks.append((user, relation, obj))
        if relation != "can_administer":
            raise AssertionError(f"the warehouse-delete gate checked an unexpected relation: {relation}")
        subject = user if ":" in user else f"user:{user}"
        return (subject, "admin", obj) in self.tuples

    async def revoke(self, _client: object, obj: str, **_kw: object) -> list[SimpleNamespace]:
        """Hands back WHAT it revoked, mirroring the helper: a count cannot be fanned out to the
        principals who just lost access."""
        self.revoked.append(obj)
        return [SimpleNamespace(user=f"user:u{i}", relation="owner", object=obj) for i in range(3)]

    async def read(self, _client: object, obj: str, **_kw: object) -> list[SimpleNamespace]:
        """The tuples held on ``obj``: what the delete reads for a namespace an earlier attempt left on the warehouse."""
        return [SimpleNamespace(user=user, relation=relation, object=held) for user, relation, held in self.tuples if held == obj]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fga_module, "check", self.check)
        monkeypatch.setattr(fga_module, "revoke_object_tuples", self.revoke)
        monkeypatch.setattr(fga_module, "read_object_tuples", self.read)


# --------------------------------------------------------------------------- #
# existence + emptiness
# --------------------------------------------------------------------------- #


def test_deleting_a_warehouse_the_registry_never_heard_of_is_a_404(tmp_path: Any) -> None:
    with pytest.raises(TableNotFoundError):
        _delete(_settings(tmp_path), _request(), warehouse_id="ghost-wh")


def test_a_warehouse_that_still_holds_namespaces_refuses_and_names_every_one_of_them(tmp_path: Any) -> None:
    # A 409 that does not say WHAT blocks it just moves the search to the user, so the refusal lists the
    # contents and the flag that would take them — and nothing is deleted behind it.
    settings = _settings(tmp_path)
    _seed(settings, namespaces=("bronze", "silver"))
    with pytest.raises(NamespaceNotEmptyError) as exc:
        _delete(settings, _request())
    message = str(exc.value)
    assert "bronze" in message and "silver" in message
    assert "cascade=true" in message
    assert warehouses.get_warehouse(settings.registry_root, StorageOptions(), "acme-wh") is not None
    assert warehouses.namespaces_bound_to(settings.registry_root, StorageOptions(), "acme-wh") == ["bronze", "silver"]


def test_a_cascade_survives_a_binding_whose_namespace_is_already_gone(tmp_path: Any) -> None:
    # Drift (a half-finished earlier delete): refusing here would make the binding undeletable by anyone,
    # so the absent namespace is logged and its registry record is still cleaned up.
    settings = _settings(tmp_path)
    _seed(settings, namespaces=("bronze",))
    ns = _FakeNamespace(missing=frozenset({"bronze"}))
    result = _delete(settings, _request(ns=ns), cascade=True)
    assert ns.dropped == []
    assert result.namespaces_dropped == ["bronze"]
    assert warehouses.namespaces_bound_to(settings.registry_root, StorageOptions(), "acme-wh") == []


# --------------------------------------------------------------------------- #
# the bucket's bytes — a separate, explicit opt-in
# --------------------------------------------------------------------------- #


def test_the_bucket_survives_a_plain_delete(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # A catalog entry is recoverable, a customer's bucket is not — the two never share a default. The
    # purge primitive must not be reached AT ALL, not merely called with a flag that spares the data.
    settings = _settings(tmp_path)
    _seed(settings)
    purged: list[str] = []
    monkeypatch.setattr(warehouses, "purge_bucket", lambda bucket, so: purged.append(bucket) or 0)

    result = _delete(settings, _request())

    assert purged == []
    assert result.bucket == "acme-bkt"  # named anyway, so the operator knows what survived
    assert result.bucket_purged is False
    assert result.objects_purged == 0


def test_purge_bucket_true_removes_the_bytes_and_reports_the_count(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    _seed(settings)
    purged: list[str] = []

    def fake_purge(bucket: str, _so: object) -> int:
        purged.append(bucket)
        return 7

    monkeypatch.setattr(warehouses, "purge_bucket", fake_purge)
    result = _delete(settings, _request(), purge_bucket=True)

    assert purged == ["acme-bkt"]
    assert result.bucket_purged is True
    assert result.objects_purged == 7  # the count the purge really returned, not the flag restated


def test_a_purge_refuses_while_a_sibling_warehouse_still_claims_the_same_bucket(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The create door lets ONE project back two warehouses with one bucket (its cross-claim guard subtracts
    the caller's own project — a work plus a ``serving="gold"`` pair is exactly that shape). A purge deletes
    every object AND the bucket, so without a symmetric guard here, deleting one of the pair destroys the
    other's data and leaves its record pointing at a bucket that no longer exists. Refuse, and name it."""
    settings = _settings(tmp_path)
    _seed(settings, warehouse_id="acme-work", bucket="acme-bkt")
    _seed(settings, warehouse_id="acme-gold", bucket="acme-bkt")
    purged: list[str] = []
    monkeypatch.setattr(warehouses, "purge_bucket", lambda bucket, so: purged.append(bucket) or 0)

    with pytest.raises(NamespaceNotEmptyError) as exc:
        _delete(settings, _request(), warehouse_id="acme-work", purge_bucket=True)

    assert "acme-gold" in str(exc.value)
    assert purged == []
    # The refusal lands before ANY step: the warehouse the caller named is still registered.
    assert warehouses.get_warehouse(settings.registry_root, StorageOptions(), "acme-work") is not None


def test_the_same_warehouse_deletes_fine_without_the_purge_flag(tmp_path: Any) -> None:
    # The sibling guard bounds the BYTES, not the record — a shared bucket must not make a warehouse
    # undeletable, only unpurgeable through this door.
    settings = _settings(tmp_path)
    _seed(settings, warehouse_id="acme-work", bucket="acme-bkt")
    _seed(settings, warehouse_id="acme-gold", bucket="acme-bkt")
    result = _delete(settings, _request(), warehouse_id="acme-work")
    assert result.bucket_purged is False
    assert warehouses.get_warehouse(settings.registry_root, StorageOptions(), "acme-work") is None
    assert warehouses.get_warehouse(settings.registry_root, StorageOptions(), "acme-gold") is not None


def test_a_purge_refuses_a_bucket_that_is_reserved_platform_storage(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """``create_warehouse`` refuses a warehouse over the catalog root/registry or a medallion zone bucket —
    but a record written before that guard existed still names one, and purging it would delete the whole
    estate's registry. The destroy door re-checks rather than trusting the create door's history."""
    settings = _settings(tmp_path)
    monkeypatch.setattr(type(settings), "reserved_bucket_set", property(lambda _self: frozenset({"acme-bkt"})))
    _seed(settings)
    purged: list[str] = []
    monkeypatch.setattr(warehouses, "purge_bucket", lambda bucket, so: purged.append(bucket) or 0)

    with pytest.raises(InvalidInputError) as exc:
        _delete(settings, _request(), purge_bucket=True)

    assert "reserved platform storage" in str(exc.value)
    assert purged == []
    assert warehouses.get_warehouse(settings.registry_root, StorageOptions(), "acme-wh") is not None


# --------------------------------------------------------------------------- #
# deletion protection (Decision 5) — and the lock `force` does NOT turn
# --------------------------------------------------------------------------- #


def test_a_protected_warehouse_refuses_the_delete(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path)
    _seed(settings, protected=True)
    purged: list[str] = []
    monkeypatch.setattr(warehouses, "purge_bucket", lambda bucket, so: purged.append(bucket) or 0)

    with pytest.raises(NamespaceNotEmptyError) as exc:
        _delete(settings, _request(), purge_bucket=True)

    assert "force=true" in str(exc.value)
    assert warehouses.get_warehouse(settings.registry_root, StorageOptions(), "acme-wh") is not None
    assert purged == []  # the refusal lands BEFORE the irreversible half


def test_force_true_overrides_the_protection_flag(tmp_path: Any) -> None:
    settings = _settings(tmp_path)
    _seed(settings, protected=True)
    _delete(settings, _request(), force=True)
    assert warehouses.get_warehouse(settings.registry_root, StorageOptions(), "acme-wh") is None


def test_an_idempotent_re_create_does_not_silently_disarm_the_protection_flag(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """``create_warehouse`` rebuilds the record from scratch, so every mutable lifecycle field it does not
    explicitly carry forward is DESTROYED by a re-POST — the ordinary GitOps-reconcile / retry path.

    ``protected`` is the one whose loss is both silent and irreversible: with the flag gone, this delete
    door stops asking for ``force=true`` and ``?purge_bucket=true`` takes the bucket. So the re-create must
    preserve it, and the proof is that the delete still refuses afterwards.
    """
    from catalog.schemas import CreateWarehouseRequest
    from catalog.services import projects as proj_svc

    settings = _settings(tmp_path)
    monkeypatch.setattr(warehouses, "provision_bucket", lambda bucket, so: None)
    proj_svc.put_project(settings.registry_root, settings.storage_options(), {"id": "acme", "created_at": "t", "created_by": "unit", "protected": "false"})
    _seed(settings, protected=True)

    async def _recreate() -> None:
        from catalog.api.v1.endpoints import warehouses as wh_ep

        await wh_ep.create_warehouse(
            settings=settings,
            token=None,
            client=None,
            control=NoopControlEmitter(),
            vendor=ModeBVendor(),  # the default `get_vendor` falls back to: no direct credentials, so the probe skips
            body=CreateWarehouseRequest(id="acme-wh", project="acme"),
        )

    asyncio.run(_recreate())

    record = warehouses.get_warehouse(settings.registry_root, StorageOptions(), "acme-wh")
    assert record is not None and record.get("protected") == "true"
    with pytest.raises(NamespaceNotEmptyError):
        _delete(settings, _request())


def test_force_true_without_authorization_is_still_refused(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The security-critical one: ``force`` turns the PROTECTION lock only.

    Both locks are set here — protected AND unauthorized — and the caller passes ``force=true``. The
    outcome must be the AUTHORIZATION failure, which proves the FGA gate is consulted before force is even
    read: had the order been reversed, force would have unlocked protection and the delete would have gone
    on to a rung this caller never held. Nothing is deleted, nothing is purged.

    The failure surfaces as the missing-warehouse 404, not a 403 — NO EXISTENCE ORACLE (audit #4, the rule
    ``_set_warehouse_status`` set in this same module): denial and absence are made indistinguishable so
    the door cannot be used to probe which warehouse ids exist. Strictly less disclosure than a 403.
    """
    settings = _settings(tmp_path, fga_enabled=True)
    _seed(settings, protected=True)
    store = _FakeStore()  # mallory holds NO admin tuple on project:acme
    store.install(monkeypatch)
    purged: list[str] = []
    monkeypatch.setattr(warehouses, "purge_bucket", lambda bucket, so: purged.append(bucket) or 0)

    with pytest.raises(TableNotFoundError):
        _delete(
            settings,
            _request(),
            token=_caller("dex~mallory"),
            client=MagicMock(),
            force=True,
            purge_bucket=True,
        )

    assert warehouses.get_warehouse(settings.registry_root, StorageOptions(), "acme-wh") is not None
    assert purged == []
    assert store.revoked == []


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #


def test_the_gate_is_the_projects_admin_rung_never_a_relation_on_the_warehouse(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Deleting storage is a TENANT-level act: the door is `can_administer` on the warehouse's OWN project,
    # not a rung someone could hold on this one warehouse (a warehouse owner must not be able to delete it).
    settings = _settings(tmp_path, fga_enabled=True)
    _seed(settings)
    store = _FakeStore(project_admins=("dex~alice",))
    store.install(monkeypatch)

    _delete(settings, _request(), token=_TOKEN, client=MagicMock())

    assert store.checks == [("dex~alice", "can_administer", "project:acme")]
    assert not any(obj.startswith("warehouse:") for _u, _r, obj in store.checks)
