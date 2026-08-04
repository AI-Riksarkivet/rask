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
from collections.abc import Iterator
from pathlib import Path

import lance
import pyarrow as pa
import pytest
from catalog.services import warehouses as wh_svc
from fastapi.testclient import TestClient
from lance_namespace import CreateNamespaceRequest, connect


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
    from catalog.services import native
    from lance_namespace import CreateNamespaceRequest

    native.call(client.app.state.namespace, "create_namespace", CreateNamespaceRequest(id=["shared"]))

    # Binding "shared" to the warehouse would route shared$* to the warehouse bucket and ORPHAN the
    # default-root tables — the collision guard must reject it with 409.
    r = client.post("/v1/warehouses/wh-a/namespaces", json={"namespace": "shared"})
    assert r.status_code == 409, r.text
    assert "orphan" in r.text.lower()
