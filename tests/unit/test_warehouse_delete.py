"""``DELETE /v1/warehouses/{id}`` — the bottom-up delete door (`open_hierarchy_lifecycle.md` Decision 3).

The registry round-trips against a LOCAL filesystem control root (``service_kit.lakehouse.objectfs.fs_and_base``
falls back to the local FS for a non-``s3://`` uri), so no object storage is needed; the warehouse's
bucket-rooted connection is a recording stand-in pre-seeded into the request's connection cache, so a
cascade exercises the REAL ``native.call`` path without reaching S3, and the bucket purge is asserted by
whether the purge primitive was called at all.

The invariants under test are the ones a delete gets wrong silently: that the FGA gate is the PROJECT's
admin rung and runs before anything that could disclose what the warehouse holds, that ``force`` turns the
protection lock and NOT the authorization one, that a cascade leaves no binding (or cached route) behind,
and that the bucket's bytes are never removed by default.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from catalog.core.config import Settings
from catalog.core.control_emit import NoopControlEmitter
from catalog.services import warehouses
from lance_namespace import (
    InvalidInputError,
    NamespaceNotEmptyError,
    NamespaceNotFoundError,
    TableNotFoundError,
)

from service_kit.governed import fga as fga_module
from service_kit.governed.oidc import IDToken


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
        data |= {"fga_enabled": True, "oidc_enabled": True, "oidc_issuer": "https://idp", "oidc_audience": "lance"}
    return Settings.model_validate(data)


_TOKEN = IDToken(iss="i", sub="alice", aud="lance", exp=1, iat=1)


class _FakeNamespace:
    """The warehouse's bucket-rooted connection: records the drops ``native.call`` routes to it.

    ``missing`` makes a namespace answer ``NamespaceNotFoundError`` — the drift case where a binding
    outlived the namespace it points at, which the cascade must survive rather than deadlock on.
    """

    def __init__(self, *, missing: frozenset[str] = frozenset()) -> None:
        self.dropped: list[list[str]] = []
        self._missing = missing

    def drop_namespace(self, request: Any) -> None:
        if request.id and request.id[0] in self._missing:
            raise NamespaceNotFoundError(f"namespace not found: {request.id}")
        self.dropped.append(list(request.id))


def _request(root_uri: str = "s3://acme-bkt", ns: _FakeNamespace | None = None, *, cached: tuple[str, ...] = ()) -> Any:
    """A stand-in request carrying the two pieces of app state the delete touches: the positive binding
    cache (which it must evict) and the per-root connection cache (pre-seeded, so no real backend is built)."""
    state = SimpleNamespace(
        warehouse_binding_cache={top_ns: {"warehouse_id": "acme-wh", "root_uri": root_uri} for top_ns in cached},
        warehouse_namespaces={root_uri: ns if ns is not None else _FakeNamespace()},
    )
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
    token: IDToken | None = None,
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

    async def revoke(self, _client: object, obj: str, **_kw: object) -> int:
        self.revoked.append(obj)
        return 3

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(fga_module, "check", self.check)
        monkeypatch.setattr(fga_module, "revoke_object_tuples", self.revoke)


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
    assert warehouses.get_warehouse(settings.registry_root, {}, "acme-wh") is not None
    assert warehouses.namespaces_bound_to(settings.registry_root, {}, "acme-wh") == ["bronze", "silver"]


def test_an_empty_warehouse_deletes_cleanly(tmp_path: Any) -> None:
    settings = _settings(tmp_path)
    _seed(settings)
    result = _delete(settings, _request())
    assert result.id == "acme-wh"
    assert result.namespaces_dropped == []
    assert warehouses.get_warehouse(settings.registry_root, {}, "acme-wh") is None


def test_cascade_drops_every_bound_namespace_and_leaves_no_binding_or_cached_route_behind(tmp_path: Any) -> None:
    # A cascade that leaves a binding behind is a bug, and so is one that leaves the POSITIVE routing cache
    # pointing at a warehouse that no longer exists — the resolver caches bindings forever.
    settings = _settings(tmp_path)
    _seed(settings, namespaces=("bronze", "silver"))
    ns = _FakeNamespace()
    request = _request(ns=ns, cached=("bronze", "silver"))

    result = _delete(settings, request, cascade=True)

    assert ns.dropped == [["bronze"], ["silver"]]  # the REAL native drop path, in the warehouse's own bucket
    assert result.namespaces_dropped == ["bronze", "silver"]
    assert warehouses.namespaces_bound_to(settings.registry_root, {}, "acme-wh") == []
    assert warehouses.get_warehouse(settings.registry_root, {}, "acme-wh") is None
    assert request.app.state.warehouse_binding_cache == {}


def test_a_cascade_survives_a_binding_whose_namespace_is_already_gone(tmp_path: Any) -> None:
    # Drift (a half-finished earlier delete): refusing here would make the binding undeletable by anyone,
    # so the absent namespace is logged and its registry record is still cleaned up.
    settings = _settings(tmp_path)
    _seed(settings, namespaces=("bronze",))
    ns = _FakeNamespace(missing=frozenset({"bronze"}))
    result = _delete(settings, _request(ns=ns), cascade=True)
    assert ns.dropped == []
    assert result.namespaces_dropped == ["bronze"]
    assert warehouses.namespaces_bound_to(settings.registry_root, {}, "acme-wh") == []


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
    assert warehouses.get_warehouse(settings.registry_root, {}, "acme-work") is not None


def test_the_same_warehouse_deletes_fine_without_the_purge_flag(tmp_path: Any) -> None:
    # The sibling guard bounds the BYTES, not the record — a shared bucket must not make a warehouse
    # undeletable, only unpurgeable through this door.
    settings = _settings(tmp_path)
    _seed(settings, warehouse_id="acme-work", bucket="acme-bkt")
    _seed(settings, warehouse_id="acme-gold", bucket="acme-bkt")
    result = _delete(settings, _request(), warehouse_id="acme-work")
    assert result.bucket_purged is False
    assert warehouses.get_warehouse(settings.registry_root, {}, "acme-work") is None
    assert warehouses.get_warehouse(settings.registry_root, {}, "acme-gold") is not None


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
    assert warehouses.get_warehouse(settings.registry_root, {}, "acme-wh") is not None


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
    assert warehouses.get_warehouse(settings.registry_root, {}, "acme-wh") is not None
    assert purged == []  # the refusal lands BEFORE the irreversible half


def test_force_true_overrides_the_protection_flag(tmp_path: Any) -> None:
    settings = _settings(tmp_path)
    _seed(settings, protected=True)
    _delete(settings, _request(), force=True)
    assert warehouses.get_warehouse(settings.registry_root, {}, "acme-wh") is None


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
            body=CreateWarehouseRequest(id="acme-wh", project="acme"),
        )

    asyncio.run(_recreate())

    record = warehouses.get_warehouse(settings.registry_root, {}, "acme-wh")
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
            token=IDToken(iss="i", sub="mallory", aud="lance", exp=1, iat=1),
            client=MagicMock(),
            force=True,
            purge_bucket=True,
        )

    assert warehouses.get_warehouse(settings.registry_root, {}, "acme-wh") is not None
    assert purged == []
    assert store.revoked == []


# --------------------------------------------------------------------------- #
# the gate + the tuple revoke
# --------------------------------------------------------------------------- #


def test_the_gate_is_the_projects_admin_rung_never_a_relation_on_the_warehouse(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # Deleting storage is a TENANT-level act: the door is `can_administer` on the warehouse's OWN project,
    # not a rung someone could hold on this one warehouse (a warehouse owner must not be able to delete it).
    settings = _settings(tmp_path, fga_enabled=True)
    _seed(settings)
    store = _FakeStore(project_admins=("alice",))
    store.install(monkeypatch)

    _delete(settings, _request(), token=_TOKEN, client=MagicMock())

    assert store.checks == [("alice", "can_administer", "project:acme")]
    assert not any(obj.startswith("warehouse:") for _u, _r, obj in store.checks)


def test_a_non_admin_of_the_owning_project_is_denied_and_nothing_is_deleted(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(tmp_path, fga_enabled=True)
    _seed(settings, namespaces=("bronze",))
    store = _FakeStore(project_admins=("alice",))
    store.install(monkeypatch)

    # The no-oracle 404 (audit #4), not a 403: an outsider cannot tell "not yours" from "does not exist".
    with pytest.raises(TableNotFoundError):
        _delete(
            settings,
            _request(),
            token=IDToken(iss="i", sub="mallory", aud="lance", exp=1, iat=1),
            client=MagicMock(),
            cascade=True,
        )

    assert warehouses.get_warehouse(settings.registry_root, {}, "acme-wh") is not None
    assert warehouses.namespaces_bound_to(settings.registry_root, {}, "acme-wh") == ["bronze"]


def test_the_warehouses_own_tuples_are_revoked_and_so_are_every_cascaded_namespaces(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # A deleted object's grants must not linger: a later object reusing the id would inherit them
    # (stale-grant privilege bleed). The reported count is the one OpenFGA returned, summed over the
    # warehouse and every namespace the cascade took with it.
    settings = _settings(tmp_path, fga_enabled=True)
    _seed(settings, namespaces=("bronze",))
    store = _FakeStore(project_admins=("alice",))
    store.install(monkeypatch)

    result = _delete(settings, _request(), token=_TOKEN, client=MagicMock(), cascade=True)

    assert store.revoked == ["namespace:bronze", "warehouse:acme-wh"]
    assert result.tuples_revoked == 6  # 3 per object, as the fake store reports them
