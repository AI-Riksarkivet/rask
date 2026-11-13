"""The routing a request's first segment decides ([[LH-593]], ADR 0206): a real app, real directory catalogs, the resolver not overridden.

A warehouse is the first segment of every identifier, so ``wh-a$bronze$clips`` is served from warehouse ``wh-a``'s own
root and its own directory catalog. The control-plane doors (create, list, get) are ``test_warehouses.py``'s.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Iterator
from pathlib import Path

import boto3
import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from lance_namespace import CreateNamespaceRequest, connect
from moto.server import ThreadedMotoServer

from catalog.core.identifiers import with_minted_uuid
from catalog.services import warehouses as wh_svc
from lakehouse.storage_options import StorageOptions
from tests.integration import store_requests


ARROW_STREAM = {"content-type": "application/vnd.apache.arrow.stream"}


def _arrow(table: pa.Table) -> bytes:
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, table.schema) as w:
        w.write_table(table)
    return sink.getvalue()


@pytest.fixture
def routing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Path, Path, Path]]:
    """A real app over a real estate root and a local control root. Yields (client, estate_root, warehouse_root, registry)."""
    yield from _routing_app(tmp_path, monkeypatch)


def _routing_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str) -> Iterator[tuple[TestClient, Path, Path, Path]]:
    """The fixture body, shared so a variant sets extra env without a second app build. ``env`` is set after the
    defaults, so a variant may move the control root."""
    estate_root = tmp_path / "default"
    warehouse_root = tmp_path / "wh-bucket"
    registry = tmp_path / "registry"
    for d in (estate_root, warehouse_root, registry):
        d.mkdir()

    monkeypatch.setenv("LANCE_REST_IMPL", "dir")
    monkeypatch.setenv("LANCE_REST_ROOT", str(estate_root))
    monkeypatch.setenv("LANCE_CONTROL_ROOT", f"file://{registry}")
    monkeypatch.setenv("LANCE_S3_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("LANCE_S3_SECRET_ACCESS_KEY", "x")
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app) as client:
        yield client, estate_root, warehouse_root, registry
    get_settings.cache_clear()


def _register_warehouse(registry: Path, warehouse_root: Path, *, wid: str = "wh-a", control: str | None = None, options: StorageOptions | None = None) -> None:
    """Register warehouse ``wid`` on a LOCAL root the resolver opens in-process, with its own row in that root's catalog
    and a namespace ``bronze`` beneath it, as the create door leaves a warehouse and CreateNamespace a namespace. The
    record goes to ``registry``, or to the control root ``control`` read with ``options``."""
    control = control or f"file://{registry}"
    options = options or StorageOptions()
    wh_svc.put_warehouse(control, options, {"id": wid, "bucket": wid, "root_uri": str(warehouse_root), "project": "acme"})
    wh_ns = connect("dir", {"root": str(warehouse_root)})
    wh_ns.create_namespace(CreateNamespaceRequest(id=[wid], properties=with_minted_uuid(None)))
    wh_ns.create_namespace(CreateNamespaceRequest(id=[wid, "bronze"], properties=with_minted_uuid(None)))


def _created(client: TestClient) -> int:
    return client.post("/v1/table/wh-a$bronze$clips/create", content=_arrow(pa.table({"id": [1, 2, 3]})), headers=ARROW_STREAM).status_code


def _declared(client: TestClient) -> int:
    return client.post("/v1/table/wh-a$bronze$clips/declare", json={}).status_code


def _directories(root: Path) -> list[str]:
    """The directories a root holds beside its ``__manifest``: one per table the catalog laid out there."""
    return sorted(child.name for child in root.iterdir() if child.is_dir() and not child.name.startswith("__"))


@pytest.mark.parametrize("make", [pytest.param(_created, id="create"), pytest.param(_declared, id="declare")])
def test_a_table_the_catalog_makes_lives_in_its_warehouse_root_under_its_uuid(
    routing: tuple[TestClient, Path, Path, Path], make: Callable[[TestClient], int]
) -> None:
    """[[LH-520]] A table the catalog creates or declares lives at ``<warehouse root>/<table uuid>`` (ADR 0173 §5).

    Through the real resolver, which routes the identifier's first segment to that warehouse's root and never to the
    estate's own. The directory is the uuid the table's describe answers and nothing else: no part of the table's name
    reaches the store.
    """
    client, estate_root, warehouse_root, registry = routing
    _register_warehouse(registry, warehouse_root)
    assert make(client) == 200

    described = client.post("/v1/table/wh-a$bronze$clips/describe", json={})

    assert described.status_code == 200, described.text
    table_uuid = described.json()["properties"]["rask.uuid"]
    assert Path(described.json()["location"].removeprefix("file://")) == warehouse_root / table_uuid, described.json()
    assert (_directories(warehouse_root), _directories(estate_root)) == ([table_uuid], [])


def test_two_warehouses_each_hold_the_same_names_and_no_binding_is_recorded(routing: tuple[TestClient, Path, Path, Path]) -> None:
    """[[LH-593]] A name belongs to its warehouse: ``wh-a$bronze$t`` and ``wh-b$bronze$t`` are two tables in two roots,
    and nothing on the control root binds a name to a warehouse."""
    client, _estate_root, warehouse_root, registry = routing
    other_root = registry.parent / "wh-b-bucket"
    other_root.mkdir()
    _register_warehouse(registry, warehouse_root)
    _register_warehouse(registry, other_root, wid="wh-b")
    for warehouse, rows in (("wh-a", [1]), ("wh-b", [1, 2])):
        made = client.post(f"/v1/table/{warehouse}$bronze$t/create", content=_arrow(pa.table({"id": rows})), headers=ARROW_STREAM)
        assert made.status_code == 200, made.text

    described = {warehouse: client.post(f"/v1/table/{warehouse}$bronze$t/describe", json={}).json() for warehouse in ("wh-a", "wh-b")}

    held = {
        warehouse: (Path(answer["location"].removeprefix("file://")).parent, lance.dataset(answer["location"]).count_rows())
        for warehouse, answer in described.items()
    }
    assert held == {"wh-a": (warehouse_root, 1), "wh-b": (other_root, 2)}
    assert sorted(path.name for path in (registry / "_warehouses").iterdir()) == ["wh-a.json", "wh-b.json"]


def test_deactivate_quarantines_then_activate_restores(routing: tuple[TestClient, Path, Path, Path]) -> None:
    client, _estate_root, warehouse_root, registry = routing
    _register_warehouse(registry, warehouse_root)

    def _create(name: str) -> int:
        return client.post(
            f"/v1/table/wh-a$bronze${name}/create?mode=overwrite",
            content=_arrow(pa.table({"id": [1]})),
            headers=ARROW_STREAM,
        ).status_code

    # Active → a create routes and succeeds.
    assert _create("t_before") == 200

    # Deactivate → the resolver quarantines EVERY op under the warehouse (403), so no new table lands.
    d = client.post("/management/v1/warehouses/wh-a/deactivate")
    assert d.status_code == 200, d.text
    assert d.json()["status"] == "deactivated"
    blocked = client.post(
        "/v1/table/wh-a$bronze$t_during/create?mode=overwrite",
        content=_arrow(pa.table({"id": [1]})),
        headers=ARROW_STREAM,
    )
    assert blocked.status_code == 403, blocked.text
    assert "deactivated" in blocked.text.lower()

    # Activate → routing is restored; a create succeeds again (status is read live).
    a = client.post("/management/v1/warehouses/wh-a/activate")
    assert a.status_code == 200 and a.json()["status"] == "active"
    assert _create("t_after") == 200


@pytest.fixture
def routing_recoverable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, Path, Path, Path]]:
    """`routing` with a GRACE PERIOD, so a cascade DETACHES instead of destroying (#96)."""
    monkeypatch.setenv("LANCE_TRASH_GRACE_DAYS", "7")
    yield from _routing_app(tmp_path, monkeypatch)


def test_an_undropped_namespace_is_rebuilt_in_its_own_warehouse(routing_recoverable: tuple[TestClient, Path, Path, Path]) -> None:
    """A recoverable cascade's undrop rebuilds the subtree in the warehouse its name routes to, never in the estate's
    own root: rebuilding there would be a tenant-isolation break with no error, so it is asserted physically."""
    client, estate_root, warehouse_root, registry = routing_recoverable
    _register_warehouse(registry, warehouse_root)
    created = client.post("/v1/table/wh-a$bronze$clips/create?mode=overwrite", content=_arrow(pa.table({"id": [1, 2, 3]})), headers=ARROW_STREAM)
    assert created.status_code == 200, created.text
    dropped = client.post("/v1/namespace/wh-a$bronze/drop", json={"behavior": "cascade"})
    assert dropped.status_code == 200, dropped.text

    recovered = client.post("/management/v1/namespace/wh-a$bronze/undrop", json={})

    assert recovered.status_code == 200, recovered.text
    described = client.post("/v1/table/wh-a$bronze$clips/describe", json={})
    assert described.status_code == 200, described.text
    location = described.json()["location"]
    assert (str(warehouse_root) in location, _directories(estate_root), lance.dataset(location).count_rows()) == (True, [], 3), location


def test_a_table_registered_in_a_warehouse_is_then_described(routing: tuple[TestClient, Path, Path, Path]) -> None:
    """[[LH-463]] `RegisterTable` answers for the table it registered: describe and list find it afterwards."""
    client, _estate_root, warehouse_root, registry = routing
    _register_warehouse(registry, warehouse_root)
    registered = client.post("/v1/table/wh-a$bronze$events/register", json={"location": "medallion/bronze"})

    assert registered.status_code == 200, registered.text
    described = client.post("/v1/table/wh-a$bronze$events/describe", json={})
    listed = client.get("/v1/namespace/wh-a$bronze/table/list")
    assert (described.status_code, listed.json().get("tables")) == (200, ["events"]), (described.text, listed.text)


@pytest.fixture
def registry_store() -> Iterator[store_requests.CountedStore]:
    """An S3 store holding the bucket ``registry``, behind a proxy counting what reaches it."""
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    url = f"http://{host}:{port}"
    boto3.client("s3", endpoint_url=url, aws_access_key_id="x", aws_secret_access_key="x", region_name="us-east-1").create_bucket(Bucket="registry")
    with store_requests.counted(url) as store:
        yield store
    server.stop()


@pytest.fixture
def routing_on_a_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registry_store: store_requests.CountedStore
) -> Iterator[tuple[TestClient, Path, Path, Path]]:
    """`routing` with its control root, and so its warehouse records, on ``registry_store``."""
    yield from _routing_app(tmp_path, monkeypatch, LANCE_CONTROL_ROOT="s3://registry", LANCE_S3_ENDPOINT=registry_store.url)


def test_a_listing_reads_its_warehouse_record_once(
    routing_on_a_store: tuple[TestClient, Path, Path, Path], registry_store: store_requests.CountedStore
) -> None:
    """[[LH-576]] E10h: a request resolves its warehouse once, so a listing judging each of its tables
    through that warehouse reads the warehouse's record once, where it read it once per listed table. Counted on the
    wire in front of the control root's store; the record is still read on every request, so a deactivation is in force
    from the next one."""
    from catalog.core.config import get_settings

    client, _estate_root, warehouse_root, registry = routing_on_a_store
    _register_warehouse(registry, warehouse_root, control="s3://registry", options=get_settings().storage_options())
    for name in ("a", "b", "c", "d"):
        assert client.post(f"/v1/table/wh-a$bronze${name}/create", content=_arrow(pa.table({"id": [1]})), headers=ARROW_STREAM).status_code == 200
    registry_store.taken()

    listed = client.get("/v1/namespace/wh-a$bronze/table/list", params={"include_declared": "false"})

    assert (listed.status_code, listed.json()["tables"]) == (200, ["a", "b", "c", "d"]), listed.text
    record_reads = [request for request in registry_store.taken() if request.method == "GET" and request.path.endswith("/wh-a.json")]
    assert len(record_reads) == 1, f"the listing read its warehouse record {len(record_reads)} times"


def test_a_name_whose_first_segment_is_no_warehouse_reaches_nothing_the_estate_root_holds(routing: tuple[TestClient, Path, Path, Path]) -> None:
    """A first segment naming no warehouse is served from no catalog at all. The estate's own root may hold a row under
    that segment (a namespace made before names began with a warehouse, or planted past the doors), and a name routed
    there would reach it and every table beneath it."""
    client, estate_root, _warehouse_root, _registry = routing
    estate = connect("dir", {"root": str(estate_root)})
    estate.create_namespace(CreateNamespaceRequest(id=["stray"], properties=with_minted_uuid(None)))
    estate.create_namespace(CreateNamespaceRequest(id=["stray", "sales"], properties=with_minted_uuid(None)))

    answers = (
        client.post("/v1/namespace/stray$sales/describe", json={}),
        client.get("/v1/namespace/stray/list"),
        client.post("/v1/table/stray$sales$t/declare", json={}),
    )

    assert [(answer.status_code, answer.json()["code"]) for answer in answers] == [(404, 1), (404, 1), (404, 1)], [a.text for a in answers]
