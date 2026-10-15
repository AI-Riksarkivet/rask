"""[[LH-495]] A warehouse cascade revokes a grant only once its object is gone, and a re-issue finishes what a failure left.

A cascade that revokes before it removes strands live data. A namespace drop on the `dir` backend is Restrict, so a
namespace still holding a table refuses it, and every table under it has by then lost the grants its owner reads it
through. Measured on the live estate 2026-10-15: `lh467-8ba7d771-bronze$t`, still in its namespace, answered 403
`can_get_metadata required` to the estate admin who owns it through that namespace, and the reconciler reported it in
`ungoverned_tables`. The same reconcile reported 17 `ghost_tables`, each a scratch tenant's table that a recoverable
drop had detached: the grants that drop keeps for an undrop (#75) outlived the trash record the warehouse delete
clears with the warehouse.

ONE TENANT ON MOTO: the real catalog app, its warehouse bucket provisioned through the create door, its namespaces bound
through the warehouse door, each table created through the namespace doors on real pylance, and the in-memory
`TupleStore` behind the real `service_kit.governed.fga` seed and revoke. Every check is allowed, because who may act is
not the claim: the tuples each call leaves behind are. A table's tuples are what its owner reads it through
(`model.fga.yaml` pins that a project's admin owns a table through its parent edges).
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Iterator
from typing import Any, override
from unittest.mock import MagicMock

import boto3
import httpx
import pyarrow as pa
import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from openfga_sdk.client.models import ClientWriteRequest

from catalog.core.config import get_settings
from catalog.core.lineage_emit import HttpLineageEmitter
from catalog.services import warehouses
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from storage import S3Client
from tests.integration.openfga_grants import Options, TupleStore, act_as


PROJECT = "acme"
_ROWS = pa.table({"id": pa.array([1, 2], pa.int64())})


class _Outage(TupleStore):
    """The store, refusing every revoke of ``down`` as an OpenFGA that stopped answering does, while ``down`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.down: str | None = None

    @override
    async def write(self, body: ClientWriteRequest, options: Options = None) -> None:
        if self.down is not None and any(self.down in (t.user, t.object) for t in body.deletes or []):
            raise OSError("openfga did not answer")
        await super().write(body, options)


class _Tenant:
    """Project ``acme`` and its warehouse on a bucket of its own, driven by the project's admin."""

    def __init__(self, client: TestClient, store: _Outage, posted: list[dict[str, Any]], s3: S3Client, warehouse: str) -> None:
        self.client = client
        self.store = store
        self.posted = posted
        self.s3 = s3
        self.warehouse = warehouse

    def bind(self, namespace: str) -> None:
        answer = self.client.post(f"/management/v1/warehouses/{self.warehouse}/namespaces", json={"namespace": namespace})
        assert answer.status_code == 200, answer.text

    def create_namespace(self, namespace: str) -> None:
        answer = self.client.post(f"/v1/namespace/{namespace}/create", json={})
        assert answer.status_code == 200, answer.text

    def create_table(self, table: str) -> None:
        answer = self.client.post(f"/v1/table/{table}/create", content=encode_arrow_stream(_ROWS), headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
        assert answer.status_code == 200, answer.text

    def delete_warehouse(self) -> httpx.Response:
        return self.client.delete(f"/management/v1/warehouses/{self.warehouse}", params={"cascade": "true", "purge_bucket": "true"})

    def present(self, objects: list[str]) -> list[str]:
        """Those of ``objects`` the catalog still serves, each asked through its own door."""
        served: list[str] = []
        for obj in objects:
            kind, _, name = obj.partition(":")
            answer = self.client.get(f"/management/v1/warehouses/{name}") if kind == "warehouse" else self.client.post(f"/v1/{kind}/{name}/exists", json={})
            assert answer.status_code in (200, 404), f"{obj}: {answer.status_code} {answer.text}"
            if answer.status_code == 200:
                served.append(obj)
        return served

    def announced_drops(self) -> list[str]:
        """The tables lineage was told stopped existing, in the order it was told."""
        return [str(event["dataset"]["name"]) for event in self.posted if _lifecycle(event) == "DROP"]

    def bucket_exists(self) -> bool:
        try:
            self.s3.head_bucket(Bucket=self.warehouse)
        except ClientError:
            return False
        return True

    def bound(self, namespace: str) -> bool:
        settings = get_settings()
        return warehouses.binding_for_namespace(settings.registry_root, settings.storage_options(), namespace) is not None


def _lifecycle(event: dict[str, Any]) -> str | None:
    facets = event["dataset"]["facets"] if "dataset" in event else {}
    return facets.get("lifecycleStateChange", {}).get("lifecycleStateChange")


@pytest.fixture
def tenant(moto_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Tenant]:
    """The catalog on a control bucket of its own, with OIDC and FGA on once its lifespan has run, as alice."""
    control = f"lh495-{uuid.uuid4().hex[:10]}"
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
    }.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    from catalog.main import app

    store = _Outage()
    posted: list[dict[str, Any]] = []
    lock = threading.Lock()

    def _receive(request: httpx.Request) -> httpx.Response:
        with lock:
            posted.append(json.loads(request.content))
        return httpx.Response(200)

    with TestClient(app, headers={"Authorization": "Bearer t"}) as client:
        governed = get_settings().model_copy(update={"oidc_enabled": True, "oidc_issuer": "https://idp.example", "oidc_audience": "lance", "fga_enabled": True})
        app.dependency_overrides[get_settings] = lambda: governed
        monkeypatch.setattr(app.state, "oidc", MagicMock())
        act_as(client, "alice")
        monkeypatch.setattr(app.state, "fga", store)
        emitter = HttpLineageEmitter(httpx.AsyncClient(transport=httpx.MockTransport(_receive)), "http://lineage/api/v1/lineage", job_namespace="lance-catalog")
        monkeypatch.setattr(app.state, "lineage_emitter", emitter, raising=False)
        for path, body in (("/management/v1/projects", {"id": PROJECT}), ("/management/v1/warehouses", {"id": warehouse, "project": PROJECT})):
            answer = client.post(path, json=body)
            assert answer.status_code == 200, f"{path}: {answer.text}"
        yield _Tenant(client, store, posted, s3, warehouse)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_a_cascade_removes_every_table_and_namespace_then_revokes_their_grants(tenant: _Tenant) -> None:
    """Tables under the bound namespace and under a nested one are detached, and so is the namespace subtree; a table a
    recoverable drop already detached loses the grants its undrop needed; the count the door reports is what the store lost."""
    tenant.bind("bronze")
    for table in ("bronze$a", "bronze$b"):
        tenant.create_table(table)
    tenant.create_namespace("bronze$inner")
    tenant.create_table("bronze$inner$c")
    tenant.create_table("bronze$trashed")
    assert tenant.client.post("/v1/table/bronze$trashed/drop", json={}).status_code == 200
    tables = ["bronze$a", "bronze$b", "bronze$inner$c", "bronze$trashed"]
    governed = [f"warehouse:{tenant.warehouse}", "namespace:bronze", "namespace:bronze$inner", *(f"table:{table}" for table in tables)]
    assert len(tenant.store.naming(governed)) == len(governed), "an object was seeded no tuple, so the revoke below would pass vacuously"
    assert "bronze" in tenant.client.app.state.warehouse_binding_cache, "nothing routed through the binding, so its eviction below would pass vacuously"
    held = len(tenant.store.tuples)
    tenant.posted.clear()

    answer = tenant.delete_warehouse()

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert (body["namespaces_dropped"], body["bucket_purged"]) == (["bronze$inner", "bronze"], True)
    assert tenant.store.naming(governed) == {}, "a grant outlived the object it was on"
    assert body["tuples_revoked"] == held - len(tenant.store.tuples), "the door reported a count the store did not lose"
    assert sorted(tenant.announced_drops()) == ["bronze$a", "bronze$b", "bronze$inner$c"]
    assert not tenant.bucket_exists()
    assert not tenant.bound("bronze") and "bronze" not in tenant.client.app.state.warehouse_binding_cache


def test_a_cascade_that_fails_part_way_says_what_landed_keeps_the_rest_governed_and_a_reissue_finishes_it(tenant: _Tenant) -> None:
    """OpenFGA stops answering for ``bronze$b`` while the first namespace's grants are revoked: the 503 counts what the
    store lost, every object the catalog still serves keeps every grant and stays readable, and the same call issued
    again finishes the delete."""
    for namespace in ("bronze", "silver"):
        tenant.bind(namespace)
    tables = ["bronze$a", "bronze$b", "silver$c"]
    for table in tables:
        tenant.create_table(table)
    governed = [f"warehouse:{tenant.warehouse}", "namespace:bronze", "namespace:silver", *(f"table:{table}" for table in tables)]
    before = tenant.store.naming(governed)
    held = len(tenant.store.tuples)
    tenant.store.down = "table:bronze$b"

    partial = tenant.delete_warehouse()

    served = tenant.present(governed)
    assert tenant.store.naming(served) == {obj: before[obj] for obj in served}, "a grant on an object still there was revoked"
    assert (partial.status_code, partial.headers["content-type"]) == (503, "application/problem+json"), partial.text
    report = partial.json()
    assert (report["partial"], report["namespaces_dropped"], sorted(report["tables_removed"])) == (True, ["bronze"], ["bronze$a", "bronze$b"])
    assert report["tuples_revoked"] == held - len(tenant.store.tuples), "the 503 reported a count the store did not lose"
    assert served == [f"warehouse:{tenant.warehouse}", "namespace:silver", "table:silver$c"]
    tenant.store.down = None

    finished = tenant.delete_warehouse()

    assert finished.status_code == 200, finished.text
    assert tenant.store.naming(governed) == {}, "the re-issue left a grant on an object it removed"
    assert not tenant.bucket_exists()
