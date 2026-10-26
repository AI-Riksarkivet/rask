"""[[LH-211]] [[LH-471]] ``CreateTableVersion`` holds a client-written fragment to its data files, and a forgery leaves the table as it was.

THE FORGER is a writer holding a write vend: it can put any object under the table's ``data/`` and send
any ``FragmentMetadata``. Measured through the catalog's client-direct append on pylance 12.0.0 before the
judgement, every case below COMMITTED (a file listed twice inside one fragment did too; pylance 13.0.0's commit
collapses it to one entry, measured, so it is no longer a case), and the table then failed every read, read rows that were never
written, re-minted every field id, could not be opened, or lost its blobs, while the door emitted an
INSERT carrying the false row count.

ONE ESTATE, ON MOTO: the real catalog app served by uvicorn over a fresh bucket, every table created through its
create door, and each fragment written straight under the table's ``data/`` by ``write_fragments`` with the estate
key a write vend stands in for, then forged the way the case names. Each forgery is committed by stock pylance as an
``Append`` through the namespace, so it reaches the catalog as a staged manifest at ``version/create`` (ADR 0157). It
must be refused with the latest version unchanged and nothing left under ``_versions/`` that the version line does not
hold. pylance reports a refused finalize as an unknown outcome and keeps its staged manifest, so the catalog's answer is
read by asking for that manifest again: 400 ``InvalidInput`` naming the forgery.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import quote

import boto3
import httpx
import lance
import lance_namespace
import pyarrow as pa
import pytest
import uvicorn
from lance import blob_array, blob_field
from lance.blob import Blob

from service_kit.lakehouse.objectfs import lance_storage_options
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from storage import S3Client


_INVALID_INPUT = 13
_ROWS = pa.table({"id": pa.array([1, 2], pa.int64()), "a": ["a1", "a2"], "b": ["b1", "b2"]})
_STAGED = pa.table({"id": pa.array([3, 4], pa.int64()), "a": ["a3", "a4"], "b": ["b3", "b4"]})
#: A blob-v2 bronze table, its placement thresholds named rather than inherited: a client writing bronze through the
#: catalog's doors chooses them, and the guide stores them in the dataset schema and refuses an append naming others.
_BLOB_SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64(), nullable=False, metadata={"lance-schema:unenforced-primary-key": "true"}),
        pa.field("source_uri", pa.string()),
        blob_field("payload", nullable=False, inline_size_threshold=64 * 1024, dedicated_size_threshold=4 * 1024 * 1024),
    ]
)
#: Those thresholds put these inline, in one packed sidecar, and in a dedicated sidecar.
_PAYLOADS = (b"i" * 1024, b"p" * (100 * 1024), b"d" * (5 * 1024 * 1024))


def _blob_rows(units: list[tuple[str, bytes]], external_base: str | None = None) -> pa.Table:
    """Bronze rows for ``(source uri, bytes)`` units: an EXTERNAL descriptor of the uri when the table has a base, else the bytes."""
    return pa.table(
        {
            "id": pa.array([int(hashlib.sha256(uri.encode()).hexdigest()[:15], 16) for uri, _ in units], pa.int64()),
            "source_uri": pa.array([uri for uri, _ in units], pa.string()),
            "payload": blob_array([Blob.from_uri(uri) for uri, _ in units] if external_base else [payload for _, payload in units]),
        }
    )


def _write(location: str, rows: pa.Table, key: dict[str, str]) -> list[dict[str, Any]]:
    """Fragments a vended writer puts under the table's ``data/``, as the JSON the commit door takes."""
    return [json.loads(json.dumps(fragment.to_json())) for fragment in lance.fragment.write_fragments(rows, location, storage_options=key)]


class _Estate:
    """The catalog on a fresh bucket, with the estate key a write vend stands in for."""

    def __init__(self, url: str, client: httpx.Client, bucket: str, monkeypatch: pytest.MonkeyPatch) -> None:
        self.client = client
        self.stock = lance_namespace.connect("rest", {"uri": str(client.base_url)})
        self.bucket = bucket
        self.monkeypatch = monkeypatch
        #: The external blob base a bronze create names, inside the approved corpus bucket ([[LH-209]]).
        self.external_base = f"s3://{bucket}-corpus/media/pages/"
        self.key = lance_storage_options(url, "test", "test", "us-east-1")
        self.s3: S3Client = boto3.client("s3", endpoint_url=url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
        assert client.post("/v1/namespace/db/create", json={}).status_code == 200

    def create(self, table: str, rows: pa.Table, external_blob_base: str | None = None) -> str:
        """The create door, with the external blob base requested as a query parameter."""
        resp = self.client.post(
            f"/v1/table/{quote(table, safe='$')}/create",
            content=encode_arrow_stream(rows),
            params={"external_blob_base": external_blob_base} if external_blob_base else None,
            headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE},
        )
        assert resp.status_code == 200, resp.text
        return str(resp.json()["location"])

    def open(self, location: str) -> lance.LanceDataset:
        return lance.dataset(location, storage_options=self.key)

    def put_uri(self, uri: str, body: bytes) -> None:
        bucket, _, key = uri.removeprefix("s3://").partition("/")
        self.s3.put_object(Bucket=bucket, Key=key, Body=body)

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
    s3 = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    for name in (bucket, f"{bucket}-corpus"):
        s3.create_bucket(Bucket=name)
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{bucket}",
        "LANCE_CONTROL_ROOT": f"s3://{bucket}",
        "LANCE_S3_ENDPOINT": moto_url,
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "LANCE_EXTERNAL_BLOB_BASES": f"s3://{bucket}/models/,s3://{bucket}-corpus/media/",
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
        yield _Estate(moto_url, client, bucket, monkeypatch)
    server.should_exit = True
    thread.join(timeout=30)
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


def _bronze(estate: _Estate, *, external_base: str | None = None) -> _Staged:
    """A bronze table created with the external base, when one is named, and one managed-blob fragment staged under it."""
    table = f"db$b{uuid.uuid4().hex[:8]}"
    location = estate.create(table, _BLOB_SCHEMA.empty_table(), external_base)
    written = _write(location, _blob_rows([(f"file:///page-{i}.tif", payload) for i, payload in enumerate(_PAYLOADS)]), estate.key)
    return _Staged(estate, table, location, written[0])


def _bronze_with_a_base(estate: _Estate) -> _Staged:
    return _bronze(estate, external_base=estate.external_base)


def _bronze_with_a_planted_base(estate: _Estate) -> _Staged:
    """A bronze table with its own external base, to which the writer then adds another table's source prefix inside the
    same approved entry: sanctioned by configuration for reads, never recorded for this table."""
    staged = _bronze_with_a_base(estate)
    victim = f"s3://{estate.bucket}-corpus/media/victim/"
    estate.open(staged.location).add_bases([lance.DatasetBasePath(victim, name="planted", is_dataset_root=False)])
    estate.put_uri(f"{victim}scan.bin", b"VICTIM-SCAN")
    return staged


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


def _commit(staged: _Staged, fragments: list[dict[str, Any]], read_version: int) -> httpx.Response | None:
    """Commit ``fragments`` as an Append through the namespace; on a refusal, the catalog's answer to the retained staged manifest."""
    table_id = staged.table.split("$")
    operation = lance.LanceOperation.Append([lance.FragmentMetadata.from_json(json.dumps(fragment)) for fragment in fragments])
    try:
        lance.LanceDataset.commit(
            staged.location,
            operation,
            read_version=read_version,
            storage_options=staged.estate.key,
            namespace_client=staged.estate.stock,
            table_id=table_id,
            namespace_client_managed_versioning=True,
        )
    except OSError:
        retained = [key for key in staged.estate.keys(f"{staged.estate.prefix(staged.location)}/_versions/") if ".manifest-" in key]
        assert len(retained) == 1, retained
        manifest_path = f"{staged.estate.bucket}/{retained[0]}".partition("/")[2]
        return staged.estate.client.post(
            f"/v1/table/{quote(staged.table, safe='$')}/version/create", json={"version": read_version + 1, "manifest_path": manifest_path}
        )
    return None


def _external(uri: str) -> Callable[[_Staged], list[dict[str, Any]]]:
    """A bronze fragment whose payload names ``uri`` as an external blob, written with Lance's outside-bases bypass."""

    def forge(staged: _Staged) -> list[dict[str, Any]]:
        target = uri.format(bucket=staged.estate.bucket, base=staged.estate.external_base)
        if "secret" in target:
            staged.estate.put_uri(target, b"SECRET")
        batch = _blob_rows([(target, b"")], external_base=staged.estate.external_base)
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
        pytest.param(_plain, _copied_row_ids, "reuse stable row ids below the table's next row id", id="copied-row-id-meta"),
        pytest.param(_plain, _an_overlay, "the staged manifest cannot be opened", id="overlay"),
        pytest.param(_plain, _with(files=[]), "name no data file", id="no-data-file"),
        pytest.param(_plain, _with_file(fields=[0, 2, 1]), "its columns are the table's field ids [0, 1, 2]", id="fields-permuted"),
        pytest.param(_plain, _with_file(fields=[5, 6, 7]), "its columns are the table's field ids [0, 1, 2]", id="fields-unknown"),
        pytest.param(_plain, _with_file(fields=[0, 1, -2]), "fields must be a non-empty list of field ids", id="fields-tombstone"),
        pytest.param(_plain, _outside_data, "is not a bare file name", id="path-outside-data"),
        pytest.param(lambda estate: _plain(estate, data_storage_version="2.1"), _declared_2_2, "its footer is file format 2.1", id="footer-2.1-declared-2.2"),
        pytest.param(lambda estate: _plain(estate, data_storage_version="2.3"), _declared_2_2, "its footer is file format 2.3", id="footer-2.3-declared-2.2"),
        pytest.param(_bronze, _missing_sidecar, "points into a blob sidecar that is missing", id="missing-blob-sidecar"),
        pytest.param(
            _bronze_with_a_base, _external("s3://{bucket}/elsewhere/secret.bin"), "an external blob is a path relative to", id="external-blob-absolute-uri"
        ),
        pytest.param(_bronze_with_a_base, _external("{base}absent.bin"), "which does not exist", id="external-blob-missing-target"),
        pytest.param(
            _bronze_with_a_planted_base,
            _external("s3://{bucket}-corpus/media/victim/scan.bin"),
            "an external blob is a path relative to",
            id="external-blob-at-another-tables-object-through-a-planted-base",
        ),
        pytest.param(_bronze, _external("s3://{bucket}/elsewhere/secret.bin"), "the table has no external blob base", id="external-blob-without-a-base"),
        pytest.param(_plain, lambda staged: [staged.copy(), staged.copy()], "one data file is named twice", id="same-file-in-two-fragments"),
        pytest.param(_committed, lambda staged: [staged.copy()], "names data file(s) the table already holds", id="re-append-a-held-file-after-a-delete"),
    ],
)
def test_a_forged_fragment_is_refused_and_the_table_is_unchanged(
    estate: _Estate, stage: Callable[[_Estate], _Staged], forge: Callable[[_Staged], list[dict[str, Any]]], refusal: str
) -> None:
    staged = stage(estate)
    before, rows = estate.open(staged.location).version, estate.ids(staged.location)
    fragments = forge(staged)

    resp = _commit(staged, fragments, before)

    assert resp is not None, "the forged commit was finalized"
    assert (resp.status_code, resp.json().get("code")) == (400, _INVALID_INPUT), resp.text
    assert refusal in resp.json()["detail"], resp.text
    assert estate.open(staged.location).version == before, "the refused commit still minted a version"
    assert estate.ids(staged.location) == rows
    assert estate.version_residue(staged.location) == []


def test_a_writers_own_blob_fragments_commit_through_every_check(estate: _Estate) -> None:
    """The honest writer passes: a bronze table created through the door with its approved external base, then managed blobs
    (inline, packed, dedicated) and an external blob under that base, each written under ``data/`` and committed through the
    same door the forgeries are refused at."""
    table = f"db$b{uuid.uuid4().hex[:8]}"
    location = estate.create(table, _BLOB_SCHEMA.empty_table(), estate.external_base)
    source = f"{estate.external_base}page-external.tif"
    estate.put_uri(source, b"external-page")
    managed = _write(location, _blob_rows([(f"file:///page-{i}.tif", payload) for i, payload in enumerate(_PAYLOADS)]), estate.key)
    external = _write(location, _blob_rows([(source, b"external-page")], external_base=estate.external_base), estate.key)
    before = estate.open(location).version

    refused = _commit(_Staged(estate, table, location, managed[0]), [*managed, *external], before)

    assert refused is None, refused.text
    landed = estate.open(location)
    assert landed.version == before + 1
    assert landed.count_rows() == len(_PAYLOADS) + 1
    assert [payload for _, payload in landed.read_blobs("payload", indices=list(range(len(_PAYLOADS) + 1)))] == [*_PAYLOADS, b"external-page"]
    assert estate.version_residue(location) == []


def test_a_writers_own_fragment_of_a_packed_struct_commits(estate: _Estate) -> None:
    """A struct Lance packs is one column of the file (`lance_docs/file_format.md` § Field IDs: "Only leaf fields and
    packed structs have column indices"), at the top level and inside a struct Lance does not pack ([[LH-246]])."""
    inner = pa.struct([pa.field("a", pa.int64()), pa.field("b", pa.string())])
    packed = {"lance-encoding:packed": "true"}
    schema = pa.schema(
        [
            pa.field("id", pa.int64()),
            pa.field("p", pa.struct([pa.field("x", pa.float32()), pa.field("label", pa.string())]), metadata=packed),
            pa.field("q", pa.struct([pa.field("y", pa.float32()), pa.field("inner", inner, metadata=packed)])),
        ]
    )
    table = f"db$p{uuid.uuid4().hex[:8]}"
    location = estate.create(table, schema.empty_table())
    rows = pa.Table.from_pylist(
        [{"id": i, "p": {"x": float(i), "label": f"l{i}"}, "q": {"y": float(i), "inner": {"a": i, "b": f"b{i}"}}} for i in (1, 2)], schema=schema
    )
    staged = _write(location, rows, estate.key)
    before = estate.open(location).version

    refused = _commit(_Staged(estate, table, location, staged[0]), staged, before)

    assert refused is None, refused.text
    assert estate.ids(location) == [1, 2]
