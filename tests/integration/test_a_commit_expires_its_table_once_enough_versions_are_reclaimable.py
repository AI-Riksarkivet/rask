"""A commit expires its table's versions once enough are reclaimable, and not before (Lakekeeper's min-snapshots-to-expire).

Driven through the real app's `/maintenance-arrival` route over a moto store, for a table registered in a real `dir`
namespace's `__manifest`, under a policy keeping one version, with a fresh clone list as the last sweep leaves it. The
threshold is set through the environment, and each side of it is one case: one reclaimable version short reclaims
nothing; at the threshold every reclaimable version goes. A clone list past its age reclaims nothing at the threshold
either: it is the only evidence of which tables a clone reads through, so the lane leaves the table to the sweep.
The same pass removes what a commit that lost its version left behind, the manifest it staged ([[LH-529]]).

The table's name is one the catalog percent-encodes ([[LH-319]]): the event names the URL the catalog opened,
``…$r%C3%A4ksm%C3%B6rg%C3%A5s`` as ``describe_table`` answers it, while the ``__manifest`` row and the store hold
``…$räksmörgås``. The policy is keyed by the uuid the catalog's register door recorded in the location's claim, and the
namespace carries its own in its ``__manifest`` row (ADR 0173 §8).
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
from lance_namespace_urllib3_client.models import CreateNamespaceRequest, DescribeTableRequest, RegisterTableRequest
from moto.server import ThreadedMotoServer
from pydantic import SecretStr

from service_kit.governed.store_credential import S3KeyPair
from service_kit.lakehouse import location_claims, maintenance_policies
from service_kit.lakehouse.maintenance_due import CloneList, write_clone_list
from service_kit.lakehouse.naming import UUID_PROPERTY
from service_kit.lakehouse.objectfs import StorageOptions, lance_storage_options


TABLE = "db$räksmörgås"
LOCATION = f"c0ffee00_{TABLE}"
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
def store(moto_url: str) -> StorageOptions:
    return lance_storage_options(moto_url, S3KeyPair(access_key_id="test", secret_access_key=SecretStr("test")), "us-east-1")


def _table_with(versions: int, *, bucket: str, options: StorageOptions, clone_list_age: timedelta) -> str:
    """The table at ``versions`` versions, registered, under a policy keeping one, beside a clone list ``clone_list_age``
    old; the location the catalog describes it at."""
    root = f"s3://{bucket}"
    for i in range(versions):
        rows = pa.table({"id": pa.array([i], pa.int64())})
        lance.write_dataset(
            rows,
            f"{root}/{LOCATION}",
            mode="create" if i == 0 else "append",
            storage_options=options.in_clear(),
            data_storage_version="2.2",
            enable_stable_row_ids=True,
        )
    namespace = connect("dir", {"root": root, **{f"storage.{key}": value for key, value in options.in_clear().items()}})
    namespace.create_namespace(CreateNamespaceRequest(id=["db"], properties={UUID_PROPERTY: str(uuid.uuid4())}))
    namespace.register_table(RegisterTableRequest(id=TABLE.split("$"), location=LOCATION))
    described = str(namespace.describe_table(DescribeTableRequest(id=TABLE.split("$"))).location)
    table_uuid = str(uuid.uuid4())
    claims = location_claims.ClaimStore(control_root=root, storage_options=options)
    location_claims.take(claims, described, TABLE, ["db", "räksmörgås"], is_gone=lambda _claim: False, uuid=table_uuid)
    maintenance_policies.put_policy(root, options, {"kind": "table", "id": TABLE, "uuid": table_uuid, "retain_versions": 1})
    write_clone_list(root, options, CloneList(taken_at=datetime.now(UTC) - clone_list_age))
    return described


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
    """The catalog's write event for the table at ``version``, as the bus delivers it."""
    output = {"name": TABLE, "facets": {"version": {"datasetVersion": str(version)}, "dataSource": {"uri": uri}}}
    return {
        "id": "ce-1",
        "specversion": "1.0",
        "type": "com.dapr.event.sent",
        "data": {"run": {"facets": {"rask_lance": {"operation": "insert"}}}, "outputs": [output]},
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
    *, bucket: str, store: StorageOptions, maintenance: TestClient, versions: int, clone_list_age: timedelta, kept: int
) -> None:
    uri = _table_with(versions, bucket=bucket, options=store, clone_list_age=clone_list_age)

    answered = maintenance.post("/maintenance-arrival", json=_written(uri, versions))

    assert answered.status_code == 200 and answered.json() == {"status": "SUCCESS"}, answered.text
    assert len(lance.dataset(uri, storage_options=store.in_clear()).versions()) == kept


def _staged(bucket: str, moto_url: str, location: str, version: int) -> str:
    """A manifest staged for ``version`` as a stock writer stages one (`<inverted version>.manifest-<uuid>`); its name."""
    name = f"{(1 << 64) - 1 - version:020d}.manifest-{uuid.uuid4()}"
    s3 = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    s3.put_object(Bucket=bucket, Key=f"{location}/_versions/{name}", Body=b"")
    return name


@pytest.mark.parametrize(
    ("grace_seconds", "lost_stays"),
    [
        pytest.param("0", False, id="a-manifest-staged-for-a-committed-version-goes"),
        pytest.param("3600", True, id="one-inside-its-grace-stays"),
    ],
)
def test_a_commits_expiry_removes_the_staged_manifests_no_version_will_take(
    *, bucket: str, moto_url: str, store: StorageOptions, maintenance: TestClient, monkeypatch: pytest.MonkeyPatch, grace_seconds: str, lost_stays: bool
) -> None:
    """A commit that lost its version to another leaves the manifest it staged, which Lance's listing skips and so
    ``cleanup_old_versions`` never reclaims ([[LH-529]]); the expiry after a commit removes it once past its grace. One
    staged for the version after the latest is a commit still in flight and stays."""
    monkeypatch.setenv("MAINTENANCE_STAGED_MANIFEST_GRACE_SECONDS", grace_seconds)
    from maintenance.core.config import get_settings

    get_settings.cache_clear()
    uri = _table_with(THRESHOLD + 1, bucket=bucket, options=store, clone_list_age=timedelta(0))
    lost = _staged(bucket, moto_url, LOCATION, 2)
    in_flight = _staged(bucket, moto_url, LOCATION, THRESHOLD + 2)

    answered = maintenance.post("/maintenance-arrival", json=_written(uri, THRESHOLD + 1))

    assert answered.status_code == 200 and answered.json() == {"status": "SUCCESS"}, answered.text
    s3 = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    held = {entry["Key"].rsplit("/", 1)[-1] for entry in s3.list_objects_v2(Bucket=bucket, Prefix=f"{LOCATION}/_versions/").get("Contents", [])}
    assert {name for name in (lost, in_flight) if name in held} == ({lost, in_flight} if lost_stays else {in_flight})
    assert lance.dataset(uri, storage_options=store.in_clear()).version == THRESHOLD + 1
