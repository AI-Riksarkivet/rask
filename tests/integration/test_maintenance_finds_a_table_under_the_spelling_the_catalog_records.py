"""[[LH-319]] Maintenance finds a registered table's bytes where pylance keeps them, under whatever spelling the catalog records.

A table the catalog makes lives under the uuid it mints ([[LH-520]]), which needs no spelling. A table registered at a
location keeps its registrar's spelling, and the catalog records that location as the URL ``describe_table`` answers,
which percent-encodes it (``spelled/r%C3%A4ksm%C3%B6rg%C3%A5s`` for ``spelled/räksmörgås``, ``spelled/sp%20ace`` for
``spelled/sp ace``). A trash record keeps that spelling, and a manifest that declares the table as a base names it so.
Measured on pylance 13.0.0, ``dir`` backend, over moto: the table's versions, data, refs and branches land under the key
that URL decodes to (``spelled/räksmörgås/``). Read as the one key prefix it spells, the location names nothing at all.

One estate on moto: each dataset is written beside the catalog, then registered, branched and recoverably dropped through
its doors. Its readers here, each one claim:

- the undrop doors re-attach a recoverably dropped table at the key its recorded location decodes to, where its bytes are,
  so its reads answer after the table undrop and after the namespace undrop of the cascade that dropped it ([[LH-517]]);
- the purge reclaims the trash record that drop wrote, stamped long expired, under a clean drift report, and only the
  table that record's uuid names: a table created since under the same name is another uuid and keeps its bytes
  ([[LH-525]]). The table's own
  branch goes with it: the branch declares the table's root as its base, and a delete takes the branch along, so it
  protects nothing from the purge. What the drop kept for an undrop, its maintenance policy and its base record, ends with
  the bytes ([[LH-215]], [[LH-279]]);
- the tombstone sweep's probe counts every object the record still names ([[LH-061]]): a record is cleared only where that
  count is zero;
- the compaction gate's dataset-root probe finds the dataset a base declared in the catalog's spelling names.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import boto3
import lance
import pyarrow as pa
import pyarrow.ipc as ipc
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from maintenance.core.config import MaintenanceSettings
from maintenance.services.purge import objects_held, purge_expired_trash
from maintenance.services.reconcile import CATEGORIES, CategorySkipped, ReconcileReport
from service_kit.governed.store_credential import S3KeyPair
from service_kit.lakehouse import base_registry, maintenance_policies, trash
from service_kit.lakehouse.objectfs import dataset_root_probe, lance_storage_options
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from storage import S3Client


_ROWS = pa.table({"id": pa.array([1, 2, 3], pa.int64())})

#: A name the catalog answers percent-encoded as UTF-8, and one it answers with an encoded space.
_NAMES = [pytest.param("räksmörgås", id="non-ascii"), pytest.param("sp ace", id="a-space")]


class _Estate:
    """The catalog on a fresh bucket that is both its namespace root and its control root, as the shipped chart roots them."""

    def __init__(self, url: str, client: TestClient, bucket: str) -> None:
        self.url = url
        self.client = client
        self.bucket = bucket
        self.root = f"s3://{bucket}"
        self.key = lance_storage_options(url, S3KeyPair(access_key_id="test", secret_access_key=SecretStr("test")), "us-east-1")
        self.s3: S3Client = boto3.client("s3", endpoint_url=url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
        #: The uuid each table's maintenance policy is kept under, as the policy door answered it (ADR 0173 §8).
        self.policy_uuids: dict[str, str] = {}
        assert client.post("/v1/namespace/db/create", json={}).status_code == 200

    def door(self, table: str, action: str, **body: object) -> str:
        answer = self.client.post(f"/v1/table/{quote(table, safe='$')}/{action}", json=body)
        assert answer.status_code == 200, f"{action} {table!r}: {answer.text}"
        return answer.text

    def made(self, table: str) -> None:
        """``table`` created through the create door, at the uuid-named location the catalog chooses."""
        path = f"/v1/table/{quote(table, safe='$')}/create"
        answer = self.client.post(path, content=encode_arrow_stream(_ROWS), headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
        assert answer.status_code == 200, f"create {table!r}: {answer.text}"

    def reads(self, table: str) -> tuple[int, int]:
        """What ``count_rows`` and ``query`` answer for ``table``."""
        counted = int(self.door(table, "count_rows"))
        queried = self.client.post(f"/v1/table/{quote(table, safe='$')}/query", json={"k": 10, "vector": {}})
        assert queried.status_code == 200, f"query {table!r}: {queried.text}"
        return counted, ipc.open_file(pa.BufferReader(queried.content)).read_all().num_rows

    def registered(self, table: str, name: str) -> str:
        """A dataset written at the key ``spelled/<name>``, registered as ``table`` by that raw name; the location described."""
        lance.write_dataset(
            _ROWS, f"{self.root}/spelled/{quote(name)}", storage_options=self.key.in_clear(), data_storage_version="2.2", enable_stable_row_ids=True
        )
        assert self.client.post(f"/v1/table/{quote(table, safe='$')}/register", json={"location": f"spelled/{name}"}).status_code == 200
        return str(json.loads(self.door(table, "describe"))["location"])

    def objects(self) -> dict[str, int]:
        """Every table object in the bucket and its size: everything outside the catalog's own ``_`` and ``__`` prefixes."""
        listed = [entry for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket) for entry in page.get("Contents", [])]
        return {str(entry["Key"]): int(entry["Size"]) for entry in listed if not str(entry["Key"]).startswith("_")}

    def dropped_with_a_branch(self, table: str, name: str) -> dict[str, int]:
        """``table`` registered, branched, given a maintenance policy and recoverably dropped through the doors; every object
        it holds, all under one key."""
        self.registered(table, name)
        self.door(table, "branches/create", name="work")
        answer = self.client.post(f"/management/v1/table/{quote(table, safe='$')}/policy/set", json={"retention_days": 1})
        assert answer.status_code == 200, answer.text
        self.policy_uuids[table] = str(answer.json()["uuid"])
        self.door(table, "drop")
        held = self.objects()
        assert any(f"spelled/{name}/tree/work/" in key for key in held), f"precondition: the branch is held: {sorted(held)}"
        assert all(key.startswith(f"spelled/{name}/") for key in held), f"precondition: pylance keeps the table under the decoded key: {sorted(held)}"
        return held

    def recorded(self, table: str) -> dict[str, object]:
        """The trash record the drop of ``table`` wrote."""
        dropped = trash.named(self.root, self.key, table)
        assert len(dropped) == 1, f"precondition: the drop of {table!r} kept one trash record: {dropped}"
        return dropped[0]

    def recorded_location(self, table: str) -> str:
        """The location the drop's trash record holds, as the catalog recorded it."""
        return str(self.recorded(table)["location"])


@pytest.fixture
def estate(moto_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Estate]:
    bucket = f"lh319-{uuid.uuid4().hex[:10]}"
    boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1").create_bucket(Bucket=bucket)
    for name, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{bucket}",
        "LANCE_CONTROL_ROOT": f"s3://{bucket}",
        "LANCE_S3_ENDPOINT": moto_url,
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "LANCE_TRASH_GRACE_DAYS": "1",
    }.items():
        monkeypatch.setenv(name, value)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app) as client:
        yield _Estate(moto_url, client, bucket)
    get_settings.cache_clear()


@pytest.mark.parametrize("door", ["table", "namespace"])
@pytest.mark.parametrize("made", [pytest.param(True, id="catalog-made"), pytest.param(False, id="registered")])
def test_a_dropped_table_answers_its_reads_after_its_undrop(estate: _Estate, made: bool, door: str) -> None:
    table = "db$räksmörgås"
    if made:
        estate.made(table)
    else:
        estate.registered(table, "räksmörgås")
    if door == "table":
        estate.door(table, "drop")
        undropped = estate.client.post(f"/management/v1/table/{quote(table, safe='$')}/undrop")
    else:
        assert estate.client.post("/v1/namespace/db/drop", json={"behavior": "Cascade"}).status_code == 200
        undropped = estate.client.post("/management/v1/namespace/db/undrop")
    assert undropped.status_code == 200, undropped.text

    assert estate.reads(table) == (3, 3)


@pytest.mark.parametrize("name", _NAMES)
def test_an_expired_record_reclaims_its_table_under_every_spelling_of_its_location(estate: _Estate, name: str) -> None:
    table = f"db${name}"
    held = estate.dropped_with_a_branch(table, name)
    dropped = estate.recorded(table)
    location = str(dropped["location"])
    policy_uuid = estate.policy_uuids[table]
    assert maintenance_policies.get_policy(estate.root, estate.key, "table", policy_uuid) is not None, "precondition: the drop kept the policy for an undrop"
    trash.put(estate.root, estate.key, {**dropped, "expires_at": (datetime.now(UTC) - timedelta(days=29)).isoformat()})
    # A table created since under the dropped table's name, which the purge of the dropped one must leave as it is.
    estate.made(table)
    standing = {key: size for key, size in estate.objects().items() if not key.startswith("spelled/")}
    assert standing, "precondition: the table created since holds bytes"
    report = ReconcileReport(
        checked_at=datetime.now(UTC).isoformat(),
        counts={category: 0 for category in CATEGORIES if category != "orphan_files"},
        total=0,
        skipped=[CategorySkipped(category="orphan_files", reason="off")],
    )
    settings = MaintenanceSettings.model_validate(
        {
            "s3_endpoint": estate.url,
            "s3_access_key_id": "test",
            "s3_secret_access_key": "test",
            "s3_bucket": estate.bucket,
            "policy_root": estate.root,
            "control_root": estate.root,
            "trash_purge_enabled": True,
        }
    )

    out = asyncio.run(purge_expired_trash(settings, report=report, control_root=estate.root, data_roots={estate.root}))

    assert out.refused == [], out.refused
    assert [(purged.id, purged.bytes_deleted, purged.files_deleted) for purged in out.purged] == [(table, sum(held.values()), len(held))]
    # pyarrow's S3 `delete_dir` keeps the emptied parent as a zero-byte `spelled/` marker, which is no byte of the table.
    assert [key for key in estate.objects() if key.startswith(f"spelled/{name}/")] == [], "the purge left bytes of the table it reclaimed"
    assert trash.named(estate.root, estate.key, table) == [], "the record outlived its purge"
    assert estate.reads(table) == (3, 3), "the purge reached the table created since under the name"
    assert {key: size for key, size in estate.objects().items() if not key.startswith("spelled/")} == standing
    assert maintenance_policies.get_policy(estate.root, estate.key, "table", policy_uuid) is None, "the policy outlived the table it governed"
    registry = base_registry.BaseRegistry(control_root=estate.root, storage_options=estate.key)
    assert base_registry.read_base_record(registry, location) is None, "the table's base record outlived its bytes"


@pytest.mark.parametrize("name", _NAMES)
def test_the_tombstone_probe_counts_every_object_a_record_still_names(estate: _Estate, name: str) -> None:
    table = f"db${name}"
    held = estate.dropped_with_a_branch(table, name)

    counted = objects_held(estate.recorded_location(table), estate.key)

    assert counted == len(held), f"the probe counted {counted} of the {len(held)} objects the record names: {sorted(held)}"


def test_the_compaction_gate_finds_a_dataset_rooted_at_a_base_in_the_catalogs_spelling(estate: _Estate) -> None:
    location = estate.registered("db$räksmörgås", "räksmörgås")
    assert "%C3%A4" in location, f"precondition: the catalog answers the location percent-encoded: {location}"

    rooted = dataset_root_probe(f"{estate.root}/a1b2c3d4_db$silver", estate.key)(location)

    assert rooted is True, f"{location} reads as no dataset root while pylance opens a dataset there"
