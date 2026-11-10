"""A table's pass expires versions and lists what needs compacting, and rewrites nothing (ADR 0155, ADR 0162).

Maintenance decides and does not rewrite: the pass reclaims the superseded versions its policy allows and writes a due
record for a table Lance's own compaction planner has work for, which a bring-your-own job then compacts through the
catalog. Driven through the worker's own task runner (`maintenance.services.tasks.run_task`) on the real app's loop,
over a moto store, on a table laid out as the catalog makes one, at `<root>/<table uuid>` with its row in a real `dir`
namespace's `__manifest` (ADR 0173 §5), so the record can only carry the table's name by reading the manifest.

Maintenance names a dataset only through `__manifest` ([[LH-520]]): the table, and its branch through the table's row.
The catalog's vend door is a double here, and only the named table is asked. A table whose claim no longer names its
uuid gets no pass, and its due record goes with it.

The lineage event for the expiry names the table's uuid, from the claim the catalog took on its location, and its
namespace's, from the namespace's own `__manifest` row (ADR 0173 §6, §10). The sidecar it publishes to is a recording
stand-in, and so is the queue the pass schedules its next pass on, which has `TaskQueue`'s own signatures.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, ClassVar, cast

import boto3
import httpx
import lance
import pyarrow as pa
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lance_namespace import connect
from lance_namespace_urllib3_client.models import CreateNamespaceRequest, RegisterTableRequest
from moto.server import ThreadedMotoServer
from pydantic import SecretStr

from lakehouse import location_claims, maintenance_policies
from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.lineage_events import DatasetUuids, dataset_uuids
from lakehouse.maintenance_due import DueRecord, read_due, write_due
from lakehouse.objectfs import lance_storage_options
from lakehouse.storage_options import StorageOptions
from maintenance.core.config import get_settings
from maintenance.services.queue import Task, TaskQueue
from maintenance.services.table_pass import TableTask, run_table_pass
from maintenance.services.tasks import run_task


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
CATALOG = "http://catalog.test"
#: A table dropped since its last pass: its location's claim names it no longer.
DROPPED_UUID = "9b8a7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
#: A table declared and not yet written: its claim is taken, and no dataset stands at its location.
DECLARED_UUID = "4d5e6f7a-8b9c-4d0e-a1f2-b3c4d5e6f7a8"


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
def estate(moto_url: str) -> Iterator[StorageOptions]:
    """Four small fragments in four versions registered as `db$t`, under its own policy keeping two versions, and `db$b`
    with a branch, under the bucket's project policy keeping one. The bucket is emptied after the test, since moto's
    backend outlives each server in the process."""
    options = lance_storage_options(moto_url, S3KeyPair(access_key_id="test", secret_access_key=SecretStr("test")), "us-east-1")
    for uri, versions in ((URI, 4), (BRANCHED, 1)):
        for i in range(versions):
            rows = pa.table({"id": pa.array(range(i * 10, (i + 1) * 10), pa.int64())})
            lance.write_dataset(
                rows, uri, mode="create" if i == 0 else "append", storage_options=options.in_clear(), data_storage_version="2.2", enable_stable_row_ids=True
            )
    lance.dataset(BRANCHED, storage_options=options.in_clear()).create_branch("work")
    namespace = connect("dir", {"root": ROOT, **{f"storage.{key}": value for key, value in options.in_clear().items()}})
    namespace.create_namespace(CreateNamespaceRequest(id=["db"], properties={"rask.uuid": NAMESPACE_UUID}))
    namespace.register_table(RegisterTableRequest(id=["db", "t"], location=LOCATION))
    claims = location_claims.ClaimStore(control_root=ROOT, storage_options=options)
    location_claims.take(claims, URI, TABLE, ["db", "t"], is_gone=lambda _claim: False, uuid=LOCATION)
    namespace.register_table(RegisterTableRequest(id=["db", "b"], location=BRANCHED_UUID))
    location_claims.take(claims, BRANCHED, "db$b", ["db", "b"], is_gone=lambda _claim: False, uuid=BRANCHED_UUID)
    policy = {"kind": "table", "id": TABLE, "uuid": LOCATION, "compact_enabled": True, "retain_versions": 2, "target_rows_per_fragment": 1000}
    maintenance_policies.put_policy(ROOT, options, policy)
    maintenance_policies.put_policy(ROOT, options, {"kind": "project", "id": "acme", "buckets": [BUCKET], "retain_versions": 1})
    yield options
    s3 = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
        for entry in page.get("Contents", []):
            s3.delete_object(Bucket=BUCKET, Key=entry["Key"])


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
    }.items():
        monkeypatch.setenv(key, value)
    from maintenance.core.config import get_settings

    get_settings.cache_clear()
    from maintenance import runtime

    monkeypatch.setattr(runtime, "DaprClient", _Sidecar)
    _Sidecar.published = []

    @asynccontextmanager
    async def _started(app: FastAPI) -> AsyncGenerator[None]:
        async with runtime.standing(app, get_settings()), runtime.collaborators(app, get_settings()) as held:
            app.state.held = held
            yield

    # What the worker's process starts with (`maintenance.runtime`), on a loop the test can run a task on.
    with TestClient(FastAPI(lifespan=_started)) as client:
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


def _due(table: str, table_uuid: str) -> DueRecord:
    return DueRecord(table=table, uuid=table_uuid, location=f"{ROOT}/{table_uuid}", version=1, evaluated_at=datetime.now(UTC))


class _RecordingQueue:
    """The schedule a pass ends with, with `TaskQueue`'s signature, recorded."""

    def __init__(self) -> None:
        self.held: list[tuple[Task, datetime]] = []

    async def schedule_at(self, task: Task, at: datetime) -> None:
        self.held.append((task, at))


def test_a_tables_task_expires_versions_lists_the_compaction_it_does_not_run_and_holds_its_next(estate: StorageOptions, maintenance: TestClient) -> None:
    before = lance.dataset(URI, storage_options=estate.in_clear())
    fragments = [fragment.fragment_id for fragment in before.get_fragments()]
    asked: list[str] = []
    queue = _RecordingQueue()
    portal = maintenance.portal
    assert portal is not None, "the client was never entered, so nothing the worker runs with was started"
    task = Task(kind="table", uuid=LOCATION, location=URI)

    with _vends_for(asked, estate.in_clear()):
        portal.call(
            partial(
                run_task,
                task,
                settings=get_settings(),
                queue=cast(TaskQueue, queue),
                emitter=maintenance.app.state.held.lineage,
                control=maintenance.app.state.held.control,
            )
        )

    after = lance.dataset(URI, storage_options=estate.in_clear())
    assert (after.version, [fragment.fragment_id for fragment in after.get_fragments()]) == (before.version, fragments), "the pass rewrote the table"
    assert [version["version"] for version in after.versions()] == [before.version - 1, before.version]
    assert asked == [TABLE], f"the vend door was asked about {asked}"
    record = read_due(ROOT, estate, LOCATION)
    assert record is not None, f"no due record for {TABLE}"
    assert (record.table, record.uuid, record.location, record.version, record.policy) == (TABLE, LOCATION, URI, before.version, TABLE)
    assert record.compaction is not None and record.compaction.model_dump() == {
        "fragments": 4,
        "candidate_fragments": 4,
        "tasks": 1,
        "target_rows_per_fragment": 1000,
    }
    named = [dataset_uuids(output) for event in _Sidecar.published for output in event["outputs"] if output["name"] == TABLE]
    assert named == [DatasetUuids(table_uuid=LOCATION, namespace_uuid=NAMESPACE_UUID)], json.dumps(_Sidecar.published)
    assert [held for held, _ in queue.held] == [task], "the pass did not hold the table's next one"


def test_a_branch_is_passed_through_its_tables_row(estate: StorageOptions, maintenance: TestClient) -> None:
    """The table's pass reads its branches from its refs and names each by the table's ``__manifest`` row."""
    with _vends_for([], estate.in_clear()):
        outcome = run_table_pass(TableTask(uuid=BRANCHED_UUID, location=BRANCHED), get_settings())

    assert [(result.uri, result.table_id, result.refused) for result in outcome.results] == [(BRANCHED, "db$b", None), (BRANCH, "db$b", None)]


def test_a_declared_table_waits_for_its_first_commit_and_asks_no_vend(estate: StorageOptions, maintenance: TestClient) -> None:
    """A declaration takes its location's claim before any data lands there, so the pass finds no dataset: it reads
    nothing more and asks for no credential, and waits for the recheck, since the first commit's event queues its pass."""
    declared = f"{ROOT}/{DECLARED_UUID}"
    claims = location_claims.ClaimStore(control_root=ROOT, storage_options=estate)
    location_claims.take(claims, declared, "db$declared", ["db", "declared"], is_gone=lambda _claim: False, uuid=DECLARED_UUID)
    asked: list[str] = []
    before = datetime.now(UTC)

    with _vends_for(asked, estate.in_clear()):
        outcome = run_table_pass(TableTask(uuid=DECLARED_UUID, location=declared), get_settings())

    recheck = timedelta(hours=get_settings().recheck_hours)
    assert (outcome.results, outcome.ended, asked) == ([], None, [])
    assert outcome.next_due is not None and before + recheck <= outcome.next_due <= datetime.now(UTC) + recheck


def test_a_table_its_claim_no_longer_names_gets_no_pass_and_its_due_record_goes(estate: StorageOptions, maintenance: TestClient) -> None:
    write_due(ROOT, estate, _due("db$dropped", DROPPED_UUID))

    outcome = run_table_pass(TableTask(uuid=DROPPED_UUID, location=f"{ROOT}/{DROPPED_UUID}"), get_settings())

    assert (outcome.ended, outcome.next_due, read_due(ROOT, estate, DROPPED_UUID)) == ("gone", None, None)
