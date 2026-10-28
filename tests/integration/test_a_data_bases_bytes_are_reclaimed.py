"""[[LH-286]] A table's bytes on a data base are reclaimed as its root's are: by version expiry, and by every path that ends it.

LANCE'S OWN CLEANUP IS ROOT-SCOPED. Measured on pylance 13.0.0: after an overwrite of a table whose fragments sit on a
non-root data base, ``cleanup_old_versions(older_than=0, delete_unverified=True)`` removed every superseded version and
none of the base's files, which ``lance_docs/lance/guide/read_and_write.md:534`` says it would delete. A branch of such a table inherits
the base under the same id and appends into the same directory, and a delete leaves the base fragment referenced
behind a root ``_deletions/`` vector. Each table holds its own directory beneath an approved base ([[LH-252]]), and its
base record names it, so maintenance reclaims that directory itself.

One estate on moto: the catalog with one approved data base on a second bucket, each table made, overwritten, branched
and dropped through its doors, so its base record and its directory claim are the catalog's own. Each claim:

- the expiry pass deletes a base file no version of main or of any branch references once it is older than Lance's
  unverified-file age (7 days, ``lance_docs/lance/guide/read_and_write.md:564-571``), keeps one a branch still references and every file
  younger than that age, and reports the base fragment a deleted row keeps alive, which only a compaction could rewrite;
- the purge of an expired trashed table deletes its data directory with its root, then forgets its base record and
  releases the directory's claim, also where an earlier tick released it, and writes no directory marker under a prefix
  maintenance may not write ([[LH-535]]); a directory no claim names the
  table on is left to whoever's bytes it holds while the table is still purged: a record written before [[LH-252]]
  named the approved base itself, which every table naming it shared;
- every catalog door that ends a table's bytes deletes its data directory before the record that names it goes: a
  destructive drop, a destroyed namespace cascade, and a purging warehouse cascade over a live table and over a trashed
  one, since the base is no part of the bucket the cascade purges; a cascade keeping its bucket keeps the directory, and
  a purging cascade whose directory delete fails deletes it when re-issued.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from urllib.parse import quote

import boto3
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from maintenance.core.config import MaintenanceSettings
from maintenance.services import sweep
from maintenance.services.due import TableIds
from maintenance.services.expiry import DatasetResult
from maintenance.services.plan import PlanContext
from maintenance.services.purge import purge_expired_trash
from maintenance.services.reconcile import CATEGORIES, CategorySkipped, ReconcileReport
from service_kit.lakehouse import base_registry, data_directories, location_claims, trash
from service_kit.lakehouse.base_refs import normalise
from service_kit.lakehouse.base_registry import BaseOrigin, BaseRole, RecordedBase
from service_kit.lakehouse.maintenance_due import CloneList
from service_kit.lakehouse.naming import UUID_PROPERTY
from service_kit.lakehouse.objectfs import lance_storage_options
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from storage import S3Client


def _ids(start: int, stop: int) -> pa.Table:
    return pa.table({"id": pa.array(range(start, stop), pa.int64())})


class _Estate:
    """The catalog on a fresh bucket, its namespace and control root, with one approved data base on a second bucket."""

    def __init__(self, url: str, client: TestClient, bucket: str, second: str) -> None:
        self.url = url
        self.client = client
        self.bucket = bucket
        self.second = second
        self.root = f"s3://{bucket}"
        self.data_base = f"s3://{second}/data"
        self.key = lance_storage_options(url, "test", "test", "us-east-1")
        self.s3: S3Client = boto3.client("s3", endpoint_url=url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
        self.registry = base_registry.BaseRegistry(control_root=self.root, storage_options=self.key)
        assert client.post("/v1/namespace/db/create", json={}).status_code == 200

    def create(self, table: str, rows: pa.Table, *, mode: str | None = None) -> str:
        """``table`` written through the create door with its fragments on the data base; the location it answered."""
        params = {"data_base": self.data_base, **({"mode": mode} if mode else {})}
        answer = self.client.post(
            f"/v1/table/{quote(table, safe='$')}/create", params=params, content=encode_arrow_stream(rows), headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE}
        )
        assert answer.status_code == 200, answer.text
        return str(answer.json()["location"])

    def door(self, table: str, action: str, **body: object) -> None:
        answer = self.client.post(f"/v1/table/{quote(table, safe='$')}/{action}", json=body)
        assert answer.status_code == 200, f"{action} {table!r}: {answer.text}"

    def directory(self, location: str) -> str:
        """The one data directory the table at ``location`` holds, as its base record names it."""
        record = base_registry.read_base_record(self.registry, location)
        assert record is not None, "precondition: the create recorded the table's bases"
        directories = [entry.path for entry in record.entries if entry.role is base_registry.BaseRole.DATA]
        assert len(directories) == 1, f"precondition: one data directory, under the approved base: {record}"
        return directories[0]

    def keys(self, bucket: str, prefix: str) -> dict[str, int]:
        """Every object under ``prefix`` in ``bucket`` and its size."""
        pages = self.s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
        return {str(entry["Key"]): int(entry["Size"]) for page in pages for entry in page.get("Contents", [])}

    def files(self, directory: str) -> dict[str, int]:
        """The objects under a data directory, keyed by their name within it."""
        bucket, _, prefix = directory.partition("/")
        return {key.removeprefix(f"{prefix}/"): size for key, size in self.keys(bucket, f"{prefix}/").items()}

    def maintenance(self, **extra: object) -> MaintenanceSettings:
        return MaintenanceSettings.model_validate(
            {
                "s3_endpoint": self.url,
                "s3_access_key_id": "test",
                "s3_secret_access_key": "test",
                "s3_bucket": self.bucket,
                "policy_root": self.root,
                "control_root": self.root,
                **extra,
            }
        )

    def maintained(self, table: str, location: str, *, now: datetime) -> DatasetResult:
        """Maintenance's own pass on the table at ``location``, every version but the current one reclaimable, as of ``now``."""
        described = self.client.post("/v1/namespace/db/describe", json={})
        assert described.status_code == 200, described.text
        tick = sweep.SweepPlan(
            uris=[location],
            clones=CloneList(taken_at=now),
            table_ids=TableIds(by_location={normalise(location): table}, namespace_uuids={"db": str(described.json()["properties"][UUID_PROPERTY])}),
            context=PlanContext(policies=[], now=now, older_than=timedelta(0)),
        )
        return sweep.maintain_dataset(location, sweep=tick, settings=self.maintenance(min_versions_to_expire=1))


@pytest.fixture
def estate(moto_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Estate]:
    suffix = uuid.uuid4().hex[:10]
    bucket, second = f"lh286-{suffix}", f"lh286s-{suffix}"
    s3 = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    for name in (bucket, second):
        s3.create_bucket(Bucket=name)
    for name, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{bucket}",
        "LANCE_CONTROL_ROOT": f"s3://{bucket}",
        "LANCE_S3_ENDPOINT": moto_url,
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_MULTIBASE_DATA_BASES": f"s3://{second}/data",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "LANCE_TRASH_GRACE_DAYS": "1",
    }.items():
        monkeypatch.setenv(name, value)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app) as client:
        yield _Estate(moto_url, client, bucket, second)
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("days", "superseded_kept"),
    [pytest.param(0, True, id="a-file-younger-than-the-age-is-kept"), pytest.param(8, False, id="a-file-past-the-age-is-reclaimed")],
)
def test_the_expiry_pass_reclaims_a_base_file_nothing_references(estate: _Estate, days: int, superseded_kept: bool) -> None:
    table = f"db$t{uuid.uuid4().hex[:8]}"
    location = estate.create(table, _ids(0, 3))
    directory = estate.directory(location)
    branched = estate.files(directory)
    estate.door(table, "branches/create", name="keep")
    estate.create(table, _ids(3, 6), mode="overwrite")
    superseded = {name: size for name, size in estate.files(directory).items() if name not in branched}
    estate.create(table, _ids(6, 9), mode="overwrite")
    current = {name: size for name, size in estate.files(directory).items() if name not in branched and name not in superseded}
    estate.door(table, "delete", predicate="id = 6")
    assert len(branched) == len(superseded) == len(current) == 1, f"precondition: one base file per write: {estate.files(directory)}"

    result = estate.maintained(table, location, now=datetime.now(UTC) + timedelta(days=days))

    assert (result.error, result.refused) == (None, None), result
    assert estate.files(directory) == {**branched, **current, **(superseded if superseded_kept else {})}
    assert (result.base_files_removed, result.base_bytes_removed) == ((0, 0) if superseded_kept else (1, sum(superseded.values())))
    assert result.base_fragments_holding_deleted_rows == 1


def _clean_report() -> ReconcileReport:
    return ReconcileReport(
        checked_at=datetime.now(UTC).isoformat(),
        counts={category: 0 for category in CATEGORIES if category != "orphan_files"},
        total=0,
        skipped=[CategorySkipped(category="orphan_files", reason="off")],
    )


def _expired(estate: _Estate, table: str) -> None:
    (dropped,) = trash.named(estate.root, estate.key, table)
    trash.put(estate.root, estate.key, {**dropped, "expires_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()})


@pytest.mark.parametrize(
    "claim",
    [
        pytest.param("held", id="its-claim-held"),
        # A forget interrupted after it released the claims and before it deleted the record leaves the table's own claim
        # released for the tick that finishes the purge.
        pytest.param("released", id="its-claim-released-by-an-earlier-tick"),
    ],
)
def test_the_purge_deletes_a_trashed_tables_data_directory_with_its_root(estate: _Estate, claim: str) -> None:
    table = f"db$p{uuid.uuid4().hex[:8]}"
    location = estate.create(table, _ids(0, 3))
    estate.create(table, _ids(3, 6), mode="overwrite")
    directory = estate.directory(location)
    claims = location_claims.ClaimStore(control_root=estate.root, storage_options=estate.key)
    root_prefix = f"{location.removeprefix(f'{estate.root}/')}/"
    estate.door(table, "drop")
    held = {**estate.keys(estate.bucket, root_prefix), **estate.files(directory)}
    assert len(estate.files(directory)) == 2, "precondition: the create and the overwrite each left a file on the base"
    _expired(estate, table)
    if claim == "released":
        location_claims.release(claims, directory, table)
    markers = {key for key in estate.keys(estate.bucket, "") if key.endswith("/")}

    out = asyncio.run(
        purge_expired_trash(estate.maintenance(trash_purge_enabled=True), report=_clean_report(), control_root=estate.root, data_roots={estate.root})
    )

    assert out.refused == [], out.refused
    assert [(purged.id, purged.files_deleted, purged.bytes_deleted) for purged in out.purged] == [(table, len(held), sum(held.values()))]
    assert (estate.keys(estate.bucket, root_prefix), estate.files(directory)) == ({}, {}), "the purge left bytes of the table it reclaimed"
    # Maintenance may not write under `_bases/` (the chart's ForgetABaseRecordNeverWriteOne), and a store that matches the
    # Deny as AWS does refuses the marker a record delete would write, so the purge writes none ([[LH-535]]).
    assert {key for key in estate.keys(estate.bucket, "") if key.endswith("/")} == markers, "the purge wrote a directory marker"
    assert base_registry.read_base_record(estate.registry, location) is None, "the base record outlived the bytes it named"
    assert location_claims.holder(claims, directory) is None


def test_the_purge_leaves_a_directory_no_claim_names_its_table_on_and_still_purges_the_table(estate: _Estate) -> None:
    table = f"db$q{uuid.uuid4().hex[:8]}"
    location = estate.create(table, _ids(0, 3))
    root_prefix = f"{location.removeprefix(f'{estate.root}/')}/"
    # The record as a create wrote it before [[LH-252]]: the approved base itself, which no claim gives the table.
    base_registry.forget_base_record(estate.registry, location, table=table)
    base_registry.claim_bases(estate.registry, location, [RecordedBase(path=estate.data_base, role=BaseRole.DATA, origin=BaseOrigin.CREATE)])
    estate.door(table, "drop")
    on_the_base = estate.keys(estate.second, "data/")
    _expired(estate, table)

    out = asyncio.run(
        purge_expired_trash(estate.maintenance(trash_purge_enabled=True), report=_clean_report(), control_root=estate.root, data_roots={estate.root})
    )

    assert (out.refused, [purged.id for purged in out.purged]) == ([], [table]), out
    assert (estate.keys(estate.bucket, root_prefix), estate.keys(estate.second, "data/")) == ({}, on_the_base), "the purge reached the shared base"
    assert base_registry.read_base_record(estate.registry, location) is None


class _Warehoused(_Estate):
    """The catalog with warehouses on: a project, a warehouse with its own bucket and a bound namespace, the data base beside."""

    def __init__(self, url: str, client: TestClient, bucket: str, second: str) -> None:
        self.url = url
        self.client = client
        self.bucket = bucket
        self.second = second
        self.root = f"s3://{bucket}"
        self.data_base = f"s3://{second}/data"
        self.key = lance_storage_options(url, "test", "test", "us-east-1")
        self.s3 = boto3.client("s3", endpoint_url=url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
        self.registry = base_registry.BaseRegistry(control_root=self.root, storage_options=self.key)
        suffix = uuid.uuid4().hex[:8]
        self.warehouse, self.namespace = f"wh{suffix}", f"ns{suffix}"
        assert client.post("/management/v1/projects", json={"id": f"p{suffix}"}).status_code == 200
        assert client.post("/management/v1/warehouses", json={"id": self.warehouse, "project": f"p{suffix}"}).status_code == 200
        bound = client.post(f"/management/v1/warehouses/{self.warehouse}/namespaces", json={"namespace": self.namespace})
        assert bound.status_code == 200, bound.text


@pytest.fixture
def warehoused(moto_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Warehoused]:
    suffix = uuid.uuid4().hex[:10]
    bucket, second = f"lh286w-{suffix}", f"lh286x-{suffix}"
    s3 = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    for name in (bucket, second):
        s3.create_bucket(Bucket=name)
    for name, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{bucket}",
        "LANCE_CONTROL_ROOT": f"s3://{bucket}",
        "LANCE_S3_ENDPOINT": moto_url,
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_MULTIBASE_DATA_BASES": f"s3://{second}/data",
        "LANCE_WAREHOUSES_ENABLED": "true",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "LANCE_TRASH_GRACE_DAYS": "1",
    }.items():
        monkeypatch.setenv(name, value)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app) as client:
        yield _Warehoused(moto_url, client, bucket, second)
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("door", "trashed", "purge"),
    [
        pytest.param("table", False, True, id="a-destructive-drop"),
        pytest.param("namespace", False, True, id="a-namespace-cascade"),
        pytest.param("warehouse", False, True, id="a-warehouse-cascade-over-a-live-table"),
        pytest.param("warehouse", True, True, id="a-warehouse-cascade-over-a-trashed-table"),
        # A cascade keeping its bucket keeps every byte the table had, its data directory included.
        pytest.param("warehouse", False, False, id="a-warehouse-cascade-keeping-its-bucket-over-a-live-table"),
        pytest.param("warehouse", True, False, id="a-warehouse-cascade-keeping-its-bucket-over-a-trashed-table"),
    ],
)
def test_every_door_that_ends_a_tables_bytes_ends_its_data_directory_with_them(warehoused: _Warehoused, door: str, trashed: bool, purge: bool) -> None:
    table = f"{warehoused.namespace}$t"
    location = warehoused.create(table, _ids(0, 3))
    directory = warehoused.directory(location)
    created = warehoused.files(directory)
    assert created, "precondition: the create put the table's fragments in its data directory"
    if trashed:
        warehoused.door(table, "drop")

    match door:
        case "table":
            answer = warehoused.client.post(f"/v1/table/{quote(table, safe='$')}/drop", params={"purge": "true"}, json={})
        case "namespace":
            answer = warehoused.client.post(f"/v1/namespace/{warehoused.namespace}/drop", params={"purge": "true"}, json={"behavior": "Cascade"})
        case _:
            answer = warehoused.client.delete(
                f"/management/v1/warehouses/{warehoused.warehouse}", params={"cascade": "true", "purge_bucket": str(purge).lower()}
            )

    assert answer.status_code == 200, answer.text
    assert warehoused.files(directory) == ({} if purge else created)
    # [[LH-279]] Only a detach that keeps the bytes keeps the record vouching for their bases; a trashed table's goes with its trash.
    assert (base_registry.read_base_record(warehoused.registry, location) is not None) == (not purge and not trashed)


def test_a_purging_warehouse_cascade_whose_directory_delete_fails_finishes_it_when_reissued(warehoused: _Warehoused, monkeypatch: pytest.MonkeyPatch) -> None:
    table = f"{warehoused.namespace}$t"
    location = warehoused.create(table, _ids(0, 3))
    directory = warehoused.directory(location)
    refusals = iter([OSError("the store refused the delete")])
    delete_objects = data_directories.delete_objects

    def _refused_once(uri: str, storage_options: dict[str, str], paths: list[str]) -> None:
        if (refusal := next(refusals, None)) is not None:
            raise refusal
        delete_objects(uri, storage_options, paths)

    monkeypatch.setattr(data_directories, "delete_objects", _refused_once)
    cascade = partial(warehoused.client.delete, f"/management/v1/warehouses/{warehoused.warehouse}", params={"cascade": "true", "purge_bucket": "true"})
    refused = cascade()
    assert refused.status_code == 503, f"precondition: the refused delete failed the cascade: {refused.text}"

    answer = cascade()

    assert answer.status_code == 200, answer.text
    assert warehoused.files(directory) == {}, "the re-issued cascade left the data directory the failed one did not delete"
    assert base_registry.read_base_record(warehoused.registry, location) is None
