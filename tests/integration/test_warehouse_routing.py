"""#3-A REAL routing coverage (P2.2) — drives the actual warehouse-aware resolver, no fake namespace.

The other warehouse tests (``test_warehouses.py``) exercise the admin CONTROL plane (create/list/get) and
reuse the shared ``client`` fixture, which overrides ``get_namespace`` with a MagicMock. That leaves the most
load-bearing #3-A path — ``get_namespace → _resolve_warehouse_root → warehouses.warehouse_for_namespace``,
the routing that sends a bound namespace's tables to its physically-isolated bucket — with ZERO non-faked
coverage. These tests build a REAL app (real dir namespaces, real local-FS registry, resolver NOT overridden)
so the routing is proven, not assumed: a table created under a bound namespace physically lands in the
warehouse's root and is ABSENT from the default root.
"""

from __future__ import annotations

import io
import time
from collections.abc import Iterator
from pathlib import Path

import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from lance_namespace import CreateNamespaceRequest, connect

from catalog.services import warehouses as wh_svc


ARROW_STREAM = {"content-type": "application/vnd.apache.arrow.stream"}


def _arrow(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema) as w:
        w.write_table(table)
    return sink.getvalue()


@pytest.fixture
def routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Path, Path, Path]]:
    """A REAL app: warehouses on, a real default-root dir namespace, a local-FS registry, and — crucially —
    ``get_namespace`` NOT overridden, so the warehouse resolver actually runs. Yields (client, default_root,
    warehouse_root, registry)."""
    yield from _routing_app(tmp_path, monkeypatch)


def _routing_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Path, Path, Path]]:
    """The fixture body, shared so a variant can set extra env (see ``routing_recoverable``) without
    copying the whole app build — a second copy would drift from this one the first time either moved."""
    default_root = tmp_path / "default"
    warehouse_root = tmp_path / "wh-bucket"
    registry = tmp_path / "registry"
    for d in (default_root, warehouse_root, registry):
        d.mkdir()

    monkeypatch.setenv("LANCE_REST_IMPL", "dir")
    monkeypatch.setenv("LANCE_REST_ROOT", str(default_root))
    monkeypatch.setenv("LANCE_WAREHOUSES_ENABLED", "true")
    monkeypatch.setenv("LANCE_CONTROL_ROOT", f"file://{registry}")
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("LANCE_S3_SECRET_ACCESS_KEY", "x")

    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app) as client:
        yield client, default_root, warehouse_root, registry
    get_settings.cache_clear()


def _register_warehouse(registry: Path, warehouse_root: Path, *, wid: str = "wh-a") -> None:
    """Register a warehouse whose root is a LOCAL dir (not s3://) so the resolver can open it in-process,
    and pre-create its top-level namespace, mirroring what create_warehouse_namespace does on a real stack."""
    control = f"file://{registry}"
    wh_svc.put_warehouse(control, {}, {"id": wid, "bucket": wid, "root_uri": str(warehouse_root), "project": "acme"})
    # The tenant namespace lives IN the warehouse bucket — create it there, then bind.
    wh_ns = connect("dir", {"root": str(warehouse_root)})
    wh_ns.create_namespace(CreateNamespaceRequest(id=["tenantns"]))
    wh_svc.bind_namespace(control, {}, "tenantns", wid, str(warehouse_root))


def test_bound_namespace_table_lands_in_warehouse_root_not_default(
    routing: tuple[TestClient, Path, Path, Path],
) -> None:
    client, default_root, warehouse_root, registry = routing
    _register_warehouse(registry, warehouse_root)

    # Create a table under the BOUND namespace, through the REAL resolver (no get_namespace override).
    resp = client.post(
        "/v1/table/tenantns$clips/create?mode=overwrite",
        content=_arrow(pa.table({"id": [1, 2, 3]})),
        headers=ARROW_STREAM,
    )
    assert resp.status_code == 200, resp.text
    location = resp.json()["location"]

    # The routing WORKED: the dataset physically lives under the warehouse root, not the shared default root.
    assert str(warehouse_root) in location, f"table did not route to the warehouse root: {location}"
    assert str(default_root) not in location, f"table leaked into the default/shared root: {location}"
    assert lance.dataset(location).count_rows() == 3  # and it is real, readable data

    # And the default root physically holds no tenant table dir — true isolation, not just a different URI.
    default_children = {p.name for p in default_root.rglob("tenantns*")}
    assert not default_children, f"tenant data present in the default root: {default_children}"


def test_deactivate_quarantines_then_activate_restores(routing: tuple[TestClient, Path, Path, Path]) -> None:
    client, _default_root, warehouse_root, registry = routing
    _register_warehouse(registry, warehouse_root)

    def _create(name: str) -> int:
        return client.post(
            f"/v1/table/tenantns${name}/create?mode=overwrite",
            content=_arrow(pa.table({"id": [1]})),
            headers=ARROW_STREAM,
        ).status_code

    # Active → a create routes and succeeds.
    assert _create("t_before") == 200

    # Deactivate → the resolver quarantines EVERY op on the bound namespace (403), so no new table lands.
    d = client.post("/v1/warehouses/wh-a/deactivate")
    assert d.status_code == 200, d.text
    assert d.json()["status"] == "deactivated"
    blocked = client.post(
        "/v1/table/tenantns$t_during/create?mode=overwrite",
        content=_arrow(pa.table({"id": [1]})),
        headers=ARROW_STREAM,
    )
    assert blocked.status_code == 403, blocked.text
    assert "deactivated" in blocked.text.lower()

    # Activate → routing is restored; a create succeeds again (status is read live, no stale cache).
    a = client.post("/v1/warehouses/wh-a/activate")
    assert a.status_code == 200 and a.json()["status"] == "active"
    assert _create("t_after") == 200


def test_deactivate_missing_warehouse_404(routing: tuple[TestClient, Path, Path, Path]) -> None:
    client, *_ = routing
    assert client.post("/v1/warehouses/ghost/deactivate").status_code == 404


def test_binding_collides_with_existing_default_namespace_409(
    routing: tuple[TestClient, Path, Path, Path],
) -> None:
    client, _default_root, warehouse_root, registry = routing
    # A warehouse exists, but the name we will try to bind ALREADY exists unbound in the default root.
    wh_svc.put_warehouse(
        f"file://{registry}",
        {},
        {"id": "wh-a", "bucket": "wh-a", "root_uri": str(warehouse_root), "project": "acme"},
    )
    # A LEGACY unbound namespace in the default root. Created through the app's OWN native connection
    # rather than the HTTP door, because that door is gone: a top-level namespace must now be created
    # inside a warehouse (`require_warehouse_scoped`). The collision is still reachable — every
    # namespace made before that rule landed is exactly this shape — so the guard below must hold.
    # Going through the native API (not mkdir) is what makes the namespace VISIBLE to the existence
    # probe the guard uses; a bare directory is not a namespace to it.
    from lance_namespace import CreateNamespaceRequest

    from catalog.services import native

    native.call(client.app.state.namespace, "create_namespace", CreateNamespaceRequest(id=["shared"]))

    # Binding "shared" to the warehouse would route shared$* to the warehouse bucket and ORPHAN the
    # default-root tables — the collision guard must reject it with 409.
    r = client.post("/v1/warehouses/wh-a/namespaces", json={"namespace": "shared"})
    assert r.status_code == 409, r.text
    assert "orphan" in r.text.lower()


# --------------------------------------------------------------------------- #
# diff2 F6 leg (c) — the warehouse binding must not outlive its namespace, and
# undrop must route the recovered subtree back to the SAME bucket.
# --------------------------------------------------------------------------- #


@pytest.fixture
def routing_recoverable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Path, Path, Path]]:
    """`routing` with a GRACE PERIOD, so a cascade DETACHES instead of destroying (#96)."""
    monkeypatch.setenv("LANCE_TRASH_GRACE_DAYS", "7")
    yield from _routing_app(tmp_path, monkeypatch)


def test_a_recoverable_cascade_unbinds_and_undrop_rebinds_the_same_warehouse(
    routing_recoverable: tuple[TestClient, Path, Path, Path],
) -> None:
    """THE ACCEPTANCE CRITERION for F6 leg (c), and its point is the SECOND half.

    The binding used to be KEPT on a recoverable drop so undrop could still route — which meant it
    outlived the namespace, and when the grace window expired the purge reclaimed the bytes and left
    the binding standing forever. Nothing could see it either: the reconciler's `dangling_bindings`
    detects a binding whose WAREHOUSE record is missing, and the warehouse is still there.

    So the drop now unbinds, and the root trash record carries `{warehouse_id, root_uri}` as the only
    surviving copy. The half that makes that safe rather than merely tidy is the undrop: `NamespaceDep`
    resolves BEFORE the handler body, so with the binding gone it has already fallen back to the
    estate's DEFAULT root — and rebuilding through it would silently re-create the whole subtree in the
    shared bucket. That is a tenant-isolation break, not an error, so it is asserted physically.
    """
    from service_kit.lakehouse import trash

    client, default_root, warehouse_root, registry = routing_recoverable
    control = f"file://{registry}"
    _register_warehouse(registry, warehouse_root)

    created = client.post(
        "/v1/table/tenantns$clips/create?mode=overwrite",
        content=_arrow(pa.table({"id": [1, 2, 3]})),
        headers=ARROW_STREAM,
    )
    assert created.status_code == 200, created.text
    assert str(warehouse_root) in created.json()["location"]

    dropped = client.post("/v1/namespace/tenantns/drop", json={"behavior": "cascade"})
    assert dropped.status_code == 200, dropped.text

    # NO LEAK: the binding is gone the moment the namespace is.
    assert wh_svc.binding_for_namespace(control, {}, "tenantns") is None, "the drop left the warehouse binding behind"
    # …and the root record is the only place it now lives.
    root_record = trash.get(control, {}, "tenantns", kind="namespace")
    assert root_record is not None, "no recoverable drop was filed for the root"
    assert root_record.get("binding") == {"warehouse_id": "wh-a", "root_uri": str(warehouse_root)}

    # COLD CACHE, and without this the test is vacuous. `_resolve_warehouse_root` caches bindings
    # POSITIVELY AND FOREVER (#46), so inside one process `NamespaceDep` keeps resolving `tenantns` to
    # the warehouse root from a stale entry even after the unbind — which masks the whole reason the
    # undrop has to re-resolve. Clearing it is what a second replica, or this one after a restart,
    # actually sees. (Caught by the mutation pass: removing the re-resolve left the suite green.)
    client.app.state.warehouse_binding_cache.clear()

    recovered = client.post("/v1/namespace/tenantns/undrop", json={})
    assert recovered.status_code == 200, recovered.text

    # The binding is restored…
    assert wh_svc.binding_for_namespace(control, {}, "tenantns") == {
        "top_ns": "tenantns",
        "warehouse_id": "wh-a",
        "root_uri": str(warehouse_root),
    }
    # …and the recovered table is physically back in the WAREHOUSE bucket, not the shared default one.
    described = client.post("/v1/table/tenantns$clips/describe", json={})
    assert described.status_code == 200, described.text
    location = described.json()["location"]
    assert str(warehouse_root) in location, f"the recovered table did not route back to its warehouse: {location}"
    assert str(default_root) not in location, f"undrop rebuilt the subtree in the SHARED root: {location}"
    assert lance.dataset(location).count_rows() == 3


def test_undrop_refuses_when_the_id_was_bound_elsewhere_during_the_grace_window(
    routing_recoverable: tuple[TestClient, Path, Path, Path],
) -> None:
    """Re-binding is write-once at the store, and that refusal is load-bearing.

    If `tenantns` was re-bound to a DIFFERENT warehouse while the drop sat in the trash, restoring the
    recorded binding would hand this subtree someone else's bucket. `bind_namespace` refuses a
    conflicting binding (409) and undrop must surface that rather than route anywhere — failing loudly
    is the only safe answer, because the alternative is a silent cross-tenant write.
    """
    client, _default_root, warehouse_root, registry = routing_recoverable
    control = f"file://{registry}"
    _register_warehouse(registry, warehouse_root)
    assert client.post("/v1/namespace/tenantns/drop", json={"behavior": "cascade"}).status_code == 200

    # Somebody else claims the freed name against another warehouse.
    other = registry.parent / "wh-other"
    other.mkdir(exist_ok=True)
    wh_svc.put_warehouse(control, {}, {"id": "wh-b", "bucket": "wh-b", "root_uri": str(other), "project": "rival"})
    wh_svc.bind_namespace(control, {}, "tenantns", "wh-b", str(other))

    refused = client.post("/v1/namespace/tenantns/undrop", json={})
    assert refused.status_code == 409, refused.text
    # And the rival's binding is untouched — the refusal changed nothing.
    assert wh_svc.binding_for_namespace(control, {}, "tenantns") == {
        "top_ns": "tenantns",
        "warehouse_id": "wh-b",
        "root_uri": str(other),
    }


# --------------------------------------------------------------------------- #
# diff2 F10 item 3 — a TTL floor under the forever-positive binding cache
# --------------------------------------------------------------------------- #


def test_a_stale_binding_cache_entry_expires_instead_of_lasting_until_restart(
    routing: tuple[TestClient, Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache is POSITIVE-FOREVER, and #46's broadcast eviction is best-effort.

    A binding is immutable, so caching a resolved one forever is sound — until one of the three
    mutations that break that premise happens and the control event announcing it is dropped. That
    broadcast rides a pub/sub subscription with no dead-lettering, so a lost event leaves an entry
    nothing will ever evict, and the consequence lasts for the life of the PROCESS.

    Live warehouse-status reads mean the stale entry cannot route at a deleted bucket (the finding's
    own text corrects that), so what the TTL bounds is the rest: persistent 403s on a since-re-bound
    namespace, and wrong-bucket routing under warehouse-id reuse.

    Asserted through the resolver rather than the dict, so it is the ROUTING that recovers, not just
    a key that disappeared.
    """
    from catalog.api import dependencies as deps

    client, _default_root, warehouse_root, registry = routing
    _register_warehouse(registry, warehouse_root)

    # Warm the cache through a real request.
    assert client.post("/v1/namespace/tenantns/describe", json={}).status_code in (200, 404)
    cache = client.app.state.warehouse_binding_cache
    assert "tenantns" in cache, "the resolver did not cache the binding — this test proves nothing"

    # A mutation lands whose control event is LOST: the binding is rewritten behind the replica's back.
    other = registry.parent / "wh-moved"
    other.mkdir(exist_ok=True)
    wh_svc.unbind_namespace(f"file://{registry}", {}, "tenantns")
    wh_svc.put_warehouse(f"file://{registry}", {}, {"id": "wh-m", "bucket": "wh-m", "root_uri": str(other), "project": "acme"})
    wh_svc.bind_namespace(f"file://{registry}", {}, "tenantns", "wh-m", str(other))

    # Without a TTL the replica keeps the old entry forever. Advance past the window.
    real_monotonic = time.monotonic
    monkeypatch.setattr(deps.time, "monotonic", lambda: real_monotonic() + 10_000)

    resolved = client.app.state.warehouse_binding_cache
    assert deps._fresh_cached_binding(resolved, "tenantns", 300.0) is None, "the stale entry outlived its TTL"
    assert "tenantns" not in resolved, "the expired entry was not evicted"


def test_the_ttl_can_be_disabled_and_then_the_entry_is_kept(routing: tuple[TestClient, Path, Path, Path]) -> None:
    """`0` restores the pre-F10.3 forever-positive behaviour, deliberately — the knob is an escape
    hatch for an estate that would rather pay the 403s than the extra registry read."""
    from catalog.api import dependencies as deps

    client, _default_root, warehouse_root, registry = routing
    _register_warehouse(registry, warehouse_root)
    assert client.post("/v1/namespace/tenantns/describe", json={}).status_code in (200, 404)

    cache = client.app.state.warehouse_binding_cache
    assert deps._fresh_cached_binding(cache, "tenantns", 0.0) is not None
    assert "tenantns" in cache
