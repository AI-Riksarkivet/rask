"""[[LH-419]] A file a commit added that is no longer as committed is named by maintenance's sweep (ADR 0184).

A writer's vended credential may put any object under the paths a commit needs (``vending.build_session_policy``), a
maintain vend may also delete one, and a session policy cannot refuse a put over an existing key. Measured on pylance
13.0.0: a larger or smaller overwrite of a data file breaks every read, and a same-size one is served silently. Lance
requires a published payload to stay immutable while a protected snapshot references it and checks none
(``lance_docs/lance/format/table/blob.md:254-257``), so the catalog records each file a commit adds as the store holds
it, and the sweep names every recorded file the store holds otherwise, or no longer holds while a retained version
names it.

ONE ESTATE, ON MOTO: the real catalog served by uvicorn over a fresh bucket, and maintenance's own sweep pass over the
table. Each case commits through one of the catalog's commit paths and adds one kind of file the version names (a data
file, a blob sidecar, a deletion file, an index file, a transaction file, a branch's data file), which is then replaced
by bytes of its size, or deleted and put back, as a holder of the table's vend can. The sweep names that file and no
other. A record is forgotten once no retained version names its files.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import boto3
import httpx
import lance
import lance_namespace
import pyarrow as pa
import pytest
import uvicorn
from lance import blob_array

from maintenance.core.config import MaintenanceSettings
from maintenance.services import sweep
from maintenance.services.due import TableIds
from maintenance.services.plan import PlanContext
from service_kit.lakehouse import file_identities
from service_kit.lakehouse.base_refs import normalise
from service_kit.lakehouse.maintenance_due import CloneList
from service_kit.lakehouse.naming import UUID_PROPERTY
from service_kit.lakehouse.objectfs import lance_storage_options
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from storage import S3Client


_TABLE_ID: list[str] = ["db", "t"]
_TABLE = "$".join(_TABLE_ID)
_DATA_FILE = r"data/[^/]+\.lance"
_MERGE = "merge_insert?on=id&when_not_matched_insert_all=true"


def _rows(name: str, first: int = 1) -> pa.Table:
    return pa.table({"id": pa.array([first, first + 1], pa.int64()), "name": [name, "amy"]})


class _Estate:
    """The catalog on a fresh bucket, the estate key a vend stands in for, and maintenance on the same store."""

    def __init__(self, url: str, client: httpx.Client, bucket: str) -> None:
        self.url = url
        self.client = client
        self.bucket = bucket
        self.stock = lance_namespace.connect("rest", {"uri": str(client.base_url)})
        self.key = lance_storage_options(url, "test", "test", "us-east-1")
        self.s3: S3Client = boto3.client("s3", endpoint_url=url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
        assert client.post("/v1/namespace/db/create", json={}).status_code == 200

    def create(self, rows: pa.Table) -> str:
        """The table, through the create door; the location it answered."""
        answer = self._post("create", rows)
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

    def _prefix(self, location: str) -> str:
        return location.removeprefix(f"s3://{self.bucket}/").rstrip("/")

    def keys(self, location: str) -> set[str]:
        """Every object under the table, by its key relative to the table."""
        pages = self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=f"{self._prefix(location)}/")
        return {str(item["Key"]).removeprefix(f"{self._prefix(location)}/") for page in pages for item in page.get("Contents", [])}

    def read(self, location: str, relative: str) -> bytes:
        return bytes(self.s3.get_object(Bucket=self.bucket, Key=f"{self._prefix(location)}/{relative}")["Body"].read())

    def put(self, location: str, relative: str, body: bytes) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=f"{self._prefix(location)}/{relative}", Body=body)

    def delete(self, location: str, relative: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=f"{self._prefix(location)}/{relative}")

    def table_uuid(self) -> str:
        return str(self.client.post(f"/v1/table/{quote(_TABLE, safe='$')}/describe", json={}).json()["properties"][UUID_PROPERTY])

    def recorded(self) -> list[str]:
        """The transaction uuids of the commits whose records the control root still holds."""
        return sorted(commit.transaction_uuid for commit in file_identities.recorded(f"s3://{self.bucket}", self.key, self.table_uuid()))

    def swept(self, location: str, *, reclaiming: bool = False) -> list[str]:
        """The files maintenance's own sweep pass over the table names as not as committed; ``reclaiming`` expires every
        version but the latest first."""
        described = self.client.post("/v1/namespace/db/describe", json={})
        assert described.status_code == 200, described.text
        now = datetime.now(UTC)
        tick = sweep.SweepPlan(
            uris=[location],
            clones=CloneList(taken_at=now),
            table_ids=TableIds(by_location={normalise(location): _TABLE}, namespace_uuids={"db": str(described.json()["properties"][UUID_PROPERTY])}),
            context=PlanContext(policies=[], now=now, older_than=timedelta(0) if reclaiming else timedelta(days=7)),
        )
        settings = MaintenanceSettings.model_validate(
            {
                "s3_endpoint": self.url,
                "s3_access_key_id": "test",
                "s3_secret_access_key": "test",
                "s3_bucket": self.bucket,
                "policy_root": f"s3://{self.bucket}",
                "control_root": f"s3://{self.bucket}",
                **({"min_versions_to_expire": 1} if reclaiming else {}),
            }
        )
        result = sweep.maintain_dataset(location, sweep=tick, settings=settings)
        assert result.error is None, result.error
        return result.overwritten_files


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
    written = lance.fragment.write_fragments(_rows("bob", 3), location, storage_options=estate.key)
    lance.LanceDataset.commit(
        location,
        lance.LanceOperation.Append(written),
        read_version=lance.dataset(location, storage_options=estate.key).version,
        storage_options=estate.key,
        namespace_client=estate.stock,
        table_id=_TABLE_ID,
        namespace_client_managed_versioning=True,
    )


def _the_create_doors_write(estate: _Estate) -> tuple[str, str]:
    location = estate.create(_rows("bob"))
    return location, next(key for key in sorted(estate.keys(location)) if re.fullmatch(_DATA_FILE, key))


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
    opened = lance.dataset(namespace_client=estate.stock, table_id=_TABLE_ID, storage_options=estate.key)
    return location, _added_by(estate, location, lambda: opened.create_scalar_index("id", "BTREE"), r"_indices/[^/]+/[^/]+")


def _overwritten(estate: _Estate, location: str, relative: str) -> None:
    """Replaced by bytes of its size, as a holder of the table's write vend can."""
    held = estate.read(location, relative)
    estate.put(location, relative, bytes([held[0] ^ 0xFF]) + held[1:])


def _deleted_then_put_back(estate: _Estate, location: str, relative: str) -> None:
    """Every file the commit added deleted, as a holder of the table's maintain vend can, swept while gone, then put back
    with ``relative`` replaced."""
    added = {key: estate.read(location, key) for key in estate.keys(location) if key == relative or re.fullmatch(r"_transactions/[^/]+\.txn", key)}
    for key in added:
        estate.delete(location, key)
    assert estate.swept(location) == sorted(added), "every file a retained version names is named while it is gone"
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
        pytest.param(_the_create_doors_write, _deleted_then_put_back, id="deleted-then-put-back"),
    ],
)
def test_a_file_a_commit_added_is_named_by_the_sweep_once_it_is_not_as_committed(
    estate: _Estate, commit: Callable[[_Estate], tuple[str, str]], tamper: Callable[[_Estate, str, str], None]
) -> None:
    location, relative = commit(estate)
    tamper(estate, location, relative)

    assert estate.swept(location) == [relative]


def test_a_record_is_forgotten_once_no_retained_version_names_its_files(estate: _Estate) -> None:
    location = estate.create(_rows("bob"))
    estate.write("create?mode=Overwrite", _rows("eve"))
    overwrite = lance.dataset(location, storage_options=estate.key).read_transaction(2)
    assert overwrite is not None, "precondition: the overwrite's version names its transaction"

    assert (estate.swept(location, reclaiming=True), estate.recorded()) == ([], [overwrite.uuid])
