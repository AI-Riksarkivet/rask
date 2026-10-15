"""A commit expires its table's versions once enough are reclaimable, and not before (Lakekeeper's min-snapshots-to-expire).

Driven through the real app's `/maintenance-arrival` route over a moto store, for a table registered in a real `dir`
namespace's `__manifest`, under a policy keeping one version, with a fresh clone list as the last sweep leaves it. The
threshold is set through the environment, and each side of it is one case: one reclaimable version short reclaims
nothing; at the threshold every reclaimable version goes. A clone list past its age reclaims nothing at the threshold
either: it is the only evidence of which tables a clone reads through, so the lane leaves the table to the sweep.
"""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import boto3
import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from lance_namespace import connect
from lance_namespace_urllib3_client.models import CreateNamespaceRequest, RegisterTableRequest
from moto.server import ThreadedMotoServer

from service_kit.lakehouse import maintenance_policies
from service_kit.lakehouse.maintenance_due import CloneList, write_clone_list
from service_kit.lakehouse.objectfs import lance_storage_options


LOCATION = "c0ffee00_db$t"
THRESHOLD = 3


@pytest.fixture(scope="module")
def moto_url() -> Iterator[str]:
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture
def bucket(moto_url: str) -> str:
    """A bucket of this test's own: moto's in-process backend is shared by every server in the process."""
    name = f"lake-{uuid.uuid4().hex[:8]}"
    boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1").create_bucket(Bucket=name)
    return name


@pytest.fixture
def store(moto_url: str) -> dict[str, str]:
    return lance_storage_options(moto_url, "test", "test", "us-east-1")


def _table_with(versions: int, *, bucket: str, options: dict[str, str], clone_list_age: timedelta) -> str:
    """`db$t` at ``versions`` versions, registered, under a policy keeping one, beside a clone list ``clone_list_age`` old."""
    root = f"s3://{bucket}"
    uri = f"{root}/{LOCATION}"
    for i in range(versions):
        rows = pa.table({"id": pa.array([i], pa.int64())})
        lance.write_dataset(rows, uri, mode="create" if i == 0 else "append", storage_options=options, data_storage_version="2.2", enable_stable_row_ids=True)
    namespace = connect("dir", {"root": root, **{f"storage.{key}": value for key, value in options.items()}})
    namespace.create_namespace(CreateNamespaceRequest(id=["db"]))
    namespace.register_table(RegisterTableRequest(id=["db", "t"], location=LOCATION))
    maintenance_policies.put_policy(root, options, {"kind": "table", "id": "db$t", "path": f"{bucket}/{LOCATION}", "retain_versions": 1})
    write_clone_list(root, options, CloneList(taken_at=datetime.now(UTC) - clone_list_age))
    return uri


@pytest.fixture
def maintenance(moto_url: str, bucket: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The real app, rebuilt with the event lane registered: the route is mounted at import, from the environment."""
    for key, value in {
        "MAINTENANCE_S3_ENDPOINT": moto_url,
        "MAINTENANCE_S3_ACCESS_KEY_ID": "test",
        "MAINTENANCE_S3_SECRET_ACCESS_KEY": "test",
        "MAINTENANCE_S3_BUCKET": bucket,
        "MAINTENANCE_ARRIVAL_ENABLED": "true",
        "MAINTENANCE_EVENT_MIN_VERSIONS": "1",
        "MAINTENANCE_MIN_VERSIONS_TO_EXPIRE": str(THRESHOLD),
    }.items():
        monkeypatch.setenv(key, value)
    from maintenance.core.config import get_settings

    get_settings.cache_clear()
    import maintenance.service

    service = importlib.reload(maintenance.service)
    with TestClient(service.app) as client:
        yield client
    get_settings.cache_clear()


def _written(uri: str, version: int) -> dict[str, object]:
    """The catalog's write event for `db$t` at ``version``, as the bus delivers it."""
    output = {"name": "db$t", "facets": {"version": {"datasetVersion": str(version)}, "dataSource": {"uri": uri}}}
    return {
        "id": "ce-1",
        "specversion": "1.0",
        "type": "com.dapr.event.sent",
        "data": {"run": {"facets": {"lance": {"operation": "insert"}}}, "outputs": [output]},
    }


@pytest.mark.parametrize(
    ("versions", "clone_list_age", "kept"),
    [
        pytest.param(THRESHOLD, timedelta(0), THRESHOLD, id="one-short-reclaims-nothing"),
        pytest.param(THRESHOLD + 1, timedelta(0), 1, id="at-the-threshold-reclaims"),
        pytest.param(THRESHOLD + 1, timedelta(hours=2), THRESHOLD + 1, id="a-stale-clone-list-leaves-it-to-the-sweep"),
    ],
)
def test_a_commit_expires_its_table_only_at_the_threshold(
    *, bucket: str, store: dict[str, str], maintenance: TestClient, versions: int, clone_list_age: timedelta, kept: int
) -> None:
    uri = _table_with(versions, bucket=bucket, options=store, clone_list_age=clone_list_age)

    answered = maintenance.post("/maintenance-arrival", json=_written(uri, versions))

    assert answered.status_code == 200 and answered.json() == {"status": "SUCCESS"}, answered.text
    assert len(lance.dataset(uri, storage_options=store).versions()) == kept
