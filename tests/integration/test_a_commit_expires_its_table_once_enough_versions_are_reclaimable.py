"""A commit's pass expires its table's versions once enough are reclaimable, and not before.

Driven through `run_table_pass` for the task the commit lane queues for a write (`maintenance.services.arrival`: the
table's uuid and the location the event names), over a moto store, for a table registered in a real `dir` namespace's
`__manifest`, under a policy keeping one version. The threshold is set through the environment, and each side of it is
one case: one reclaimable version short reclaims nothing; at the threshold every reclaimable version goes. A pass also
removes what a commit that lost its version left behind, the manifest it staged ([[LH-529]]), whether or not versions are
due, and then schedules the table's next pass for its recheck, never a minute out; under a policy that keeps the whole
history it removes nothing. A pass that cannot read the table's own policy record, which may be a legal hold, reclaims
nothing, whatever the policy of its namespace allows, and is retried. The time a pass schedules for the table's next is
pinned branch by branch, its clock injected.

The table's name is one the catalog percent-encodes ([[LH-319]]): the event names the URL the catalog opened,
``…$r%C3%A4ksm%C3%B6rg%C3%A5s`` as ``describe_table`` answers it, while the ``__manifest`` row and the store hold
``…$räksmörgås``. The policy is keyed by the uuid the catalog's register door recorded in the location's claim, and the
namespace carries its own in its ``__manifest`` row (ADR 0173 §8).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import lance
import pyarrow as pa
import pytest
from lance_namespace import connect
from lance_namespace_urllib3_client.models import CreateNamespaceRequest, DescribeTableRequest, RegisterTableRequest
from moto.server import ThreadedMotoServer
from pydantic import SecretStr

from lakehouse import location_claims, maintenance_policies
from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.lancekit.versions import committed_at
from lakehouse.naming import UUID_PROPERTY
from lakehouse.objectfs import lance_storage_options
from lakehouse.record_store import record_key
from lakehouse.storage_options import StorageOptions
from maintenance.core.config import MaintenanceSettings
from maintenance.services import table_pass
from maintenance.services.expiry import HISTORY_FENCED, DatasetResult
from maintenance.services.table_pass import TableTask, held_location, run_table_pass
from tests.integration.maintenance_vend_doors import CATALOG_URL, maintain_tier_door


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


#: The policy every table here is kept under, but where a case names its own.
KEEP_ONE: dict[str, Any] = {"retain_versions": 1}


def _table_with(
    versions: int, *, bucket: str, options: StorageOptions, policy: dict[str, Any] | None = KEEP_ONE, namespace_uuid: str | None = None
) -> TableTask:
    """The table at ``versions`` versions, registered under a namespace holding ``namespace_uuid`` (a fresh one by default),
    under ``policy`` (keeping one version by default; ``None`` for a table with no record of its own); the task the lane
    queues for its write: its uuid, and the location the catalog describes it at."""
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
    namespace.create_namespace(CreateNamespaceRequest(id=["db"], properties={UUID_PROPERTY: namespace_uuid or str(uuid.uuid4())}))
    namespace.register_table(RegisterTableRequest(id=TABLE.split("$"), location=LOCATION))
    described = str(namespace.describe_table(DescribeTableRequest(id=TABLE.split("$"))).location)
    table_uuid = str(uuid.uuid4())
    claims = location_claims.ClaimStore(control_root=root, storage_options=options)
    location_claims.take(claims, described, TABLE, ["db", "räksmörgås"], is_gone=lambda _claim: False, uuid=table_uuid)
    if policy is not None:
        maintenance_policies.put_policy(root, options, {"kind": "table", "id": TABLE, "uuid": table_uuid, **policy})
    return TableTask(uuid=table_uuid, location=described)


@pytest.fixture
def settings(moto_url: str, bucket: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[], MaintenanceSettings]]:
    """``settings()``: the service's settings as the environment gives them, read again after a test sets more of it."""
    for key, value in {
        "MAINTENANCE_S3_ENDPOINT": moto_url,
        "MAINTENANCE_S3_ACCESS_KEY_ID": "test",
        "MAINTENANCE_S3_SECRET_ACCESS_KEY": "test",
        "MAINTENANCE_S3_BUCKET": bucket,
        "MAINTENANCE_MIN_VERSIONS_TO_EXPIRE": str(THRESHOLD),
        "MAINTENANCE_CATALOG_URL": CATALOG_URL,
    }.items():
        monkeypatch.setenv(key, value)
    from maintenance.core.config import get_settings

    def _settings() -> MaintenanceSettings:
        get_settings.cache_clear()
        return get_settings()

    yield _settings
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("versions", "kept"),
    [
        pytest.param(THRESHOLD, THRESHOLD, id="one-short-reclaims-nothing"),
        pytest.param(THRESHOLD + 1, 1, id="at-the-threshold-reclaims"),
    ],
)
def test_a_commits_pass_expires_its_table_only_at_the_threshold(
    *, bucket: str, store: StorageOptions, settings: Callable[[], MaintenanceSettings], versions: int, kept: int
) -> None:
    task = _table_with(versions, bucket=bucket, options=store)

    with maintain_tier_door(lambda _table: task.location):
        outcome = run_table_pass(task, settings())

    assert (outcome.ended, len(lance.dataset(task.location, storage_options=store.in_clear()).versions())) == (None, kept)


def _staged(bucket: str, moto_url: str, location: str, version: int) -> str:
    """A manifest staged for ``version`` as a stock writer stages one (`<inverted version>.manifest-<uuid>`); its name."""
    name = f"{(1 << 64) - 1 - version:020d}.manifest-{uuid.uuid4()}"
    s3 = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    s3.put_object(Bucket=bucket, Key=f"{location}/_versions/{name}", Body=b"")
    return name


@pytest.mark.parametrize(
    ("grace_seconds", "policy", "lost_stays", "next_pass"),
    [
        pytest.param("0", KEEP_ONE, False, timedelta(hours=24), id="a-manifest-staged-for-a-committed-version-goes"),
        pytest.param("3600", KEEP_ONE, True, timedelta(hours=1), id="one-inside-its-grace-stays-until-the-grace-ends"),
        pytest.param("0", {"cleanup_enabled": False}, True, timedelta(hours=24), id="one-whose-table-keeps-its-whole-history-stays"),
    ],
)
def test_a_commits_pass_removes_the_staged_manifests_no_version_will_take(
    *,
    bucket: str,
    moto_url: str,
    store: StorageOptions,
    settings: Callable[[], MaintenanceSettings],
    monkeypatch: pytest.MonkeyPatch,
    grace_seconds: str,
    policy: dict[str, Any],
    lost_stays: bool,
    next_pass: timedelta,
) -> None:
    """A commit that lost its version to another leaves the manifest it staged, which Lance's listing skips and so
    ``cleanup_old_versions`` never reclaims ([[LH-529]]); a pass removes it once past its grace, on a table no version of
    which is due yet, and next passes the table at its recheck. One staged for the version after the latest is a commit
    still in flight and stays. The table's next pass comes when what it holds next comes due: the grace's end, or its
    recheck (a day, `MAINTENANCE_RECHECK_HOURS`)."""
    monkeypatch.setenv("MAINTENANCE_STAGED_MANIFEST_GRACE_SECONDS", grace_seconds)
    task = _table_with(THRESHOLD, bucket=bucket, options=store, policy=policy)
    lost = _staged(bucket, moto_url, LOCATION, 2)
    in_flight = _staged(bucket, moto_url, LOCATION, THRESHOLD + 1)
    now = datetime.now(UTC)

    with maintain_tier_door(lambda _table: task.location):
        outcome = run_table_pass(task, settings(), now=now)

    s3 = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    held = {entry["Key"].rsplit("/", 1)[-1] for entry in s3.list_objects_v2(Bucket=bucket, Prefix=f"{LOCATION}/_versions/").get("Contents", [])}
    assert {name for name in (lost, in_flight) if name in held} == ({lost, in_flight} if lost_stays else {in_flight})
    due = outcome.next_due
    assert due is not None, "the pass scheduled no next pass"
    # The store stamps a put to the second, so the grace's end comes up to a second before the pass's clock adds it up.
    assert abs(due - now - next_pass) < timedelta(seconds=2), f"the table's next pass is {due - now} out"
    assert lance.dataset(task.location, storage_options=store.in_clear()).version == THRESHOLD


def _outcome(kind: str, uri: str) -> DatasetResult:
    """A dataset's result as the pass leaves it: failed, fenced off by a ref write, refused, dropped mid-pass, or run."""
    match kind:
        case "failed":
            return DatasetResult(uri=uri, error="maintain: the store refused the cleanup")
        case "fenced":
            return DatasetResult(uri=uri, skipped=HISTORY_FENCED)
        case "refused":
            return DatasetResult(uri=uri, refused="the manifest carries a flag maintenance refuses", refused_by="manifest_flags")
        case "trashed":
            return DatasetResult(uri=uri, trashed="a-dropped-tables-uuid")
        case _:
            return DatasetResult(uri=uri)


#: Each case's next pass, from the pass's clock and the commit times of the table's superseded versions, oldest first.
type _Expected = Callable[[datetime, list[datetime]], datetime]
_RETRY = timedelta(seconds=900)
_RECHECK = timedelta(hours=72)


@pytest.mark.parametrize(
    ("policy", "staged_grace", "kind", "expected"),
    [
        pytest.param(KEEP_ONE, None, "failed", lambda now, _: now + _RETRY, id="a-failed-pass-is-retried"),
        pytest.param(KEEP_ONE, None, "fenced", lambda now, _: now + _RETRY, id="a-history-fenced-pass-is-retried"),
        pytest.param({"retention_days": 1}, None, "refused", lambda now, _: now + _RECHECK, id="a-refused-dataset-waits-for-the-recheck"),
        pytest.param({"retention_days": 1}, None, "trashed", lambda now, _: now + _RECHECK, id="one-dropped-mid-pass-waits-for-the-recheck"),
        pytest.param(
            {"retention_days": 1}, None, "ran", lambda _, superseded: superseded[THRESHOLD - 1] + timedelta(days=1), id="enough-versions-come-due-as-they-age"
        ),
        pytest.param(
            {"retention_days": 1, "compact_interval_hours": 30}, None, "ran", lambda now, _: now + timedelta(hours=30), id="the-policy-cadence-holds-it-later"
        ),
        pytest.param(KEEP_ONE, "30", "ran", lambda now, _: now + timedelta(seconds=60), id="never-sooner-than-a-minute"),
    ],
)
def test_a_tables_next_pass_comes_when_its_first_dataset_needs_one(
    *,
    bucket: str,
    moto_url: str,
    store: StorageOptions,
    settings: Callable[[], MaintenanceSettings],
    monkeypatch: pytest.MonkeyPatch,
    policy: dict[str, Any],
    staged_grace: str | None,
    kind: str,
    expected: _Expected,
) -> None:
    """The time a pass schedules for the table (`table_pass.next_due`), read afresh after the pass: a dataset the pass
    failed on, or found fenced, is tried again after the retry; a refused one, and one dropped mid-pass, waits for the
    recheck; the versions the
    policy keeps come due as they age, held to the policy's cadence once it has stamped one; and none comes sooner than a
    minute, however soon a staged manifest's grace ends."""
    monkeypatch.setenv("MAINTENANCE_RECHECK_HOURS", str(int(_RECHECK.total_seconds() // 3600)))
    if staged_grace is not None:
        monkeypatch.setenv("MAINTENANCE_STAGED_MANIFEST_GRACE_SECONDS", staged_grace)
    task = _table_with(THRESHOLD + 1, bucket=bucket, options=store, policy=policy)
    if staged_grace is not None:
        _staged(bucket, moto_url, LOCATION, 2)
    configured = settings()
    now = datetime.now(UTC)
    if "compact_interval_hours" in policy:
        record = {"kind": "table", "id": TABLE, "uuid": task.uuid, **policy}
        maintenance_policies.write_state(f"s3://{bucket}", store, record, table_uuid=task.uuid, uri=held_location(task.location), at=now.isoformat())
    held = location_claims.holder(location_claims.ClaimStore(control_root=f"s3://{bucket}", storage_options=store), task.location)
    assert held is not None
    readings = table_pass.read_for(task.location, held, configured, now=now)
    assert readings is not None
    versions = lance.dataset(task.location, storage_options=store.in_clear()).versions()
    superseded = sorted(committed_at(entry) for entry in versions[:-1])

    due = table_pass.next_due([_outcome(kind, held_location(task.location))], readings=readings, settings=configured)

    assert due == expected(now, superseded)


def test_a_pass_that_cannot_read_its_tables_policy_reclaims_nothing_and_is_retried(
    *, bucket: str, moto_url: str, store: StorageOptions, settings: Callable[[], MaintenanceSettings]
) -> None:
    namespace_uuid = str(uuid.uuid4())
    task = _table_with(THRESHOLD + 1, bucket=bucket, options=store, namespace_uuid=namespace_uuid)
    maintenance_policies.put_policy(f"s3://{bucket}", store, {"kind": "namespace", "id": "db", "uuid": namespace_uuid, "retain_versions": 1})
    s3 = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    s3.put_object(Bucket=bucket, Key=record_key(maintenance_policies.PREFIX, "table", task.uuid), Body=b'{"kind": "table", "cleanup_enabled": fal')
    now = datetime.now(UTC)

    with maintain_tier_door(lambda _table: task.location):
        outcome = run_table_pass(task, settings(), now=now)

    kept = len(lance.dataset(task.location, storage_options=store.in_clear()).versions())
    assert (kept, outcome.next_due) == (THRESHOLD + 1, now + timedelta(seconds=settings().retry_seconds))


def test_a_pass_whose_tables_uuid_is_read_once_and_fails_runs_under_no_policy_at_all(
    *, bucket: str, store: StorageOptions, settings: Callable[[], MaintenanceSettings], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The uuids a table's policy is kept under are read once a pass: a claim read the store fails leaves the table
    refused for that pass, and no later read of the same claim lets it run under the defaults its own record replaces."""
    task = _table_with(THRESHOLD + 1, bucket=bucket, options=store)
    read = location_claims.read
    failed: list[str] = []

    def _fails_once(claims: location_claims.ClaimStore, location: str) -> location_claims.ClaimRead | None:
        if not failed:
            failed.append(location)
            raise OSError("SlowDown")
        return read(claims, location)

    monkeypatch.setattr(location_claims, "read", _fails_once)

    with maintain_tier_door(lambda _table: task.location):
        outcome = run_table_pass(task, settings(), now=datetime.now(UTC))

    kept = len(lance.dataset(task.location, storage_options=store.in_clear()).versions())
    assert (failed != [], [result.refused_by for result in outcome.results], kept) == (True, ["unidentified"], THRESHOLD + 1)


def test_a_projects_hold_governs_its_buckets_table_where_the_policies_are_kept_apart_from_the_registry(
    *, bucket: str, moto_url: str, store: StorageOptions, settings: Callable[[], MaintenanceSettings], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bucket's owner is read off the claim the catalog keeps on the control root, and the project's record off the
    policy root: with the two apart, a project's hold still governs a table that has no record of its own."""
    task = _table_with(THRESHOLD + 1, bucket=bucket, options=store, policy=None)
    s3 = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    apart = f"{bucket}-policies"
    s3.create_bucket(Bucket=apart)
    monkeypatch.setenv("MAINTENANCE_POLICY_ROOT", f"s3://{apart}")
    hold = {"kind": "project", "id": "acme", "buckets": [bucket], "cleanup_enabled": False}
    maintenance_policies.put_policy(f"s3://{apart}", store, hold)
    s3.put_object(Bucket=bucket, Key=f"_warehouses/bucket-claims/{bucket}.json", Body=b'{"project": "acme"}')
    held = location_claims.holder(location_claims.ClaimStore(control_root=f"s3://{bucket}", storage_options=store), task.location)
    assert held is not None

    readings = table_pass.read_for(task.location, held, settings(), now=datetime.now(UTC))

    assert readings is not None and [policy.get("cleanup_enabled") for policy in readings.context.policies] == [False]
