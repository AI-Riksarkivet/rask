"""Neither the purge nor a destructive drop deletes bytes another live table id resolves to ([[LH-204]]).

A liveness check by object id alone purges a trash record whose id is absent from ``__manifest`` even
when another id is registered at the same location — the state a rename or a register leaves when it
attaches a dropped table's bytes under a new id — and so deletes a live table. The purge judges a table by
its location ([[LH-525]]), so a live table at the recorded location is the dropped one still registered, under
whatever name. A destructive drop deletes
a location too, so an alias over another table's bytes must not destroy them.

Driven on a real pylance ``dir`` estate: through the real purge with a real trash record on a local
control root, and through the real catalog app.
"""

from __future__ import annotations

import asyncio
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import pyarrow as pa
import pytest
import respx
from fastapi.testclient import TestClient
from lance_namespace import CreateNamespaceRequest, CreateTableRequest, DeregisterTableRequest, DescribeTableRequest, RegisterTableRequest, connect
from pydantic import SecretStr

from lakehouse import trash
from lakehouse.storage_options import StorageOptions
from maintenance.core.config import MaintenanceSettings
from maintenance.services.purge import purge_expired_trash
from maintenance.services.reconcile import CATEGORIES, ReconcileReport


CATALOG = "http://catalog.test"


def test_an_expired_record_whose_bytes_another_id_resolves_to_is_refused(tmp_path: Path) -> None:
    data = tmp_path / "data"
    control_root = f"file://{tmp_path / 'control'}"
    ns = connect("dir", {"root": str(data)})
    ns.create_namespace(CreateNamespaceRequest(id=["team"]))
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, pa.schema([("a", pa.int64())])) as writer:
        writer.write_table(pa.table({"a": [1, 2, 3]}))
    ns.create_table(CreateTableRequest(id=["team", "orders"]), sink.getvalue())
    location = str(ns.describe_table(DescribeTableRequest(id=["team", "orders"])).location)
    ns.deregister_table(DeregisterTableRequest(id=["team", "orders"]))
    record = trash.make_record(
        "team$orders", uuid=str(uuid4()), location=location, dropped_by="user:alice", grace_days=7, now=datetime.now(UTC) - timedelta(days=30)
    )
    # Released, as the catalog's release door leaves a record the purge may act on.
    trash.put(control_root, StorageOptions(), {**record, trash.RELEASED_AT: (datetime.now(UTC) - timedelta(days=1)).isoformat()})
    ns.register_table(RegisterTableRequest(id=["team", "renamed"], location=location.rstrip("/").rsplit("/", 1)[-1]))
    report = ReconcileReport(
        checked_at=datetime.now(UTC).isoformat(),
        counts=dict.fromkeys(CATEGORIES, 0),
        total=0,
    )
    settings = MaintenanceSettings(
        s3_bucket="lance-catalog", s3_secret_access_key=SecretStr("unit"), control_root=control_root, trash_purge_enabled=True, catalog_url=CATALOG
    )

    # The refusal under test is maintenance's own, which the catalog notes on the record ([[XC-084]]): no session is asked.
    with respx.mock() as door:
        door.post(url__regex=rf"^{CATALOG}/management/v1/trash/table/[^/]+/refusals").mock(return_value=httpx.Response(200, json={"attempts": 1}))
        out = asyncio.run(purge_expired_trash(settings, report=report, control_root=control_root, data_roots={f"file://{data}"}))

    assert out.purged == [], f"the purge deleted bytes table team$renamed resolves to: {out.purged}"
    assert [(refused.id, "still registered" in refused.reason) for refused in out.refused] == [("team$orders", True)], out.refused
    assert Path(location.removeprefix("file://"), "_versions").is_dir()
    assert [record["id"] for record in trash.named(control_root, StorageOptions(), "team$orders")] == ["team$orders"]


@pytest.mark.parametrize(
    ("door", "params", "body"),
    [
        pytest.param("/v1/table/other$alias/drop", {"purge": "true"}, None, id="table-drop"),
        pytest.param("/v1/namespace/other/drop", {"purge": "true"}, {"behavior": "Cascade"}, id="namespace-cascade"),
    ],
)
def test_a_destructive_drop_of_an_alias_spares_the_bytes_the_claim_holder_resolves_to(
    catalog: TestClient, tmp_path: Path, door: str, params: dict[str, str], body: dict[str, str] | None
) -> None:
    for name in ("team", "other"):
        assert catalog.post(f"/v1/namespace/{name}/create", json={}).status_code == 200
    sink = io.BytesIO()
    with pa.ipc.new_stream(sink, pa.schema([("a", pa.int64())])) as writer:
        writer.write_table(pa.table({"a": [1, 2, 3]}))
    created = catalog.post("/v1/table/team$src/create?mode=create", content=sink.getvalue(), headers={"content-type": "application/vnd.apache.arrow.stream"})
    assert created.status_code == 200, created.text
    assert catalog.post("/v1/table/team$src/rename", json={"new_table_name": "dst"}).status_code == 200
    location = str(catalog.post("/v1/table/team$dst/describe", json={}).json()["location"])
    connect("dir", {"root": str(tmp_path / "catalog")}).register_table(
        RegisterTableRequest(id=["other", "alias"], location=location.rstrip("/").rsplit("/", 1)[-1])
    )

    refused = catalog.post(door, params=params, json=body)

    assert refused.status_code == 409, refused.text
    assert Path(location.removeprefix("file://"), "_versions").is_dir(), "the drop destroyed bytes team$dst resolves to"
    assert catalog.post("/v1/table/team$dst/describe", json={}).status_code == 200
