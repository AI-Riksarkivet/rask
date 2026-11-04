"""``DELETE /management/v1/warehouses/{id}`` — the bottom-up delete door (`open_hierarchy_lifecycle.md` Decision 3).

The registry round-trips against a LOCAL filesystem control root (``lakehouse_kit.objectfs.fs_and_base``
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
from catalog.services import warehouses
from lakehouse_kit.control_emit import NoopControlEmitter
from service_kit.governed.idp_identity import PersonPrincipal
from service_kit.naming import UUID_PROPERTY
from service_kit.storage_options import StorageOptions
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

    async def _resolved_then_deleted() -> Any:
        # The door's gate is its `AdministeredWarehouseDep`, which FastAPI resolves before the handler runs.
        record = await wh_ep.administered_warehouse(warehouse_id, settings, token, client)
        return await wh_ep.delete_warehouse(
            warehouse_id=warehouse_id,
            record=record,
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

    return asyncio.run(_resolved_then_deleted())


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


def test_force_true_overrides_the_protection_flag(tmp_path: Any) -> None:
    settings = _settings(tmp_path)
    _seed(settings, protected=True)
    _delete(settings, _request(), force=True)
    assert warehouses.get_warehouse(settings.registry_root, StorageOptions(), "acme-wh") is None


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
