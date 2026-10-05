"""[[LH-211]] ``/commit`` holds a client-written fragment to its data files, and a forgery leaves the table as it was.

THE FORGER is a writer holding a write vend: it can put any object under the table's ``data/`` and send
any ``FragmentMetadata``. Measured through ``commit_appended_fragments`` on pylance 12.0.0 before the
fix, every case below COMMITTED, and the table then failed every read, read rows that were never
written, re-minted every field id, could not be opened, or lost its blobs, while the door emitted an
INSERT carrying the false row count.

ONE ESTATE, ON MOTO: the real catalog app over a fresh bucket, every table created through its create
door, and each fragment written straight under the table's ``data/`` by ``write_fragments`` (ingest's own
writer for the blob table) with the estate key a write vend stands in for, then forged the way the case
names. Each forgery is sent to the door and must be refused 400 ``InvalidInput`` with the latest
version unchanged and nothing left under ``_versions/`` that the version line does not hold.
"""

from __future__ import annotations

import copy
import json
import os
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import boto3
import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from ingest.catalog_service import CatalogServiceClient
from ingest.lander import write_unit_fragments
from ingest.runtime import BRONZE_SCHEMA
from ingest.worker import units_to_table
from service_kit.lakehouse.objectfs import lance_storage_options
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from storage import S3Client


_INVALID_INPUT = 13
_ROWS = pa.table({"id": pa.array([1, 2], pa.int64()), "a": ["a1", "a2"], "b": ["b1", "b2"]})
_STAGED = pa.table({"id": pa.array([3, 4], pa.int64()), "a": ["a3", "a4"], "b": ["b3", "b4"]})
#: ingest's thresholds put these inline, in one packed sidecar, and in a dedicated sidecar.
_PAYLOADS = (b"i" * 1024, b"p" * (100 * 1024), b"d" * (5 * 1024 * 1024))


class _Estate:
    """The catalog on a fresh bucket, with the estate key a write vend stands in for."""

    def __init__(self, url: str, client: TestClient, bucket: str, monkeypatch: pytest.MonkeyPatch) -> None:
        self.client = client
        self.bucket = bucket
        self.monkeypatch = monkeypatch
        #: ``LANCE_EXTERNAL_BLOB_BASES``: every table the door creates registers it.
        self.external_base = f"s3://{bucket}/models/"
        self.key = lance_storage_options(url, "test", "test", "us-east-1")
        self.s3: S3Client = boto3.client("s3", endpoint_url=url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
        assert client.post("/v1/namespace/db/create", json={}).status_code == 200

    def create(self, table: str, rows: pa.Table) -> str:
        resp = self.client.post(
            f"/v1/table/{quote(table, safe='$')}/create", content=encode_arrow_stream(rows), headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE}
        )
        assert resp.status_code == 200, resp.text
        return str(resp.json()["location"])

    def open(self, location: str) -> lance.LanceDataset:
        return lance.dataset(location, storage_options=self.key)

    def without_external_bases(self) -> None:
        """Configure no external blob base, so a table created from here registers none and the door sanctions none."""
        from catalog.core.config import get_settings

        self.monkeypatch.setenv("LANCE_EXTERNAL_BLOB_BASES", "")
        get_settings.cache_clear()

    def ids(self, location: str) -> list[int]:
        return sorted(self.open(location).to_table(columns=["id"]).column("id").to_pylist())

    def prefix(self, location: str) -> str:
        return location.removeprefix(f"s3://{self.bucket}/").rstrip("/")

    def keys(self, prefix: str) -> list[str]:
        listed = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        return [str(item["Key"]) for item in listed.get("Contents", [])]

    def version_residue(self, location: str) -> list[str]:
        """Objects under ``_versions/`` the version line does not hold: a detached manifest, or a directory marker."""
        return [key for key in self.keys(f"{self.prefix(location)}/_versions/") if key.rsplit("/", 1)[-1][:1] in ("", "d")]


@pytest.fixture
def estate(moto_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Estate]:
    bucket = f"lh211-{uuid.uuid4().hex[:10]}"
    boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1").create_bucket(Bucket=bucket)
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{bucket}",
        "LANCE_CONTROL_ROOT": f"s3://{bucket}",
        "LANCE_S3_ENDPOINT": moto_url,
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "LANCE_EXTERNAL_BLOB_BASES": f"s3://{bucket}/models/",
    }.items():
        monkeypatch.setenv(key, value)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    # A forgery the door lets through can break the read-back after the commit; that is a 500 to assert on, not an exception to abort with.
    with TestClient(app, raise_server_exceptions=False) as client:
        yield _Estate(moto_url, client, bucket, monkeypatch)
    get_settings.cache_clear()


class _Staged:
    """A table created through the door and one fragment a vended writer wrote under its ``data/``, not yet committed."""

    def __init__(self, estate: _Estate, table: str, location: str, fragment: dict[str, Any]) -> None:
        self.estate = estate
        self.table = table
        self.location = location
        self.fragment = fragment

    def copy(self) -> dict[str, Any]:
        return copy.deepcopy(self.fragment)

    @property
    def file(self) -> dict[str, Any]:
        return self.fragment["files"][0]

    def put(self, path: str, body: bytes) -> None:
        self.estate.s3.put_object(Bucket=self.estate.bucket, Key=f"{self.estate.prefix(self.location)}/data/{path}", Body=body)

    def read(self, path: str) -> bytes:
        return self.estate.s3.get_object(Bucket=self.estate.bucket, Key=f"{self.estate.prefix(self.location)}/data/{path}")["Body"].read()


def _plain(estate: _Estate, **write: Any) -> _Staged:  # noqa: ANN401 — write_fragments' own keyword arguments
    table = f"db$t{uuid.uuid4().hex[:8]}"
    location = estate.create(table, _ROWS)
    written = lance.fragment.write_fragments(_STAGED, location, storage_options=estate.key, **write)
    return _Staged(estate, table, location, json.loads(json.dumps(written[0].to_json())))


def _bronze(estate: _Estate) -> _Staged:
    table = f"db$b{uuid.uuid4().hex[:8]}"
    location = estate.create(table, BRONZE_SCHEMA.empty_table())
    batch = units_to_table([(f"file:///page-{i}.tif", payload) for i, payload in enumerate(_PAYLOADS)])
    written = write_unit_fragments(location, batch, storage_options=estate.key)
    return _Staged(estate, table, location, json.loads(written[0]))


def _bronze_without_a_base(estate: _Estate) -> _Staged:
    estate.without_external_bases()
    return _bronze(estate)


def _committed(estate: _Estate) -> _Staged:
    """A table whose first committed fragment's row 1 was then deleted, and that fragment's own file staged again."""
    table = f"db$t{uuid.uuid4().hex[:8]}"
    location = estate.create(table, _ROWS)
    estate.open(location).delete("id = 1")
    held = estate.open(location).get_fragments()[0].metadata.to_json()
    fragment = {
        **json.loads(json.dumps(held)),
        "row_id_meta": None,
        "created_at_version_meta": None,
        "last_updated_at_version_meta": None,
        "deletion_file": None,
    }
    return _Staged(estate, table, location, fragment)


def _external(uri: str) -> Callable[[_Staged], list[dict[str, Any]]]:
    """A bronze fragment whose payload names ``uri`` as an external blob, written with Lance's outside-bases bypass."""

    def forge(staged: _Staged) -> list[dict[str, Any]]:
        target = uri.format(bucket=staged.estate.bucket)
        if "secret" in target:
            staged.estate.s3.put_object(Bucket=staged.estate.bucket, Key=target.removeprefix(f"s3://{staged.estate.bucket}/"), Body=b"SECRET")
        batch = units_to_table([(target, b"")], external_base=staged.estate.external_base)
        written = lance.fragment.write_fragments(batch, staged.location, storage_options=staged.estate.key, allow_external_blob_outside_bases=True)
        return [json.loads(json.dumps(written[0].to_json()))]

    return forge


def _with(**changes: Any) -> Callable[[_Staged], list[dict[str, Any]]]:  # noqa: ANN401 — any FragmentMetadata field
    def forge(staged: _Staged) -> list[dict[str, Any]]:
        fragment = staged.copy()
        fragment.update(changes)
        return [fragment]

    return forge


def _with_file(**changes: Any) -> Callable[[_Staged], list[dict[str, Any]]]:  # noqa: ANN401 — any DataFile field
    def forge(staged: _Staged) -> list[dict[str, Any]]:
        fragment = staged.copy()
        fragment["files"][0].update(changes)
        return [fragment]

    return forge


def _rows(delta: int) -> Callable[[_Staged], list[dict[str, Any]]]:
    return lambda staged: _with(physical_rows=staged.fragment["physical_rows"] + delta)(staged)


def _bigger_than_written(staged: _Staged) -> list[dict[str, Any]]:
    return _with_file(file_size_bytes=staged.file["file_size_bytes"] + 100)(staged)


def _garbage_file(staged: _Staged) -> list[dict[str, Any]]:
    staged.put("0garbage.lance", os.urandom(64))
    return _with_file(path="0garbage.lance", file_size_bytes=64)(staged)


def _copied_row_ids(staged: _Staged) -> list[dict[str, Any]]:
    committed = staged.estate.open(staged.location).get_fragments()[0].metadata.to_json()
    return _with(row_id_meta=committed["row_id_meta"])(staged)


def _an_overlay(staged: _Staged) -> list[dict[str, Any]]:
    return _with(overlays=[{"data_file": copy.deepcopy(staged.file), "offsets": [0]}])(staged)


def _same_file_twice(staged: _Staged) -> list[dict[str, Any]]:
    fragment = staged.copy()
    fragment["files"] = [copy.deepcopy(staged.file), copy.deepcopy(staged.file)]
    return [fragment]


def _outside_data(staged: _Staged) -> list[dict[str, Any]]:
    """The writer's own file again, one directory down: the store finds it, and Lance escapes the ``/``."""
    staged.put(f"sub/{staged.file['path']}", staged.read(staged.file["path"]))
    return _with_file(path=f"sub/{staged.file['path']}")(staged)


def _declared_2_2(staged: _Staged) -> list[dict[str, Any]]:
    return _with_file(file_major_version=2, file_minor_version=2)(staged)


def _missing_sidecar(staged: _Staged) -> list[dict[str, Any]]:
    stem = staged.file["path"].removesuffix(".lance")
    sidecars = staged.estate.keys(f"{staged.estate.prefix(staged.location)}/data/{stem}/")
    assert len(sidecars) == 2, f"precondition: one packed and one dedicated sidecar, got {sidecars}"
    staged.estate.s3.delete_object(Bucket=staged.estate.bucket, Key=sidecars[0])
    return [staged.copy()]


@pytest.mark.parametrize(
    ("stage", "forge", "refusal"),
    [
        pytest.param(_plain, _bigger_than_written, "declare a file_size_bytes the object does not have", id="file-size-plus-100"),
        pytest.param(_plain, _garbage_file, "is not a readable Lance file", id="64-byte-garbage-file"),
        pytest.param(_plain, _rows(+1), "rows and the fragment declares physical_rows=3", id="physical-rows-over"),
        pytest.param(_plain, _rows(-1), "rows and the fragment declares physical_rows=1", id="physical-rows-under"),
        pytest.param(_plain, _with_file(column_indices=[5, 6, 7]), "its footer has 3 columns", id="column-indices-past-the-file"),
        pytest.param(_plain, _copied_row_ids, "carries none of row_id_meta", id="copied-row-id-meta"),
        pytest.param(_plain, _an_overlay, "carries none of overlays", id="overlay"),
        pytest.param(_plain, _with(files=[]), "no data file", id="no-data-file"),
        pytest.param(_plain, _same_file_twice, "are listed twice", id="same-file-twice"),
        pytest.param(_plain, _with_file(fields=[0, 2, 1]), "its columns are the table's field ids [0, 1, 2]", id="fields-permuted"),
        pytest.param(_plain, _with_file(fields=[5, 6, 7]), "its columns are the table's field ids [0, 1, 2]", id="fields-unknown"),
        pytest.param(_plain, _with_file(fields=[0, 1, -2]), "fields must be a non-empty list of field ids", id="fields-tombstone"),
        pytest.param(_plain, _outside_data, "is not a bare file name", id="path-outside-data"),
        pytest.param(lambda estate: _plain(estate, data_storage_version="2.1"), _declared_2_2, "its footer is file format 2.1", id="footer-2.1-declared-2.2"),
        pytest.param(lambda estate: _plain(estate, data_storage_version="2.3"), _declared_2_2, "its footer is file format 2.3", id="footer-2.3-declared-2.2"),
        pytest.param(_bronze, _missing_sidecar, "points into a blob sidecar that is missing", id="missing-blob-sidecar"),
        pytest.param(_bronze, _external("s3://{bucket}/elsewhere/secret.bin"), "an external blob is a path relative to", id="external-blob-absolute-uri"),
        pytest.param(_bronze, _external("s3://{bucket}/models/absent.bin"), "which does not exist", id="external-blob-missing-target"),
        pytest.param(
            _bronze_without_a_base, _external("s3://{bucket}/elsewhere/secret.bin"), "the table has no external blob base", id="external-blob-without-a-base"
        ),
        pytest.param(_plain, lambda staged: [staged.copy(), staged.copy()], "names the same file", id="same-file-in-two-fragments"),
        pytest.param(_committed, lambda staged: [staged.copy()], "the table already holds this file", id="re-append-a-held-file-after-a-delete"),
    ],
)
def test_a_forged_fragment_is_refused_and_the_table_is_unchanged(
    estate: _Estate, stage: Callable[[_Estate], _Staged], forge: Callable[[_Staged], list[dict[str, Any]]], refusal: str
) -> None:
    staged = stage(estate)
    before, rows = estate.open(staged.location).version, estate.ids(staged.location)
    fragments = forge(staged)

    resp = estate.client.post(f"/management/v1/table/{quote(staged.table, safe='$')}/commit", json={"fragments": fragments, "read_version": before})

    assert (resp.status_code, resp.json().get("code")) == (400, _INVALID_INPUT), resp.text
    assert refusal in resp.json()["detail"], resp.text
    assert estate.open(staged.location).version == before, "the refused commit still minted a version"
    assert estate.ids(staged.location) == rows
    assert estate.version_residue(staged.location) == []


def test_ingests_own_blob_fragments_commit_through_every_check(estate: _Estate, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real writer passes: ingest's managed blobs (inline, packed, dedicated) and an external blob under the table's
    external base, each written by ingest's own writer and committed through ingest's own catalog client."""
    staged = _bronze(estate)
    source = f"{estate.external_base}page-external.tif"
    estate.s3.put_object(Bucket=estate.bucket, Key=source.removeprefix(f"s3://{estate.bucket}/"), Body=b"external-page")
    external = write_unit_fragments(
        staged.location, units_to_table([(source, b"external-page")], external_base=estate.external_base), storage_options=estate.key
    )
    before = estate.open(staged.location).version
    token = tmp_path / "rask-catalog-token"
    token.write_text("unauthenticated-estate")
    monkeypatch.setenv("RASK_CATALOG_IDENTITY_TOKEN_FILE", str(token))
    monkeypatch.setattr("ingest.http.shared_client", lambda: estate.client)
    namespace, table = staged.table.split("$")

    version, rows = CatalogServiceClient(BRONZE_SCHEMA, base_url="http://testserver").commit(
        namespace, table, [json.dumps(staged.fragment), *external], before, "run-lh211"
    )

    assert (version, rows) == (before + 1, len(_PAYLOADS) + 1)
    landed = estate.open(staged.location)
    assert [payload for _, payload in landed.read_blobs("payload", indices=list(range(len(_PAYLOADS) + 1)))] == [*_PAYLOADS, b"external-page"]
    assert estate.version_residue(staged.location) == []
