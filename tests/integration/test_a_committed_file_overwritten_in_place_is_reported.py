"""[[LH-419]] A file a commit added that is no longer as committed is named by its table's pass (ADR 0184), and the
store's event of the write holds that pass ([[LH-494]] closes-when (d), ADR 0198).

A writer's vended credential may put any object under the paths a commit needs (``vending.build_session_policy``), a
maintain vend may also delete one, and a session policy cannot refuse a put over an existing key. Measured on pylance
13.0.0: a larger or smaller overwrite of a data file breaks every read, and a same-size one is served silently. Lance
requires a published payload to stay immutable while a protected snapshot references it and checks none
(``lance_docs/lance/format/table/blob.md:254-257``), so the catalog records each file a commit adds as the store holds
it, and the table's pass names every recorded file the store holds otherwise, or no longer holds while a retained
version names it.

ONE ESTATE, ON MOTO: the real catalog served by uvicorn over a fresh bucket, and maintenance's own pass over the table.
Each case commits through one of the catalog's commit paths and adds one kind of file the version names (a data file, a
blob sidecar, a deletion file, an index file, a transaction file, a branch's data file), which is then replaced by bytes
of its size, or deleted and put back, as a holder of the table's vend can. The pass names that file and no other. The
store's events of an overwrite and of two keys no table holds, handled as a replica handles them
(``maintenance.services.store_events``), hold the pass of that table alone, a table under a namespace named like a Lance
directory too, and that pass names the file even when its policy's cadence skips the rest of it. A record is forgotten
once no retained version names its files, under the table's vend ([[LH-219]]); when the catalog refuses that vend, the
record is kept and the pass carries the refusal.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import uuid
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import quote

import boto3
import httpx
import lance
import lance_namespace
import pyarrow as pa
import pytest
import respx
import uvicorn
from lance import blob_array
from pydantic import SecretStr

from lakehouse import file_identities, maintenance_policies
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.base_refs import normalise
from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.naming import UUID_PROPERTY
from lakehouse.objectfs import lance_storage_options
from lakehouse.storage import S3Client
from maintenance.core.config import MaintenanceSettings
from maintenance.services import table_pass
from maintenance.services.due import TableIds
from maintenance.services.expiry import DatasetResult
from maintenance.services.naming import claim_store, policy_owners
from maintenance.services.plan import PlanContext
from maintenance.services.queue import Task, TaskQueue
from maintenance.services.store_events import StoreEvents
from tests.integration.maintenance_vend_doors import CATALOG_URL, MAINTAIN_TIER_DOOR, VENDED, described_by, maintain_tier_door


_TABLE_ID: list[str] = ["db", "t"]
_TABLE = "$".join(_TABLE_ID)
_DATA_FILE = r"data/[^/]+\.lance"
_MERGE = "merge_insert?on=id&when_not_matched_insert_all=true"

type _Policy = dict[str, object]


def _rows(name: str, first: int = 1) -> pa.Table:
    return pa.table({"id": pa.array([first, first + 1], pa.int64()), "name": [name, "amy"]})


class _HoldingQueue:
    """The follow-ups store events hold, one per table as the queue's guard keeps them, with `TaskQueue.follow_up`'s
    signature."""

    def __init__(self) -> None:
        self.held: list[Task] = []

    async def follow_up(self, task: Task, at: datetime) -> bool:
        if any(held.uuid == task.uuid for held in self.held):
            return False
        self.held.append(task)
        return True


class _Estate:
    """The catalog on a fresh bucket, the estate key a vend stands in for, and maintenance on the same store."""

    def __init__(self, url: str, client: httpx.Client, bucket: str) -> None:
        self.url = url
        self.client = client
        self.bucket = bucket
        self.stock = lance_namespace.connect("rest", {"uri": str(client.base_url)})
        self.key = lance_storage_options(url, S3KeyPair(access_key_id="test", secret_access_key=SecretStr("test")), "us-east-1")
        self.s3: S3Client = boto3.client("s3", endpoint_url=url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
        assert client.post("/v1/namespace/db/create", json={}).status_code == 200

    def create(self, rows: pa.Table) -> str:
        """The table, through the create door; the location it answered."""
        answer = self._post("create", rows)
        return str(answer.json()["location"])

    def register(self, relative: str) -> str:
        """The dataset at ``relative`` in the estate's bucket, registered as the table; the location the door answered."""
        answer = self.client.post(f"/v1/table/{quote(_TABLE, safe='$')}/register", json={"location": relative})
        assert answer.status_code == 200, answer.text
        return str(answer.json()["location"])

    def write(self, action: str, rows: pa.Table) -> None:
        self._post(action, rows)

    def call(self, action: str, body: dict[str, object]) -> None:
        answer = self.client.post(f"/v1/table/{quote(_TABLE, safe='$')}/{action}", json=body)
        assert answer.status_code == 200, answer.text

    def _post(self, action: str, rows: pa.Table) -> httpx.Response:
        answer = self.client.post(
            f"/v1/table/{quote(_TABLE, safe='$')}/{action}", content=encode_arrow_stream(rows), headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE}
        )
        assert answer.status_code == 200, answer.text
        return answer

    def key_of(self, location: str) -> str:
        """The store key of ``location`` in the estate's bucket."""
        return location.removeprefix(f"s3://{self.bucket}/").rstrip("/")

    def keys(self, location: str) -> set[str]:
        """Every object under the table, by its key relative to the table."""
        pages = self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=f"{self.key_of(location)}/")
        return {str(item["Key"]).removeprefix(f"{self.key_of(location)}/") for page in pages for item in page.get("Contents", [])}

    def read(self, location: str, relative: str) -> bytes:
        return bytes(self.s3.get_object(Bucket=self.bucket, Key=f"{self.key_of(location)}/{relative}")["Body"].read())

    def put(self, location: str, relative: str, body: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=f"{self.key_of(location)}/{relative}", Body=body)

    def delete(self, location: str, relative: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=f"{self.key_of(location)}/{relative}")

    def table_uuid(self) -> str:
        return str(self.client.post(f"/v1/table/{quote(_TABLE, safe='$')}/describe", json={}).json()["properties"][UUID_PROPERTY])

    def recorded(self) -> list[str]:
        """The transaction uuids of the commits whose records the control root still holds."""
        return sorted(file_identities.listed_records(f"s3://{self.bucket}", self.key, self.table_uuid()))

    def settings(self, **extra: object) -> MaintenanceSettings:
        return MaintenanceSettings.model_validate(
            {
                "s3_endpoint": self.url,
                "s3_access_key_id": "test",
                "s3_secret_access_key": "test",
                "s3_bucket": self.bucket,
                "policy_root": f"s3://{self.bucket}",
                "control_root": f"s3://{self.bucket}",
                "catalog_url": CATALOG_URL,
                **extra,
            }
        )

    def passed(
        self,
        location: str,
        *,
        reclaiming: bool = False,
        door: AbstractContextManager[respx.MockRouter] | None = None,
        policies: Sequence[_Policy] = (),
    ) -> DatasetResult:
        """Maintenance's own pass over the table at ``location``, under ``policies``, ``door`` answering its maintain-tier
        vends (by default, each over the table's location); ``reclaiming`` expires every version but the latest first."""
        described = self.client.post("/v1/namespace/db/describe", json={})
        assert described.status_code == 200, described.text
        now = datetime.now(UTC)
        settings = self.settings(**({"min_versions_to_expire": 1} if reclaiming else {}))
        ids = TableIds(by_location={normalise(location): _TABLE}, namespace_uuids={"db": str(described.json()["properties"][UUID_PROPERTY])})
        readings = table_pass.PassReadings(
            table_ids=ids,
            context=PlanContext(policies=list(policies), now=now, older_than=timedelta(0) if reclaiming else timedelta(days=7)),
            table=_TABLE,
            owners=policy_owners(ids, _TABLE, claims=claim_store(settings)),
        )
        with door or maintain_tier_door(described_by(self.client)):
            result = table_pass.maintain_dataset(location, readings=readings, settings=settings)
        assert result.error is None, result.error
        return result

    def reported_from(self, events: Sequence[bytes], *, policies: Sequence[_Policy]) -> dict[str, tuple[str | None, list[str]]]:
        """The store's ``events`` handled as a replica handles them, then the pass of each table they held, under
        ``policies``: by the table's uuid, what cut the pass short and the files it named."""
        queue = _HoldingQueue()
        handler = StoreEvents(self.settings(), cast(TaskQueue, queue))
        for event in events:
            asyncio.run(handler.hold_passes(event))
        results = {task.uuid: self.passed(task.location, policies=policies) for task in queue.held}
        return {table: (result.skipped, result.overwritten_files) for table, result in results.items()}


@pytest.fixture
def estate(moto_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Estate]:
    bucket = f"lh419-{uuid.uuid4().hex[:10]}"
    boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1").create_bucket(Bucket=bucket)
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{bucket}",
        "LANCE_CONTROL_ROOT": f"s3://{bucket}",
        "LANCE_S3_ENDPOINT": moto_url,
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
    }.items():
        monkeypatch.setenv(key, value)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.05)
    with httpx.Client(base_url=f"http://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}", timeout=120) as client:
        yield _Estate(moto_url, client, bucket)
    server.should_exit = True
    thread.join(timeout=30)
    get_settings.cache_clear()


def _added_by(estate: _Estate, location: str, commit: Callable[[], None], pattern: str) -> str:
    """The first file matching ``pattern`` (relative to the table) that ``commit`` added."""
    before = estate.keys(location)
    commit()
    added = sorted(key for key in estate.keys(location) - before if re.fullmatch(pattern, key))
    assert added, f"precondition: the commit added a file matching {pattern}: {sorted(estate.keys(location) - before)}"
    return added[0]


def _stock_append(estate: _Estate, location: str) -> None:
    """A stock client's append, which it stages itself and asks ``version/create`` to finalize."""
    written = lance.fragment.write_fragments(_rows("bob", 3), location, storage_options=estate.key.in_clear())
    lance.LanceDataset.commit(
        location,
        lance.LanceOperation.Append(written),
        read_version=lance.dataset(location, storage_options=estate.key.in_clear()).version,
        storage_options=estate.key.in_clear(),
        namespace_client=estate.stock,
        table_id=_TABLE_ID,
        namespace_client_managed_versioning=True,
    )


def _the_create_doors_write(estate: _Estate) -> tuple[str, str]:
    location = estate.create(_rows("bob"))
    return location, next(key for key in sorted(estate.keys(location)) if re.fullmatch(_DATA_FILE, key))


def _an_insert_into_a_table_registered_under_a_prefix_named_data(estate: _Estate) -> tuple[str, str]:
    """A pipeline's table registered where it wrote it, under a prefix with a segment named like the directory its data
    files sit in, then written through the insert door."""
    written = f"s3://{estate.bucket}/x/data/t"
    lance.write_dataset(_rows("bob"), written, storage_options=estate.key.in_clear(), data_storage_version="2.2", enable_stable_row_ids=True)
    location = estate.register("x/data/t")
    assert "data" in estate.key_of(location).split("/")[1:-1], f"precondition: a segment before the table's own is named data: {location}"
    return location, _added_by(estate, location, lambda: estate.write("insert", _rows("eve", 3)), _DATA_FILE)


def _a_blob_sidecar_of_the_create_doors_write(estate: _Estate) -> tuple[str, str]:
    location = estate.create(pa.table({"id": pa.array([1], pa.int64()), "payload": blob_array([b"p" * 200_000])}))
    return location, next(key for key in sorted(estate.keys(location)) if re.fullmatch(r"data/[^/]+/[^/]+\.blob", key))


def _the_create_doors_overwrite(estate: _Estate) -> tuple[str, str]:
    location = estate.create(_rows("bob"))
    return location, _added_by(estate, location, lambda: estate.write("create?mode=Overwrite", _rows("eve")), _DATA_FILE)


def _the_insert_doors_append(estate: _Estate) -> tuple[str, str]:
    location = estate.create(_rows("bob"))
    return location, _added_by(estate, location, lambda: estate.write("insert", _rows("eve", 3)), _DATA_FILE)


def _a_doors_stamped_merge(estate: _Estate) -> tuple[str, str]:
    location = estate.create(_rows("bob"))
    return location, _added_by(estate, location, lambda: estate.write(_MERGE, _rows("eve", 3)), _DATA_FILE)


def _a_deletion_file_of_a_doors_stamped_delete(estate: _Estate) -> tuple[str, str]:
    location = estate.create(_rows("bob"))
    return location, _added_by(estate, location, lambda: estate.call("delete", {"predicate": "id = 1"}), r"_deletions/[^/]+")


def _a_branch_data_file_of_a_doors_stamped_merge(estate: _Estate) -> tuple[str, str]:
    location = estate.create(_rows("bob"))
    estate.call("branches/create", {"name": "b"})
    return location, _added_by(estate, location, lambda: estate.write(f"{_MERGE}&branch=b", _rows("eve", 3)), r"tree/b/data/[^/]+\.lance")


def _a_stock_clients_version_create(estate: _Estate) -> tuple[str, str]:
    location = estate.create(_rows("bob"))
    return location, _added_by(estate, location, lambda: _stock_append(estate, location), _DATA_FILE)


def _the_transaction_file_of_a_stock_clients_version_create(estate: _Estate) -> tuple[str, str]:
    location = estate.create(_rows("bob"))
    return location, _added_by(estate, location, lambda: _stock_append(estate, location), r"_transactions/[^/]+\.txn")


def _an_index_file_of_a_stock_clients_version_create(estate: _Estate) -> tuple[str, str]:
    location = estate.create(_rows("bob"))
    opened = lance.dataset(namespace_client=estate.stock, table_id=_TABLE_ID, storage_options=estate.key.in_clear())
    return location, _added_by(estate, location, lambda: opened.create_scalar_index("id", "BTREE"), r"_indices/[^/]+/[^/]+")


def _overwritten(estate: _Estate, location: str, relative: str) -> None:
    """Replaced by bytes of its size, as a holder of the table's write vend can."""
    held = estate.read(location, relative)
    estate.put(location, relative, bytes([held[0] ^ 0xFF]) + held[1:])


def _deleted(estate: _Estate, location: str, relative: str) -> None:
    """Deleted while a retained version names it, as a holder of the table's maintain vend can."""
    estate.delete(location, relative)


def _deleted_then_put_back(estate: _Estate, location: str, relative: str) -> None:
    """Every file the commit added deleted, a pass run while they are gone, then put back with ``relative`` replaced: a
    record the pass forgot while its files were gone would name nothing once they are back."""
    added = {key: estate.read(location, key) for key in estate.keys(location) if key == relative or re.fullmatch(r"_transactions/[^/]+\.txn", key)}
    for key in added:
        estate.delete(location, key)
    estate.passed(location)
    for key, held in added.items():
        estate.put(location, key, bytes([held[0] ^ 0xFF]) + held[1:] if key == relative else held)


@pytest.mark.parametrize(
    ("commit", "tamper"),
    [
        pytest.param(_the_create_doors_write, _overwritten, id="the-create-doors-write"),
        pytest.param(_a_blob_sidecar_of_the_create_doors_write, _overwritten, id="a-blob-sidecar"),
        pytest.param(_the_create_doors_overwrite, _overwritten, id="the-create-doors-overwrite"),
        pytest.param(_the_insert_doors_append, _overwritten, id="the-insert-doors-append"),
        pytest.param(_a_doors_stamped_merge, _overwritten, id="a-doors-stamped-merge"),
        pytest.param(_a_deletion_file_of_a_doors_stamped_delete, _overwritten, id="a-deletion-file"),
        pytest.param(_a_branch_data_file_of_a_doors_stamped_merge, _overwritten, id="a-branch-data-file"),
        pytest.param(_a_stock_clients_version_create, _overwritten, id="a-stock-clients-version-create"),
        pytest.param(_the_transaction_file_of_a_stock_clients_version_create, _overwritten, id="a-transaction-file"),
        pytest.param(_an_index_file_of_a_stock_clients_version_create, _overwritten, id="an-index-file"),
        pytest.param(_the_create_doors_write, _deleted, id="deleted"),
        pytest.param(_the_create_doors_write, _deleted_then_put_back, id="deleted-then-put-back"),
    ],
)
def test_a_file_a_commit_added_is_named_by_its_tables_pass_once_it_is_not_as_committed(
    estate: _Estate, commit: Callable[[_Estate], tuple[str, str]], tamper: Callable[[_Estate, str, str], None]
) -> None:
    location, relative = commit(estate)
    tamper(estate, location, relative)

    assert estate.passed(location).overwritten_files == [relative]


def _store_event(bucket: str, key: str) -> bytes:
    """The store's event of a write of ``key``, shaped as RustFS 1.0.3 publishes one (measured 2026-11-09,
    ``maintenance.services.store_events``): one record, the key URL-encoded under ``data.s3``."""
    name, encoded = "s3:ObjectCreated:Put", quote(key, safe="")
    record = {
        "bucket_name": bucket,
        "object_name": encoded,
        "event_name": name,
        "data": {"eventVersion": "2.1", "eventSource": "rustfs:s3", "eventName": name, "s3": {"bucket": {"name": bucket}, "object": {"key": encoded}}},
    }
    return json.dumps({"EventName": name, "Key": f"{bucket}/{key}", "Records": [record]}).encode()


def _no_policy(estate: _Estate, location: str) -> list[_Policy]:
    return []


def _a_cadence_that_skips_the_pass(estate: _Estate, location: str) -> list[_Policy]:
    """The table's own policy, its cadence stamped a minute ago, so its next pass is cut short by it."""
    policy: _Policy = {"kind": "table", "id": _TABLE, "uuid": estate.table_uuid(), "compact_enabled": True, "compact_interval_hours": 24}
    stamped = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    maintenance_policies.write_state(f"s3://{estate.bucket}", estate.key, policy, table_uuid=estate.table_uuid(), uri=location, at=stamped)
    return [policy]


@pytest.mark.parametrize(
    ("commit", "arrange", "cut_short_by"),
    [
        pytest.param(_the_create_doors_write, _no_policy, None, id="a-table-with-no-policy"),
        pytest.param(_the_create_doors_write, _a_cadence_that_skips_the_pass, "policy_interval", id="a-pass-its-policys-cadence-skips"),
        pytest.param(_an_insert_into_a_table_registered_under_a_prefix_named_data, _no_policy, None, id="a-table-registered-under-a-prefix-named-data"),
    ],
)
def test_an_overwrite_is_reported_by_the_pass_its_store_event_holds(
    estate: _Estate,
    commit: Callable[[_Estate], tuple[str, str]],
    arrange: Callable[[_Estate, str], list[_Policy]],
    cut_short_by: str | None,
) -> None:
    location, relative = commit(estate)
    policies = arrange(estate, location)
    _overwritten(estate, location, relative)
    events = [
        _store_event(estate.bucket, key) for key in (f"{estate.key_of(location)}/{relative}", "_file_identities/a/b.json", f"{uuid.uuid4()}/data/x.lance")
    ]

    reported = estate.reported_from(events, policies=policies)

    assert reported == {estate.table_uuid(): (cut_short_by, [relative])}


def _vended(location: str) -> httpx.Response:
    return httpx.Response(200, json={"mode": "direct", "credentials": {"storage_options": VENDED}, "location": location})


def _refused(_location: str) -> httpx.Response:
    return httpx.Response(403, json={"title": "Forbidden", "status": 403, "detail": "the caller may not maintain the table"})


@contextmanager
def _maintain_door_answering(*answers: httpx.Response) -> Generator[respx.MockRouter]:
    """The maintain-tier door answering each ask in turn: the expiry's, then the forget's."""
    with respx.mock() as router:
        router.post(url__regex=MAINTAIN_TIER_DOOR, params={"tier": "maintain"}).mock(side_effect=list(answers))
        yield router


@pytest.mark.parametrize(
    ("forget_answer", "kept", "refusal"),
    [
        pytest.param(_vended, ("overwrite",), (None, None), id="forgotten-under-the-tables-vend"),
        pytest.param(_refused, ("create", "overwrite"), ("vend_denied", _TABLE), id="kept-and-refused-when-its-vend-is-refused"),
    ],
)
def test_a_record_is_forgotten_once_no_retained_version_names_its_files(
    estate: _Estate, forget_answer: Callable[[str], httpx.Response], kept: tuple[str, ...], refusal: tuple[str | None, str | None]
) -> None:
    location = estate.create(_rows("bob"))
    estate.write("create?mode=Overwrite", _rows("eve"))
    table = lance.dataset(location, storage_options=estate.key.in_clear())
    create, overwrite = table.read_transaction(1), table.read_transaction(2)
    assert create is not None and overwrite is not None, "precondition: each version names its transaction"
    transactions = {"create": create.uuid, "overwrite": overwrite.uuid}

    passed = estate.passed(location, reclaiming=True, door=_maintain_door_answering(_vended(location), forget_answer(location)))

    assert (passed.overwritten_files, (passed.refused_by, passed.refused_table_id), estate.recorded()) == (
        [],
        refusal,
        sorted(transactions[commit] for commit in kept),
    )
