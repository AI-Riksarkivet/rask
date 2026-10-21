"""A sweep tick expires versions and lists what needs compacting, and rewrites nothing (ADR 0155, ADR 0162).

Maintenance decides and does not rewrite: the tick reclaims the superseded versions its policy allows and writes a due
record for a table Lance's own compaction planner has work for, which a bring-your-own job then compacts through the
catalog. Driven through the real app's cron route over a moto store, on a table laid out as the catalog makes one, at
`<root>/<table uuid>` with its row in a real `dir` namespace's `__manifest` (ADR 0173 §5), so the record can only carry
the table's name by reading the manifest.

Maintenance names a dataset only through `__manifest` ([[LH-520]]): the table, and its branch through the table's row. A
dataset no row names is reported by its location, and is neither expired nor vended, though a project policy covering
its bucket would reclaim its versions. A declared platform root, which the platform opens by explicit URI and registers
nowhere ([[LH-164]]), is no unnamed dataset: it is neither refused nor expired. The catalog's vend door is a double here,
and only the named table is asked.

The lineage event for the expiry names the table's uuid, from the claim the catalog took on its location, and its
namespace's, from the namespace's own `__manifest` row (ADR 0173 §6, §10). The sidecar it publishes to is a recording
stand-in.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, ClassVar

import boto3
import httpx
import lance
import pyarrow as pa
import pytest
import respx
from fastapi.testclient import TestClient
from lance_namespace import connect
from lance_namespace_urllib3_client.models import CreateNamespaceRequest, RegisterTableRequest
from moto.server import ThreadedMotoServer

from service_kit.lakehouse import location_claims, maintenance_policies
from service_kit.lakehouse.maintenance_due import read_due
from service_kit.lakehouse.objectfs import lance_storage_options
from service_kit.openlineage import DatasetUuids, dataset_uuids


BUCKET = "lake"
ROOT = f"s3://{BUCKET}"
#: The directory a table the catalog makes lives in: its uuid, which names nothing a parser could read.
LOCATION = "0d9c6f3e-7b2a-4c1e-9f5d-2a8b3c4d5e6f"
URI = f"{ROOT}/{LOCATION}"
TABLE = "db$t"
#: The uuid the catalog minted for the namespace `db`, in its `__manifest` row.
NAMESPACE_UUID = "3f2e1d0c-4b5a-4968-8776-a5b4c3d2e1f0"
#: A second table the catalog made, with a branch, which discovery lists as a dataset of its own.
BRANCHED_UUID = "7a1f0e2d-3c4b-4a59-8e6f-b7c8d9e0f1a2"
BRANCHED = f"{ROOT}/{BRANCHED_UUID}"
BRANCH = f"{BRANCHED}/tree/work"
#: A dataset in the catalog's layout that no `__manifest` row names.
UNNAMED = f"{ROOT}/5e3b2a1c-9d8f-4e7a-b6c5-d4e3f2a1b0c9"
#: The model registry's store: a declared platform root.
PLATFORM_ROOT = f"{ROOT}/medallion/models"
PLATFORM = f"{PLATFORM_ROOT}/m"
CATALOG = "http://catalog.test"


@pytest.fixture(scope="module")
def moto_url() -> Iterator[str]:
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    url = f"http://{host}:{port}"
    boto3.client("s3", endpoint_url=url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    yield url
    server.stop()


@pytest.fixture
def estate(moto_url: str) -> dict[str, str]:
    """Four small fragments in four versions registered as `db$t`, under its own policy keeping two versions; `db$b` with a
    branch; three versions nothing names, under the bucket's project policy keeping one; and two in a declared platform root."""
    options = lance_storage_options(moto_url, "test", "test", "us-east-1")
    for uri, versions in ((URI, 4), (BRANCHED, 1), (UNNAMED, 3), (PLATFORM, 2)):
        for i in range(versions):
            rows = pa.table({"id": pa.array(range(i * 10, (i + 1) * 10), pa.int64())})
            lance.write_dataset(
                rows, uri, mode="create" if i == 0 else "append", storage_options=options, data_storage_version="2.2", enable_stable_row_ids=True
            )
    lance.dataset(BRANCHED, storage_options=options).create_branch("work")
    namespace = connect("dir", {"root": ROOT, **{f"storage.{key}": value for key, value in options.items()}})
    namespace.create_namespace(CreateNamespaceRequest(id=["db"], properties={"rask.uuid": NAMESPACE_UUID}))
    namespace.register_table(RegisterTableRequest(id=["db", "t"], location=LOCATION))
    claims = location_claims.ClaimStore(control_root=ROOT, storage_options=options)
    location_claims.take(claims, URI, TABLE, ["db", "t"], is_gone=lambda _claim: False, uuid=LOCATION)
    namespace.register_table(RegisterTableRequest(id=["db", "b"], location=BRANCHED_UUID))
    location_claims.take(claims, BRANCHED, "db$b", ["db", "b"], is_gone=lambda _claim: False, uuid=BRANCHED_UUID)
    policy = {"kind": "table", "id": TABLE, "uuid": LOCATION, "compact_enabled": True, "retain_versions": 2, "target_rows_per_fragment": 1000}
    maintenance_policies.put_policy(ROOT, options, policy)
    maintenance_policies.put_policy(ROOT, options, {"kind": "project", "id": "acme", "buckets": [BUCKET], "retain_versions": 1})
    return options


class _Sidecar:
    """The Dapr client the service publishes its lineage through: it keeps every event, and closes at shutdown."""

    published: ClassVar[list[dict[str, Any]]] = []

    async def publish_event(self, *, data: str = "", **_kwargs: object) -> None:
        _Sidecar.published.append(json.loads(data))

    async def close(self) -> None:
        return None


@pytest.fixture
def maintenance(moto_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    for key, value in {
        "MAINTENANCE_LINEAGE_EMIT_ENABLED": "true",
        "MAINTENANCE_S3_ENDPOINT": moto_url,
        "MAINTENANCE_S3_ACCESS_KEY_ID": "test",
        "MAINTENANCE_S3_SECRET_ACCESS_KEY": "test",
        "MAINTENANCE_S3_BUCKET": BUCKET,
        "MAINTENANCE_MIN_VERSIONS_TO_EXPIRE": "1",
        "MAINTENANCE_CATALOG_URL": CATALOG,
        "MAINTENANCE_DECLARED_PLATFORM_ROOTS": PLATFORM_ROOT,
    }.items():
        monkeypatch.setenv(key, value)
    from maintenance.core.config import get_settings

    get_settings.cache_clear()
    from maintenance import service

    monkeypatch.setattr(service, "DaprClient", _Sidecar)
    _Sidecar.published = []
    with TestClient(service.app) as client:
        yield client
    get_settings.cache_clear()


def _vends_for(asked: list[str], options: dict[str, str]) -> respx.MockRouter:
    """The catalog's vend door, answering the table it governs at ``URI`` and recording every table id it is asked for."""

    def _vend(request: httpx.Request) -> httpx.Response:
        table = request.url.path.removeprefix("/management/v1/table/").removesuffix("/credentials")
        asked.append(table)
        credentials = {"storage_options": options}
        location = URI if table == TABLE else BRANCHED
        return httpx.Response(200, json={"mode": "direct", "credentials": credentials, "location": location})

    router = respx.mock(base_url=CATALOG, assert_all_called=False)
    router.post(url__regex=r"/management/v1/table/.+/credentials").mock(side_effect=_vend)
    return router


def test_a_tick_expires_versions_and_lists_the_compaction_it_does_not_run(estate: dict[str, str], maintenance: TestClient) -> None:
    before = lance.dataset(URI, storage_options=estate)
    fragments = [fragment.fragment_id for fragment in before.get_fragments()]
    asked: list[str] = []

    with _vends_for(asked, estate):
        answered = maintenance.post("/maintenance-cron")

    after = lance.dataset(URI, storage_options=estate)
    assert answered.status_code == 200, answered.text
    assert (after.version, [fragment.fragment_id for fragment in after.get_fragments()]) == (before.version, fragments), "the tick rewrote the table"
    assert [version["version"] for version in after.versions()] == [before.version - 1, before.version], answered.text
    refusals = answered.json()["refusals"]
    assert (answered.json()["refused_by"].get("unnamed"), UNNAMED in refusals, BRANCH in refusals, PLATFORM in refusals) == (1, True, False, False), json.dumps(
        answered.json()
    )
    assert len(lance.dataset(PLATFORM, storage_options=estate).versions()) == 2, "a declared platform root was expired"
    assert len(lance.dataset(UNNAMED, storage_options=estate).versions()) == 3, "a dataset no manifest names was expired"
    assert set(asked) <= {TABLE, "db$b"} and TABLE in asked, f"the vend door was asked about {asked}"
    record = read_due(ROOT, estate, LOCATION)
    assert record is not None, f"no due record for {TABLE}: {answered.json()}"
    assert (record.table, record.uuid, record.location, record.version, record.policy) == (TABLE, LOCATION, URI, before.version, TABLE)
    assert record.compaction is not None and record.compaction.model_dump() == {
        "fragments": 4,
        "candidate_fragments": 4,
        "tasks": 1,
        "target_rows_per_fragment": 1000,
    }
    named = [dataset_uuids(output) for event in _Sidecar.published for output in event["outputs"] if output["name"] == TABLE]
    assert named == [DatasetUuids(table_uuid=LOCATION, namespace_uuid=NAMESPACE_UUID)], json.dumps(_Sidecar.published)
