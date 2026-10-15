"""A sweep tick expires versions and lists what needs compacting, and rewrites nothing (ADR 0155, ADR 0162).

Maintenance decides and does not rewrite: the tick reclaims the superseded versions its policy allows and writes a due
record for a table Lance's own compaction planner has work for, which a bring-your-own job then compacts through the
catalog. Driven through the real app's cron route over a moto store, on a table registered in a real `dir` namespace's
`__manifest` at a directory whose `<hash>_<id>` label names another id — the label a rename leaves behind — so the
record can only carry the right id by reading the manifest.
"""

from __future__ import annotations

from collections.abc import Iterator

import boto3
import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from lance_namespace import connect
from lance_namespace_urllib3_client.models import CreateNamespaceRequest, RegisterTableRequest
from moto.server import ThreadedMotoServer

from service_kit.lakehouse import maintenance_policies
from service_kit.lakehouse.maintenance_due import read_due
from service_kit.lakehouse.objectfs import lance_storage_options


BUCKET = "lake"
ROOT = f"s3://{BUCKET}"
#: The label a rename leaves behind: the directory names `db$old`, the manifest names `db$t`.
LOCATION = "deadbeef_db$old"
URI = f"{ROOT}/{LOCATION}"
TABLE = "db$t"


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
    """Four small fragments in four versions, registered as `db$t`, under a policy keeping one version."""
    options = lance_storage_options(moto_url, "test", "test", "us-east-1")
    for i in range(4):
        rows = pa.table({"id": pa.array(range(i * 10, (i + 1) * 10), pa.int64())})
        lance.write_dataset(rows, URI, mode="create" if i == 0 else "append", storage_options=options, data_storage_version="2.2", enable_stable_row_ids=True)
    namespace = connect("dir", {"root": ROOT, **{f"storage.{key}": value for key, value in options.items()}})
    namespace.create_namespace(CreateNamespaceRequest(id=["db"]))
    namespace.register_table(RegisterTableRequest(id=["db", "t"], location=LOCATION))
    policy = {"kind": "table", "id": TABLE, "path": f"{BUCKET}/{LOCATION}", "compact_enabled": True, "retain_versions": 1, "target_rows_per_fragment": 1000}
    maintenance_policies.put_policy(ROOT, options, policy)
    return options


@pytest.fixture
def maintenance(moto_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    for key, value in {
        "MAINTENANCE_S3_ENDPOINT": moto_url,
        "MAINTENANCE_S3_ACCESS_KEY_ID": "test",
        "MAINTENANCE_S3_SECRET_ACCESS_KEY": "test",
        "MAINTENANCE_S3_BUCKET": BUCKET,
        "MAINTENANCE_MIN_VERSIONS_TO_EXPIRE": "1",
    }.items():
        monkeypatch.setenv(key, value)
    from maintenance.core.config import get_settings

    get_settings.cache_clear()
    from maintenance.service import app

    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()


def test_a_tick_expires_versions_and_lists_the_compaction_it_does_not_run(estate: dict[str, str], maintenance: TestClient) -> None:
    before = lance.dataset(URI, storage_options=estate)
    fragments = [fragment.fragment_id for fragment in before.get_fragments()]

    answered = maintenance.post("/maintenance-cron")

    after = lance.dataset(URI, storage_options=estate)
    assert answered.status_code == 200, answered.text
    assert (after.version, [fragment.fragment_id for fragment in after.get_fragments()]) == (before.version, fragments), "the tick rewrote the table"
    assert [version["version"] for version in after.versions()] == [before.version], "expiry did not reclaim the superseded versions"
    record = read_due(ROOT, estate, TABLE)
    assert record is not None, f"no due record for {TABLE}: {answered.json()}"
    assert (record.table, record.location, record.version, record.policy) == (TABLE, URI, before.version, TABLE)
    assert record.compaction is not None and record.compaction.model_dump() == {
        "fragments": 4,
        "candidate_fragments": 4,
        "tasks": 1,
        "target_rows_per_fragment": 1000,
    }
