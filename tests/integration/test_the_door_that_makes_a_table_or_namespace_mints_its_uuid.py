"""[[LH-519]] Every door that brings a table or namespace into being gives it a uuid the catalog mints (ADR 0173 §2, §6, §11).

A table's uuid lives in its location's claim, outside Lance context, because RegisterTable keeps no properties
(measured on pylance 13.0.0: a `register_table` naming `properties` answers and describes none), and the rename and
both undrops re-register. A namespace's lives in its own `__manifest` row as the `rask.uuid` property. DescribeTable
and DescribeNamespace answer each as `rask.uuid`, and two describes answer the same one: the uuid is read, never
minted on the way out.

The real catalog app over a real `dir` namespace on pylance, with warehouses on as the chart deploys them, so a
top-level namespace is made through the warehouse door and a nested one through the namespace door.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from pathlib import Path

import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from catalog.api.dependencies import get_namespace
from catalog.core.config import Settings, get_settings
from catalog.services import warehouses
from catalog.services.provenance_namespace import ensure_provenance_namespace
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


WAREHOUSE = "acme-bucket"
_ROWS = pa.table({"id": pa.array([1, 2], pa.int64())})

#: Makes one table or namespace through its door and answers the path that describes it.
type Door = Callable[[TestClient, Settings], str]


@pytest.fixture
def estate(real_ns_client: TestClient, tmp_path: Path) -> tuple[TestClient, Settings]:
    """The catalog with warehouses on, warehouse ``WAREHOUSE`` at its root, and the top-level namespace ``acme`` in it."""
    settings = Settings.model_validate({"root": str(tmp_path), "warehouses_enabled": True, "s3_access_key_id": "x", "s3_secret_access_key": "x"})
    real_ns_client.app.dependency_overrides[get_settings] = lambda: settings
    warehouses.put_warehouse(
        str(tmp_path), {}, {"id": WAREHOUSE, "bucket": WAREHOUSE, "root_uri": str(tmp_path), "project": "acme", "status": "active", "created_at": "t"}
    )
    made = real_ns_client.post(f"/management/v1/warehouses/{WAREHOUSE}/namespaces", json={"namespace": "acme"})
    assert made.status_code == 200, made.text
    return real_ns_client, settings


def _created(client: TestClient, _settings: Settings) -> str:
    made = client.post("/v1/table/acme$t/create", content=encode_arrow_stream(_ROWS), headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
    assert made.status_code == 200, made.text
    return "/v1/table/acme$t/describe"


def _declared(client: TestClient, _settings: Settings) -> str:
    made = client.post("/v1/table/acme$t/declare", json={})
    assert made.status_code == 200, made.text
    return "/v1/table/acme$t/describe"


def _registered(client: TestClient, settings: Settings) -> str:
    lance.write_dataset(_ROWS, f"{settings.root}/outside", data_storage_version="2.2", enable_stable_row_ids=True)
    made = client.post("/v1/table/acme$t/register", json={"location": "outside"})
    assert made.status_code == 200, made.text
    return "/v1/table/acme$t/describe"


def _through_the_warehouse_door(client: TestClient, _settings: Settings) -> str:
    made = client.post(f"/management/v1/warehouses/{WAREHOUSE}/namespaces", json={"namespace": "beta"})
    assert made.status_code == 200, made.text
    return "/v1/namespace/beta/describe"


def _through_the_namespace_door(client: TestClient, _settings: Settings) -> str:
    made = client.post("/v1/namespace/acme$lane/create", json={})
    assert made.status_code == 200, made.text
    return "/v1/namespace/acme$lane/describe"


def _at_boot(client: TestClient, settings: Settings) -> str:
    """The provenance namespace, made by the function the catalog's lifespan runs at boot."""
    ns = client.app.dependency_overrides[get_namespace]()
    assert asyncio.run(ensure_provenance_namespace(ns, settings.model_copy(update={"provenance_namespace": "prov"}), None)) is True
    return "/v1/namespace/prov/describe"


@pytest.mark.parametrize(
    "door",
    [
        pytest.param(_created, id="table-create"),
        pytest.param(_declared, id="table-declare"),
        pytest.param(_registered, id="table-register"),
        pytest.param(_through_the_warehouse_door, id="namespace-warehouse-door"),
        pytest.param(_through_the_namespace_door, id="namespace-create"),
        pytest.param(_at_boot, id="namespace-provenance-at-boot"),
    ],
)
def test_the_door_that_makes_a_table_or_namespace_mints_its_uuid(estate: tuple[TestClient, Settings], door: Door) -> None:
    client, settings = estate
    describe = door(client, settings)

    answers = [client.post(describe, json={}) for _ in range(2)]

    assert [answer.status_code for answer in answers] == [200, 200], [answer.text for answer in answers]
    first, second = [(answer.json().get("properties") or {}).get("rask.uuid") for answer in answers]
    assert first is not None, f"{describe} answers no rask.uuid: {answers[0].json()}"
    minted = uuid.UUID(first)
    assert (str(minted), minted.version) == (first, 4), f"{first!r} is not a canonical random uuid"
    assert second == first, "a second describe answered another uuid"
