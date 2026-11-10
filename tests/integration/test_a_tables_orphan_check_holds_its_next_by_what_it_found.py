"""Each table's orphan check is a task of its own, and holds its next check by what it found (owner, 2026-11-09/10).

Driven through the worker's own `run_task` for an `orphans` task, over a moto store, for a table registered in a real
`dir` namespace's `__manifest`, its location claimed under its uuid as a catalog door claims it. What the bus does with
a schedule is the bus lane's claim; here the queue records what the check asks of it, with `TaskQueue`'s signatures.

The interval rule: the floor F is a day and the ceiling C `MAINTENANCE_ORPHAN_CHECK_CEILING_DAYS`. Stranded or
undecided files hold the next check at F; a raised floor at max(F, the table's retention + a day), since the floor moves
only once a pass cleans the manifests beneath the raise; a clean check, or one finding only files Lance reclaims itself,
doubles the interval it ran at up to C; an excluded table (a branch, too many files) waits C, and so does a table in a
bucket maintenance does not govern; a table gone or in the trash is checked no more; a read that fails raises, so the
worker tries again.

A file the store lists to the whole second is placed anywhere in that second, so a planted file is a finding only once
the check starts in a later second (`_a_second_after`): a polled clock, not a sleep.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import boto3
import httpx
import lance
import pyarrow as pa
import pytest
import respx
from lance_namespace import connect
from lance_namespace_urllib3_client.models import CreateNamespaceRequest, DescribeTableRequest, RegisterTableRequest
from moto.server import ThreadedMotoServer
from pydantic import BaseModel, SecretStr

from lakehouse import location_claims, maintenance_policies, trash
from lakehouse.control_emit import NoopControlEmitter
from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.naming import UUID_PROPERTY
from lakehouse.objectfs import lance_storage_options
from lakehouse.storage_options import StorageOptions
from maintenance.core.config import MaintenanceSettings
from maintenance.core.lineage_emit import NoopEmitter
from maintenance.services import orphans
from maintenance.services.floor import FLOOR_KEY
from maintenance.services.orphan_check import OrphanCheckIncompleteError
from maintenance.services.queue import Task, TaskQueue
from maintenance.services.tasks import run_task
from tests.integration.maintenance_vend_doors import CATALOG_URL, MAINTAIN_TIER_DOOR, maintain_tier_door


TABLE = "db$events"
LOCATION = f"c0ffee00_{TABLE}"
DAY = timedelta(days=1)
#: The retention every table here is kept under (`MAINTENANCE_OLDER_THAN_DAYS`).
RETENTION = timedelta(days=3)


@pytest.fixture(scope="module")
def moto_url() -> Iterator[str]:
    server = ThreadedMotoServer(port=0)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


def _s3(moto_url: str) -> Any:  # noqa: ANN401 — boto3's client is untyped
    return boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")


@pytest.fixture
def bucket(moto_url: str) -> str:
    """A bucket of this test's own: moto's in-process backend is shared by every server in the process."""
    name = f"lake-{uuid.uuid4().hex[:8]}"
    _s3(moto_url).create_bucket(Bucket=name)
    return name


@pytest.fixture
def store(moto_url: str) -> StorageOptions:
    return lance_storage_options(moto_url, S3KeyPair(access_key_id="test", secret_access_key=SecretStr("test")), "us-east-1")


@pytest.fixture
def settings(moto_url: str, bucket: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., MaintenanceSettings]]:
    """``settings(**env)``: the service's settings as the environment gives them, with ``env`` set on top."""
    base = {
        "MAINTENANCE_S3_ENDPOINT": moto_url,
        "MAINTENANCE_S3_ACCESS_KEY_ID": "test",
        "MAINTENANCE_S3_SECRET_ACCESS_KEY": "test",
        "MAINTENANCE_S3_BUCKET": bucket,
        "MAINTENANCE_CATALOG_URL": CATALOG_URL,
        "MAINTENANCE_OLDER_THAN_DAYS": str(RETENTION.days),
        "MAINTENANCE_ORPHAN_CHECK_CEILING_DAYS": "30",
    }
    from maintenance.core.config import get_settings

    def _settings(**env: str) -> MaintenanceSettings:
        for key, value in {**base, **env}.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        return get_settings()

    yield _settings
    get_settings.cache_clear()


class _Held(BaseModel):
    """What a check asked the queue for: a task held until ``at``, as a schedule or as a follow-up."""

    task: Task
    at: datetime
    follow_up: bool = False


class _Queue:
    """The queue as the check uses it, with `TaskQueue`'s signatures: what it is asked to hold, kept in order."""

    def __init__(self) -> None:
        self.held: list[_Held] = []

    async def schedule_at(self, task: Task, at: datetime) -> None:
        self.held.append(_Held(task=task, at=at))

    async def follow_up(self, task: Task, at: datetime) -> bool:
        self.held.append(_Held(task=task, at=at, follow_up=True))
        return True

    async def schedule_first(self, task: Task, at: datetime) -> bool:
        self.held.append(_Held(task=task, at=at))
        return True


def _rows(n: int) -> pa.Table:
    return pa.table({"id": pa.array(range(n), pa.int64())})


def _table(*, bucket: str, options: StorageOptions, control: str | None = None, registered: bool = True, uuid_: str | None = None) -> Task:
    """A table of one version in ``bucket``, registered (or not) in its ``__manifest``, its location claimed on the
    ``control`` root (``bucket``'s own by default) under a fresh uuid; the check's task for it."""
    root = f"s3://{bucket}"
    lance.write_dataset(_rows(3), f"{root}/{LOCATION}", storage_options=options.in_clear(), data_storage_version="2.2", enable_stable_row_ids=True)
    namespace = connect("dir", {"root": root, **{f"storage.{key}": value for key, value in options.in_clear().items()}})
    namespace.create_namespace(CreateNamespaceRequest(id=["db"], properties={UUID_PROPERTY: str(uuid.uuid4())}))
    described = f"{root}/{LOCATION}"
    if registered:
        namespace.register_table(RegisterTableRequest(id=TABLE.split("$"), location=LOCATION))
        described = str(namespace.describe_table(DescribeTableRequest(id=TABLE.split("$"))).location)
    table_uuid = str(uuid.uuid4())
    claims = location_claims.ClaimStore(control_root=control or root, storage_options=options)
    location_claims.take(claims, described, TABLE, ["db", "events"], is_gone=lambda _claim: False, uuid=table_uuid)
    return Task(kind="orphans", uuid=uuid_ or table_uuid, location=described)


def _plant(moto_url: str, bucket: str, rel: str) -> float:
    """An unreferenced file under the table, as an aborted commit leaves one; the second the store lists it at."""
    _s3(moto_url).put_object(Bucket=bucket, Key=f"{LOCATION}/{rel}", Body=b"x" * 64)
    return float(int(time.time()))


def _a_second_after(listed: float) -> None:
    """Wait, against a deadline, until the clock has left the second ``listed`` names, so the check's cut lies past it."""
    deadline = time.monotonic() + 3
    while time.time() < listed + 1.05:
        assert time.monotonic() < deadline, "the clock did not move"


def _check(task: Task, settings: MaintenanceSettings) -> _Queue:
    import asyncio

    queue = _Queue()
    with maintain_tier_door(lambda _table: task.location):
        asyncio.run(run_task(task, settings=settings, queue=cast(TaskQueue, queue), emitter=NoopEmitter(), control=NoopControlEmitter()))
    return queue


def _next(queue: _Queue, task: Task, started: datetime) -> tuple[int, timedelta] | None:
    """The interval the next check of ``task`` is held at, and how far out, rounded to the minute; None when none is."""
    held = [entry for entry in queue.held if entry.task.kind == "orphans" and entry.task.uuid == task.uuid]
    if not held:
        return None
    (entry,) = held
    return entry.task.interval_hours, timedelta(minutes=round((entry.at - started).total_seconds() / 60))


def _clean(**_: Any) -> None:
    return None


def _a_data_file(*, moto_url: str, bucket: str, **_: Any) -> None:
    _a_second_after(_plant(moto_url, bucket, f"data/{uuid.uuid4().hex}.lance"))


def _a_transaction_file(*, moto_url: str, bucket: str, **_: Any) -> None:
    _a_second_after(_plant(moto_url, bucket, f"_transactions/9-{uuid.uuid4()}.txn"))


def _a_branch(*, task: Task, options: StorageOptions, **_: Any) -> None:
    lance.dataset(task.location, storage_options=options.in_clear()).create_branch("feature-a")


def _too_many_files(*, monkeypatch: pytest.MonkeyPatch, **_: Any) -> None:
    monkeypatch.setattr(orphans, "_MAX_REFERENCED_FILES", 1)


def _too_many_listed(*, moto_url: str, bucket: str, monkeypatch: pytest.MonkeyPatch, **_: Any) -> None:
    """More files under the table than one check lists, none of them referenced: what a writer leaves that keeps
    failing before its commit."""
    monkeypatch.setattr(orphans, "_MAX_LISTED_FILES", len(_s3(moto_url).list_objects_v2(Bucket=bucket, Prefix=f"{LOCATION}/")["Contents"]))
    _a_data_file(moto_url=moto_url, bucket=bucket)


def _in_the_trash(*, task: Task, bucket: str, options: StorageOptions, **_: Any) -> None:
    trash.put(f"s3://{bucket}", options, trash.make_record(TABLE, uuid=task.uuid, location=task.location, dropped_by=None, grace_days=7))


@pytest.mark.parametrize(
    ("ran_at", "shape", "registered", "another_uuid", "next_check"),
    [
        pytest.param(24, _clean, True, False, (48, 2 * DAY), id="a-clean-table-doubles-its-interval"),
        pytest.param(24 * 20, _clean, True, False, (24 * 30, 30 * DAY), id="a-clean-table-at-the-ceiling-stays-there"),
        pytest.param(24, _a_data_file, True, False, (48, 2 * DAY), id="files-lance-reclaims-itself-double-it-too"),
        pytest.param(24 * 8, _a_transaction_file, True, False, (24, DAY), id="a-stranded-file-holds-it-at-the-floor"),
        pytest.param(24, _a_branch, True, False, (24 * 30, 30 * DAY), id="a-table-with-a-branch-is-excluded-to-the-ceiling"),
        pytest.param(24, _too_many_files, True, False, (24 * 30, 30 * DAY), id="a-table-past-the-file-ceiling-is-excluded-to-the-ceiling"),
        pytest.param(24, _too_many_listed, True, False, (24 * 30, 30 * DAY), id="a-table-past-the-listing-ceiling-is-excluded-to-the-ceiling"),
        pytest.param(24 * 4, _clean, False, False, (24, DAY), id="a-table-no-manifest-row-names-is-held-at-the-floor"),
        pytest.param(24, _in_the_trash, True, False, None, id="a-table-in-the-trash-is-checked-no-more"),
        pytest.param(24, _clean, True, True, None, id="a-table-gone-from-its-location-is-checked-no-more"),
    ],
)
def test_a_check_holds_the_next_by_what_it_found(
    *,
    moto_url: str,
    bucket: str,
    store: StorageOptions,
    settings: Callable[..., MaintenanceSettings],
    monkeypatch: pytest.MonkeyPatch,
    ran_at: int,
    shape: Callable[..., None],
    registered: bool,
    another_uuid: bool,
    next_check: tuple[int, timedelta] | None,
) -> None:
    task = _table(bucket=bucket, options=store, registered=registered, uuid_=str(uuid.uuid4()) if another_uuid else None)
    task = task.model_copy(update={"interval_hours": ran_at})
    shape(task=task, moto_url=moto_url, bucket=bucket, options=store, monkeypatch=monkeypatch)
    version = lance.dataset(task.location, storage_options=store.in_clear()).version
    started = datetime.now(UTC)

    queue = _check(task, settings())

    assert _next(queue, task, started) == next_check
    assert [entry for entry in queue.held if entry.follow_up] == [], "a check that raised no floor held a pass"
    assert lance.dataset(task.location, storage_options=store.in_clear()).version == version, "a check that raised no floor wrote a version"


def test_a_table_in_a_bucket_maintenance_does_not_govern_is_checked_at_the_ceiling(
    *, moto_url: str, bucket: str, store: StorageOptions, settings: Callable[..., MaintenanceSettings]
) -> None:
    """A deactivated or unbound warehouse's table is no table maintenance writes to, so its check waits the ceiling."""
    elsewhere = f"{bucket}-elsewhere"
    _s3(moto_url).create_bucket(Bucket=elsewhere)
    task = _table(bucket=elsewhere, options=store, control=f"s3://{bucket}")
    started = datetime.now(UTC)

    assert _next(_check(task, settings()), task, started) == (24 * 30, 30 * DAY)


def test_a_check_whose_table_cannot_be_read_is_tried_again(
    *, moto_url: str, bucket: str, store: StorageOptions, settings: Callable[..., MaintenanceSettings]
) -> None:
    """A read that fails decides nothing: the check raises, so the worker delivers it again, and holds nothing."""
    task = _table(bucket=bucket, options=store)
    s3 = _s3(moto_url)
    for entry in s3.list_objects_v2(Bucket=bucket, Prefix=f"{LOCATION}/_versions/")["Contents"]:
        s3.put_object(Bucket=bucket, Key=entry["Key"], Body=b"not a manifest")

    with pytest.raises(OrphanCheckIncompleteError):
        _check(task, settings())


@pytest.mark.parametrize(
    ("env", "denied", "kept_whole", "raised"),
    [
        pytest.param({"MAINTENANCE_FLOOR_RAISE_DRY_RUN": "false"}, False, False, True, id="raised-under-the-tables-vend"),
        pytest.param({"MAINTENANCE_FLOOR_RAISE_DRY_RUN": "true"}, False, False, False, id="a-dry-run-writes-nothing"),
        pytest.param({"MAINTENANCE_FLOOR_RAISE_DRY_RUN": "false"}, True, False, False, id="a-catalog-denial-writes-nothing"),
        pytest.param({"MAINTENANCE_FLOOR_RAISE_DRY_RUN": "false"}, False, True, False, id="a-table-kept-whole-is-asked-no-vend-and-written-nothing"),
    ],
)
def test_a_stranded_table_has_its_floor_raised_and_its_pass_held(
    *,
    moto_url: str,
    bucket: str,
    store: StorageOptions,
    settings: Callable[..., MaintenanceSettings],
    env: dict[str, str],
    denied: bool,
    kept_whole: bool,
    raised: bool,
) -> None:
    """The raise is ONE metadata-only commit, signed by the table's vend ([[LH-219]]), that moves the floor above the
    stranded file once a pass cleans beneath it: the check holds that pass, and its own next check past the retention
    the cleanup waits on. A dry run, or a vend the catalog refuses, writes nothing and holds the next check at the floor.
    A table whose policy keeps its whole history is never cleaned, so a raise would buy it only a version: none is
    written and no vend asked."""
    task = _table(bucket=bucket, options=store)
    if kept_whole:
        maintenance_policies.put_policy(f"s3://{bucket}", store, {"kind": "table", "id": TABLE, "uuid": task.uuid, "cleanup_enabled": False})
    _a_second_after(_plant(moto_url, bucket, f"_transactions/9-{uuid.uuid4()}.txn"))
    started = datetime.now(UTC)

    queue = _Queue()
    import asyncio

    with respx.mock(assert_all_called=False) as router:
        door = router.post(url__regex=MAINTAIN_TIER_DOOR, params={"tier": "maintain"})
        if denied:
            door.mock(return_value=httpx.Response(403, json={"code": 13, "detail": "not yours"}))
        else:
            vended = {"aws_access_key_id": "test", "aws_secret_access_key": "test", "aws_endpoint": "http://store.vended.example"}
            door.mock(return_value=httpx.Response(200, json={"mode": "direct", "credentials": {"storage_options": vended}, "location": task.location}))
        router.route().pass_through()
        asyncio.run(
            run_task(
                task,
                settings=settings(MAINTENANCE_FLOOR_RAISE_ENABLED="true", **env),
                queue=cast(TaskQueue, queue),
                emitter=NoopEmitter(),
                control=NoopControlEmitter(),
            )
        )

    dataset = lance.dataset(task.location, storage_options=store.in_clear())
    follow_ups = [(entry.task.kind, entry.task.uuid) for entry in queue.held if entry.follow_up]
    assert (door.called, FLOOR_KEY in dataset.config(), follow_ups) == (not kept_whole, raised, [("table", task.uuid)] if raised else [])
    assert _next(queue, task, started) == ((24 * (RETENTION + DAY).days, RETENTION + DAY) if raised else (24, DAY))


@pytest.mark.parametrize(
    ("cleaned", "raised_again"),
    [
        pytest.param(True, True, id="raised-again-once-its-floor-has-moved-past-the-stamp"),
        pytest.param(False, False, id="not-again-while-the-manifests-beneath-the-stamp-stand"),
    ],
)
def test_a_stamped_table_is_raised_again_only_once_its_floor_has_moved(
    *, moto_url: str, bucket: str, store: StorageOptions, settings: Callable[..., MaintenanceSettings], cleaned: bool, raised_again: bool
) -> None:
    """A commit alone does not move the floor: the manifests beneath it must be cleaned. While they stand another raise
    buys only a version, so a stamped table is not raised; once a pass has cleaned them the floor sits at the raise, and
    a file stranded above it since earns a raise of its own, which a stamp refused for good would never give it.

    The cleaning is the table's own pass, which cleans beneath a standing raise from the first reclaimable version: this
    table has one, far under `MAINTENANCE_MIN_VERSIONS_TO_EXPIRE`, as a table that commits seldom has."""
    task = _table(bucket=bucket, options=store)
    maintenance_policies.put_policy(f"s3://{bucket}", store, {"kind": "table", "id": TABLE, "uuid": task.uuid, "retain_versions": 1})
    lance.dataset(task.location, storage_options=store.in_clear()).update_config({FLOOR_KEY: str(time.time())})
    if cleaned:
        _check(Task(kind="table", uuid=task.uuid, location=task.location), settings())
        assert len(lance.dataset(task.location, storage_options=store.in_clear()).versions()) == 1
    _a_second_after(_plant(moto_url, bucket, f"_transactions/9-{uuid.uuid4()}.txn"))
    version = lance.dataset(task.location, storage_options=store.in_clear()).version

    _check(task, settings(MAINTENANCE_FLOOR_RAISE_ENABLED="true", MAINTENANCE_FLOOR_RAISE_DRY_RUN="false"))

    assert lance.dataset(task.location, storage_options=store.in_clear()).version == (version + 1 if raised_again else version)


def test_a_file_written_while_the_check_reads_is_no_finding(
    *, moto_url: str, bucket: str, store: StorageOptions, settings: Callable[..., MaintenanceSettings], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit landing during the check writes its transaction file after the referenced set was read, so the file looks
    stranded; age is the only evidence an unverified file has (`lance_docs/lance/guide/read_and_write.md:564-571`), and a
    file the check cannot place before it began reading is not a finding."""
    task = _table(bucket=bucket, options=store)
    read = orphans.referenced_paths_of

    def _a_commit_lands(ds: lance.LanceDataset, uri: str) -> tuple[set[str], int, str | None]:
        found = read(ds, uri)
        _plant(moto_url, bucket, f"_transactions/9-{uuid.uuid4()}.txn")
        return found

    monkeypatch.setattr(orphans, "referenced_paths_of", _a_commit_lands)
    started = datetime.now(UTC)

    assert _next(_check(task, settings()), task, started) == (48, 2 * DAY)


def test_a_tables_pass_holds_its_first_check_a_day_out(*, bucket: str, store: StorageOptions, settings: Callable[..., MaintenanceSettings]) -> None:
    """A table's pass holds a check unless one is held already (`TaskQueue.schedule_first` expects the check's schedule to
    hold nothing), so a table's first pass starts its checks and a later one replaces none."""
    check = _table(bucket=bucket, options=store)
    started = datetime.now(UTC)

    queue = _check(check.model_copy(update={"kind": "table"}), settings())

    assert _next(queue, check, started) == (24, DAY)
