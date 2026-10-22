"""[[LH-516]] Drift repair keeps the grants of a table in the trash, so its owner can still undrop it.

A recoverable drop deregisters its table and keeps the table's grants (#75): its owner is the person who must undrop it,
and undrop asks `can_drop` on the table. The grants end with the trash record, at a purge or an expiry. A table in the
trash is in no catalog listing, so only its trash record tells it apart from a ghost, a table whose grants outlived it.
Measured on the live estate at helm rev 18, the reconcile ticks at 18:04 and 18:09 UTC on 2026-10-20 each planned
`table:c2-a9687292$dropped-owner (2 tuples) <- ghost_tables` in their dry-run drift repair, for a table in the trash.

ONE ESTATE ON MOTO, TWO SERVICES: the real catalog app drops the table through its door, so the trash record is the one
that door writes, and the real maintenance app runs its reconcile tick over the same control root with drift repair
armed. One in-memory `TupleStore` stands behind both through the real `service_kit.governed.fga` functions. A ghost
beside the trashed table, a grant on a table nothing names, is the control: the tick must revoke it, so a pass that
revokes nothing cannot pass. A live table and a renamed one are no ghosts: a rename keeps the uuid its grants are held
by. Every object is a table's uuid (``table:<uuid>``, [[LH-524]]), and the trashed table's is
the one its record keeps.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from unittest.mock import MagicMock

import boto3
import httpx
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from catalog.core.config import get_settings as catalog_settings
from catalog.core.lineage_emit import HttpLineageEmitter
from maintenance.core.config import get_settings as maintenance_settings
from service_kit.governed.fga import user_object
from service_kit.lakehouse import trash
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from storage import S3Client
from tests.integration.openfga_grants import TupleStore, act_as, principal_key


PROJECT = "acme"
_ROWS = pa.table({"id": pa.array([1, 2], pa.int64())})


class _Estate:
    """Project ``acme``'s warehouse, driven by its admin through the catalog, and the maintenance service beside it."""

    def __init__(self, catalog: TestClient, maintenance: TestClient, store: TupleStore, warehouse: str) -> None:
        self.catalog = catalog
        self.maintenance = maintenance
        self.store = store
        self.warehouse = warehouse

    def bind(self, namespace: str) -> None:
        answer = self.catalog.post(f"/management/v1/warehouses/{self.warehouse}/namespaces", json={"namespace": namespace})
        assert answer.status_code == 200, answer.text

    def create_table(self, table: str) -> None:
        answer = self.catalog.post(f"/v1/table/{table}/create", content=encode_arrow_stream(_ROWS), headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
        assert answer.status_code == 200, answer.text

    def uuid_of(self, table: str) -> str:
        """The uuid the catalog minted for ``table``, which DescribeTable answers as ``rask.uuid``."""
        answer = self.catalog.post(f"/v1/table/{table}/describe", json={})
        assert answer.status_code == 200, answer.text
        return str(answer.json()["properties"]["rask.uuid"])

    def drop_table(self, table: str) -> None:
        answer = self.catalog.post(f"/v1/table/{table}/drop", json={})
        assert answer.status_code == 200, answer.text

    def in_trash(self, table: str) -> bool:
        settings = catalog_settings()
        return bool(trash.named(settings.registry_root, settings.storage_options(), table, kind="table"))


@pytest.fixture
def estate(moto_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Estate]:
    """The catalog with a seven-day grace period, OIDC and FGA on once its lifespan has run, as alice, and maintenance
    reading the same control root with drift repair armed; both ask one OpenFGA store."""
    control = f"lh516-{uuid.uuid4().hex[:10]}"
    warehouse = f"{control}-wh"
    s3: S3Client = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    s3.create_bucket(Bucket=control)
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{control}",
        "LANCE_CONTROL_ROOT": f"s3://{control}",
        "LANCE_S3_ENDPOINT": moto_url,
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "LANCE_WAREHOUSES_ENABLED": "true",
        "LANCE_TRASH_GRACE_DAYS": "7",
        "MAINTENANCE_S3_ENDPOINT": moto_url,
        "MAINTENANCE_S3_ACCESS_KEY_ID": "test",
        "MAINTENANCE_S3_SECRET_ACCESS_KEY": "test",
        "MAINTENANCE_S3_BUCKET": control,
        "MAINTENANCE_CONTROL_ROOT": f"s3://{control}",
        "MAINTENANCE_WAREHOUSES_ENABLED": "true",
        "MAINTENANCE_DRIFT_REPAIR_ENABLED": "true",
        "MAINTENANCE_DRIFT_REPAIR_DRY_RUN": "false",
    }.items():
        monkeypatch.setenv(key, value)
    catalog_settings.cache_clear()
    maintenance_settings.cache_clear()
    from catalog.main import app as catalog_app
    from maintenance.service import app as maintenance_app

    store = TupleStore()
    lineage = HttpLineageEmitter(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))), "http://lineage/api/v1/lineage", job_namespace="lance-catalog"
    )
    with TestClient(catalog_app, headers={"Authorization": "Bearer t"}) as catalog, TestClient(maintenance_app) as maintenance:
        governed = catalog_settings().model_copy(
            update={
                "oidc_enabled": True,
                "oidc_connectors": {"local": "dex"},
                "oidc_issuer": "https://idp.example",
                "oidc_audience": "lance",
                "fga_enabled": True,
            }
        )
        catalog_app.dependency_overrides[catalog_settings] = lambda: governed
        monkeypatch.setattr(catalog_app.state, "oidc", MagicMock())
        act_as(catalog, "alice")
        monkeypatch.setattr(catalog_app.state, "fga", store)
        monkeypatch.setattr(catalog_app.state, "lineage_emitter", lineage, raising=False)
        monkeypatch.setattr(maintenance_app.state, "fga_client", store)
        for path, body in (("/management/v1/projects", {"id": PROJECT}), ("/management/v1/warehouses", {"id": warehouse, "project": PROJECT})):
            answer = catalog.post(path, json=body)
            assert answer.status_code == 200, f"{path}: {answer.text}"
        yield _Estate(catalog, maintenance, store, warehouse)
    catalog_app.dependency_overrides.clear()
    catalog_settings.cache_clear()
    maintenance_settings.cache_clear()


def test_a_reconcile_tick_revokes_a_ghost_and_keeps_the_grants_of_a_table_in_the_trash(estate: _Estate) -> None:
    estate.bind("bronze")
    for table in ("bronze$kept", "bronze$dropped", "bronze$before"):
        estate.create_table(table)
    renamed = estate.catalog.post("/v1/table/bronze$before/rename", json={"new_table_name": "after"})
    assert renamed.status_code == 200, renamed.text
    dropped = f"table:{estate.uuid_of('bronze$dropped')}"
    estate.drop_table("bronze$dropped")
    gone = str(uuid.uuid4())
    estate.store.tuples.add((user_object(principal_key("alice")), "owner", f"table:{gone}"))
    held = estate.store.naming([dropped])
    assert held and estate.in_trash("bronze$dropped"), "the drop kept nothing for an undrop, so keeping it would pass vacuously"

    answer = estate.maintenance.post("/maintenance-reconcile-cron")

    assert answer.status_code == 200, answer.text
    tick = answer.json()
    assert tick["ghost_tables"] == [{"kind": "table", "id": gone, "fga_object": f"table:{gone}", "tuples": 1}]
    assert tick["drift_repair"]["revoked"] == [{"fga_object": f"table:{gone}", "tuples": 1, "justified_by": "ghost_tables"}]
    assert estate.store.naming([dropped, f"table:{gone}"]) == held, "the tick revoked a grant the undrop needs, or kept the ghost's"
