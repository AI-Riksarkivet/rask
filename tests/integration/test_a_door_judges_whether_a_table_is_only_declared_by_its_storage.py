"""A door that asks whether a table is only declared judges the table's own storage ([[LH-528]]).

The spec answers "only declared" by storage: ``include_declared=false`` lists "only tables with storage components"
(`lance_docs/ns_catalog/spec.yaml:2356-2367`), and ``is_only_declared`` is false for a table that has them
(`spec.yaml:2926-2934`). The `dir` backend judges it by probing ``_versions/`` under ``base_path.join(location)``, and
object_store's ``Path::join`` appends one segment, percent-encoding ``/`` and every non-ASCII byte, so for a table
registered at ``db-outside/held`` it probes a key that does not exist and answers that the table has no storage
(measured on pylance 13.0.0). A door that believes it leaves the table out of a listing, describes it as only declared
with none of its metadata, writes into it on a create and deregisters it on the rollback, and answers a rename 404.

A table holds a version when a committed manifest, ``{version}.manifest`` or ``{u64::MAX - version:020}.manifest``
(`lance_docs/lance/format/table/transaction.md` § Manifest Naming Schemes), lies under the location DescribeTable answers. A staged
``{version}.manifest-{uuid}`` (`lance_docs/ns_catalog/namespace/supported-catalogs/lance-dir.md:292`), which a refused
stock commit leaves behind, is no version.

The real catalog app over a real `dir` namespace on pylance. The backend's own detailed describe of the table's twin at a
single-segment location, which it judges right, is what a detailed describe of the nested table must answer.

A detailed describe judges a table by the open that loads its metadata, and a read of a table's version asks for no
credential, so lineage's readiness read of its feed lists the feed's ``_versions/`` once ([[LH-529]]): Lance finds the
latest version by that listing on every open, the store answers it with every manifest key, and the readiness read runs
every few seconds. Counted on the wire, in front of a moto S3 server, since Lance's and pyarrow's native clients go
through no Python hook.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3
import httpx
import lance
import numpy as np
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from lance.arrow import bfloat16_array
from lance_namespace import CreateNamespaceRequest, DescribeTableRequest, connect
from moto.server import ThreadedMotoServer
from pydantic import BaseModel, ConfigDict

from catalog.core.identifiers import with_minted_uuid
from catalog.services import warehouses
from lineage.services.provenance import ProvenanceStore
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from tests.integration import store_requests
from tests.integration.lineage_app import ensure_tables, serve


#: Where the table registered at a nested location lives, relative to its warehouse's root. A non-ASCII segment, which
#: DescribeTable answers percent-encoded and the store keeps decoded.
_NESTED = "db-outside/räksmörgås"


def _every_stored_type() -> pa.Table:
    """One row with a column of every Arrow type pylance 13.0.0 stores at file version 2.2, and every extension it keeps.

    Measured: ``decimal32``, ``decimal64``, ``interval`` and ``union`` are refused by Lance, so no table holds them. An
    extension Lance keeps is answered as its storage type with its name and parameters in the field's metadata, nested
    in a list or a struct too, and ``arrow.json`` is stored as ``lance.json``.
    """
    uuids = pa.array([uuid.uuid4().bytes], pa.uuid())
    columns = {
        "null": pa.array([None], pa.null()),
        "bool": pa.array([True]),
        "i8": pa.array([1], pa.int8()),
        "u8": pa.array([1], pa.uint8()),
        "i16": pa.array([1], pa.int16()),
        "u16": pa.array([1], pa.uint16()),
        "i32": pa.array([1], pa.int32()),
        "u32": pa.array([1], pa.uint32()),
        "i64": pa.array([1], pa.int64()),
        "u64": pa.array([1], pa.uint64()),
        "f16": pa.array(np.array([1.0], dtype=np.float16)),
        "f32": pa.array([1.0], pa.float32()),
        "f64": pa.array([1.0], pa.float64()),
        "d128": pa.array([Decimal("1.25")], pa.decimal128(20, 2)),
        "d256": pa.array([Decimal("1.25")], pa.decimal256(40, 2)),
        "date32": pa.array([dt.date(2026, 1, 1)], pa.date32()),
        "date64": pa.array([dt.date(2026, 1, 1)], pa.date64()),
        "time32": pa.array([dt.time(1, 2, 3)], pa.time32("s")),
        "time64": pa.array([dt.time(1, 2, 3)], pa.time64("us")),
        "ts": pa.array([dt.datetime(2026, 1, 1)], pa.timestamp("us")),
        "tstz": pa.array([dt.datetime(2026, 1, 1)], pa.timestamp("us", tz="UTC")),
        "dur": pa.array([dt.timedelta(seconds=1)], pa.duration("s")),
        "utf8": pa.array(["a"]),
        "lutf8": pa.array(["a"], pa.large_string()),
        "bin": pa.array([b"a"]),
        "lbin": pa.array([b"a"], pa.large_binary()),
        "fsb": pa.array([b"ab"], pa.binary(2)),
        "list": pa.array([[1]], pa.list_(pa.int32())),
        "llist": pa.array([[1]], pa.large_list(pa.int32())),
        "fsl": pa.array([[1.0, 2.0]], pa.list_(pa.float32(), 2)),
        "struct": pa.StructArray.from_arrays(
            [pa.array([1], pa.int32()), uuids], fields=[pa.field("a", pa.int32(), metadata={"unit": "m"}), pa.field("u", pa.uuid())]
        ),
        "uuids": pa.LargeListArray.from_arrays(pa.array([0, 1], pa.int64()), uuids),
        "dict": pa.array(["a"]).dictionary_encode(),
        "map": pa.array([[("k", 1)]], pa.map_(pa.string(), pa.int32())),
        "blob": lance.blob_array([b"payload"]),
        "uuid": uuids,
        "tensor": pa.FixedShapeTensorArray.from_numpy_ndarray(np.ones((1, 2, 2), dtype=np.float32)),
        "json": pa.array(['{"a": 1}'], pa.json_()),
        "bool8": pa.array([1], pa.bool8()),
        "opaque": pa.ExtensionArray.from_storage(pa.opaque(pa.binary(), "t", "v"), pa.array([b"x"])),
        "bf16": bfloat16_array([1.0]),
    }
    return pa.table(columns).replace_schema_metadata({"owner": "pipeline"})


class Held(BaseModel):
    """The catalog over a real `dir` root, holding the tables every case asks about."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: TestClient
    #: Where each table lives, as DescribeTable answers it, by the name it answers to after a case: a rename keeps the location.
    locations: dict[str, str]
    #: The backend's own detailed describe of the twin at a single-segment location.
    twin_schema: dict[str, Any]


def _ok(client: TestClient, path: str, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401 — the request's own keywords
    answer = client.post(path, **kwargs)
    assert answer.status_code == 200, f"{path}: {answer.text}"
    return answer.json()


@pytest.fixture
def held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Held]:
    """The catalog as the estate runs it, warehouses on: ``db`` is a top-level namespace bound to a warehouse whose root is a
    directory of its own, never the catalog's default root. In it ``db$held`` is registered over data at a nested location,
    ``db$twin`` over the same data at a single-segment one, ``db$decl`` is declared, and ``db$staged`` is declared with a
    first commit staged and never finalized."""
    default_root, root, registry = tmp_path / "default", tmp_path / "warehouse", tmp_path / "registry"
    for directory in (default_root, root, registry):
        directory.mkdir()
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": str(default_root),
        "LANCE_WAREHOUSES_ENABLED": "true",
        "LANCE_CONTROL_ROOT": f"file://{registry}",
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
    }.items():
        monkeypatch.setenv(key, value)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app) as client:
        # The warehouse and its top-level namespace, as the warehouse door leaves them: the namespace lives in its root.
        warehouses.put_warehouse(f"file://{registry}", StorageOptions(), {"id": "wh", "bucket": "wh", "root_uri": str(root), "project": "acme"})
        connect("dir", {"root": str(root)}).create_namespace(CreateNamespaceRequest(id=["db"], properties=with_minted_uuid(None)))
        warehouses.bind_namespace(f"file://{registry}", StorageOptions(), "db", "wh", str(root))
        for location in (_NESTED, "twin"):
            lance.write_dataset(_every_stored_type(), str(root / location), data_storage_version="2.2", enable_stable_row_ids=True)
        _ok(client, "/v1/table/db$held/register", json={"location": _NESTED})
        _ok(client, "/v1/table/db$twin/register", json={"location": "twin"})
        _ok(client, "/v1/table/db$decl/declare", json={})
        staged = Path(_ok(client, "/v1/table/db$staged/declare", json={})["location"].removeprefix("file://"))
        # A refused stock commit leaves its manifest staged under its version and a uuid; the bytes are a manifest Lance wrote.
        (staged / "_versions").mkdir()
        (staged / "_versions" / f"1.manifest-{uuid.uuid4()}").write_bytes(next((root / "twin" / "_versions").glob("*.manifest")).read_bytes())
        twin = connect("dir", {"root": str(root)}).describe_table(DescribeTableRequest(id=["db", "twin"], load_detailed_metadata=True))
        assert twin.var_schema is not None, "precondition: the backend loads the twin's metadata"
        nested = _ok(client, "/v1/table/db$held/describe", json={})["location"]
        yield Held(
            client=client,
            locations={"held": nested, "moved": nested, "staged": _ok(client, "/v1/table/db$staged/describe", json={})["location"]},
            twin_schema=twin.var_schema.model_dump(by_alias=True, exclude_none=True),
        )
    get_settings.cache_clear()


_ROWS = encode_arrow_stream(pa.table({"id": pa.array([1], pa.int64())}))


_EXIST_OK = {"params": {"mode": "exist_ok"}, "content": _ROWS, "headers": {"Content-Type": ARROW_STREAM_MEDIA_TYPE}}


@pytest.mark.parametrize(
    ("method", "path", "sent", "status", "answers", "named"),
    [
        pytest.param(
            "GET",
            "/v1/namespace/db/table/list",
            {"params": {"include_declared": "false"}},
            200,
            lambda _: {"tables": ["held", "twin"]},
            "held",
            id="list-tables",
        ),
        pytest.param(
            "GET", "/v1/table", {"params": {"include_declared": "false"}}, 200, lambda _: {"tables": ["db$held", "db$twin"]}, "held", id="list-all-tables"
        ),
        pytest.param(
            "POST",
            "/v1/table/db$held/describe",
            {"params": {"check_declared": "true"}, "json": {}},
            200,
            lambda _: {"is_only_declared": False},
            "held",
            id="describe",
        ),
        pytest.param(
            "POST",
            "/v1/table/db$held/describe",
            {"params": {"load_detailed_metadata": "true"}, "json": {}},
            200,
            lambda held: {"is_only_declared": False, "version": 1, "schema": held.twin_schema},
            "held",
            id="describe-detailed",
        ),
        pytest.param("POST", "/v1/table/db$held/create", _EXIST_OK, 200, lambda _: {"version": 1}, "held", id="create-exist-ok"),
        pytest.param("POST", "/v1/table/db$held/rename", {"json": {"id": ["db", "held"], "new_table_name": "moved"}}, 200, lambda _: {}, "moved", id="rename"),
        pytest.param(
            "POST",
            "/v1/table/db$staged/describe",
            {"params": {"check_declared": "true"}, "json": {}},
            200,
            lambda _: {"is_only_declared": True},
            "staged",
            id="describe-a-declaration",
        ),
        pytest.param(
            "POST",
            "/v1/table/db$staged/describe",
            {"params": {"load_detailed_metadata": "true"}, "json": {}},
            200,
            lambda _: {"is_only_declared": True, "version": None, "schema": None},
            "staged",
            id="describe-a-declaration-detailed",
        ),
        pytest.param("POST", "/v1/table/db$staged/create", _EXIST_OK, 200, lambda _: {"version": 1}, "staged", id="create-exist-ok-into-a-declaration"),
        pytest.param(
            "POST",
            "/v1/table/db$staged/rename",
            {"json": {"id": ["db", "staged"], "new_table_name": "moved"}},
            404,
            lambda _: {"code": 4},
            "staged",
            id="rename-a-declaration",
        ),
    ],
)
def test_a_door_answers_a_table_by_its_own_storage(
    held: Held, method: str, path: str, sent: dict[str, Any], status: int, answers: Callable[[Held], dict[str, Any]], named: str
) -> None:
    """Each door answers the nested table as one holding a version and the staged declaration as one holding none, and
    each table still lives at its location after."""
    answer = held.client.request(method, path, **sent)

    assert answer.status_code == status, answer.text
    expected = answers(held)
    assert {key: answer.json().get(key) for key in expected} == expected
    assert _ok(held.client, f"/v1/table/db${named}/describe", json={})["location"] == held.locations[named]


@pytest.fixture
def feed_on_a_counted_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[ProvenanceStore, store_requests.CountedStore, str]]:
    """Lineage's store over the live catalog, whose bucket on a moto S3 server it reaches through a proxy counting what
    the store is asked; answers the store, the count, and where the feed table lives."""
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    s3 = boto3.client("s3", endpoint_url=f"http://{host}:{port}", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")
    s3.create_bucket(Bucket="counted")
    with store_requests.counted(f"http://{host}:{port}") as store:
        for key, value in {
            "LANCE_REST_IMPL": "dir",
            "LANCE_REST_ROOT": "s3://counted",
            "LANCE_S3_ENDPOINT": store.url,
            "LANCE_S3_ACCESS_KEY_ID": "test",
            "LANCE_S3_SECRET_ACCESS_KEY": "test",
            "LANCE_PROVENANCE_NAMESPACE": "provenance",
        }.items():
            monkeypatch.setenv(key, value)
        from catalog.core.config import get_settings

        get_settings.cache_clear()
        from catalog.main import app

        with serve(app) as url:
            provenance = ProvenanceStore(catalog_url=url, namespace=["provenance"], token=lambda: None, refuse_foreign_runs=False)
            ensure_tables(provenance)
            with httpx.Client(base_url=url, timeout=60) as catalog:
                feed = catalog.post("/v1/table/provenance$events/describe", json={"vend_credentials": False}).json()["location"]
            yield provenance, store, feed
        get_settings.cache_clear()
    server.stop()


def test_lineages_read_of_its_feeds_version_lists_the_feeds_versions_once(
    feed_on_a_counted_store: tuple[ProvenanceStore, store_requests.CountedStore, str],
) -> None:
    provenance, store, feed = feed_on_a_counted_store
    versions = f"{feed.removeprefix('s3://counted/')}/_versions/"
    store.taken()

    asyncio.run(provenance.ready())

    assert sum(request.lists(versions) for request in store.taken()) == 1
