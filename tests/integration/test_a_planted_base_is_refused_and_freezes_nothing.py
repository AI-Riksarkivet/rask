"""[[LH-279]] A base a writer planted in its own manifest is refused, reported, and freezes nothing — one test per clause.

THE PLANT is the confused-deputy write the row closes: with the table's own write credential (here the
estate key, as a write vend could), ``add_bases`` lands an ``UpdateBases`` commit naming another table's
root or the bucket root. Measured on pylance 12.0.0 (lh279 m2/m4/p1/p3): the planting table answered a
query with the victim's rows, ``/commit`` accepted a fragment spliced through the base, the sweep and the
purge refused the victim as a "shallow-clone source", and the lineage reconcile laundered the version as
maintenance.

ONE ESTATE, ON MOTO: a fresh bucket per test holding the catalog's ``dir`` namespace, its control root
(the shipped chart roots both at the bucket) and the configured external blob base ``models/``; the
real catalog app over its HTTP doors; the real sweep, purge pre-pass and lineage reconcile over the same
bucket. Every table below is created through the catalog's create door, so its base record is written by
the production writer. Each plant is asserted to be in the manifest before anything is judged, because a
fixture that fails to plant proves nothing.

The clauses (design.md §3), each one test:

(1) ``/commit`` of a fragment spliced through the planted base is refused and the table is unchanged;
(2) ``/credentials``, ``describe(vend)`` and ``query_table`` refuse the planting table 409;
(3) the victim is compacted by the sweep and at the catalog's compact door, and deletable by the purge;
(4) the reconcile names the plant — its version as a provenance hole, and the tip's base as drift;
(5) the LH-097 relation — silver shallow-cloned from bronze at a tag, recorded as ``derived_from`` —
    still keeps bronze from deletion, permits compaction while the tag holds, and refuses it without;
and, from the row's How, register location-exclusivity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import textwrap
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import boto3
import httpx
import lance
import pyarrow as pa
import pytest
import respx
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

import storage
from lineage.api import reconcile_cron
from lineage.core.config import LineageSettings, storage_options
from lineage.schemas import DatasetSummary
from maintenance.core.config import MaintenanceSettings
from maintenance.services import purge, sweep
from maintenance.services.optimize import DatasetResult, compact_one
from maintenance.services.work_queue import RETRY, ack_for
from service_kit.lakehouse import base_registry
from service_kit.lakehouse.base_refs import normalise
from service_kit.lakehouse.features import manifest_base_path_refs
from service_kit.lakehouse.objectfs import lance_storage_options
from service_kit.lakehouse.work_items import DatasetPlan, DatasetWorkItem
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from storage import S3Client


logging.getLogger("werkzeug").setLevel(logging.ERROR)

_ROWS = pa.table({"id": pa.array([1, 2, 3], pa.int64()), "ssn": ["111-victim", "222-victim", "333-victim"]})
_MINE = pa.table({"id": pa.array([100], pa.int64()), "ssn": ["mine"]})
_QUERY = {"k": 10, "vector": {"single_vector": []}}
_INVALID_TABLE_STATE = 19
_INVALID_INPUT = 13


def _s3(url: str) -> S3Client:
    return boto3.client("s3", endpoint_url=url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")


class _Estate:
    """The catalog on a fresh bucket, and three tables created through it: the victim, a bystander, the attacker."""

    def __init__(self, url: str, client: TestClient, bucket: str) -> None:
        self.url = url
        self.client = client
        self.bucket = bucket
        self.root = f"s3://{bucket}"
        self.models = f"{self.root}/models/"
        self.key = lance_storage_options(url, "test", "test", "us-east-1")
        self.s3 = _s3(url)
        self.registry = base_registry.BaseRegistry(control_root=self.root, storage_options=self.key)
        assert client.post("/v1/namespace/db/create", json={}).status_code == 200
        self.victim = self.create("db$victim", fragments=3)
        self.bystander = self.create("db$bystander", fragments=3)
        self.attacker = self.create("db$attacker", rows=_MINE)
        #: The attacker's version before anything was planted on it.
        self.clean_version = self.open(self.attacker).version

    def create(self, table: str, *, rows: pa.Table = _ROWS, fragments: int = 1) -> str:
        resp = self.client.post(
            f"/v1/table/{quote(table, safe='$')}/create", content=encode_arrow_stream(rows), headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE}
        )
        assert resp.status_code == 200, resp.text
        location = str(resp.json()["location"])
        for _ in range(1, fragments):
            self.open(location).insert(rows)
        return location

    def open(self, uri: str) -> lance.LanceDataset:
        return lance.dataset(uri, storage_options=self.key)

    def maintenance(self, **extra: Any) -> MaintenanceSettings:
        return MaintenanceSettings.model_validate(
            {
                "s3_endpoint": self.url,
                "s3_access_key_id": "test",
                "s3_secret_access_key": "test",
                "s3_bucket": self.bucket,
                "policy_root": self.root,
                "control_root": self.root,
                "LANCE_EXTERNAL_BLOB_BASES": self.models,
                **extra,
            }
        )


@pytest.fixture
def estate(moto_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Estate]:
    bucket = f"lh279-{uuid.uuid4().hex[:10]}"
    _s3(moto_url).create_bucket(Bucket=bucket)
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{bucket}",
        "LANCE_CONTROL_ROOT": f"s3://{bucket}",
        "LANCE_S3_ENDPOINT": moto_url,
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_EXTERNAL_BLOB_BASES": f"s3://{bucket}/models/",
        "LANCE_MULTIBASE_DATA_BASES": f"s3://{bucket}/second/data",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "LANCE_TRASH_GRACE_DAYS": "1",
    }.items():
        monkeypatch.setenv(key, value)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app) as client:
        yield _Estate(moto_url, client, bucket)
    get_settings.cache_clear()


@pytest.fixture(params=["another_table", "bucket_root"])
def planted(request: pytest.FixtureRequest, estate: _Estate) -> str:
    """The estate key's ``add_bases`` on the attacker: another table's root, or the whole bucket."""
    base, is_root = (estate.victim, True) if request.param == "another_table" else (estate.root, False)
    estate.open(estate.attacker).add_bases([lance.DatasetBasePath(base, name="pin", is_dataset_root=is_root)])
    declared = [normalise(ref.path) for ref in manifest_base_path_refs(estate.open(estate.attacker))]
    assert normalise(base) in declared, "precondition: the plant must be in the attacker's manifest, or nothing below is tested"
    return base


def _spliced_fragment(estate: _Estate, base: str) -> dict[str, Any]:
    """The victim's first fragment, its data files re-pointed through the planted base — what a write vend can forge.

    Through a bucket-root base the file's path must name the victim's directory, which pylance escapes
    (``victim%2Fdata%2F…``), so that splice cannot resolve; the refusal must cover it all the same.
    """
    pin = next(key for key, declared in estate.open(estate.attacker)._ds.base_paths().items() if declared.name == "pin")
    prefix = "" if base == estate.victim else f"{estate.victim.removeprefix(f'{estate.root}/')}/data/"
    fragment = json.loads(json.dumps(estate.open(estate.victim).get_fragments()[0].metadata.to_json()))
    for data_file in fragment["files"]:
        data_file["base_id"] = pin
        data_file["path"] = f"{prefix}{data_file['path']}"
    for key in ("row_id_meta", "created_at_version_meta", "last_updated_at_version_meta"):
        fragment.pop(key, None)
    return fragment


def _splice(estate: _Estate, base: str) -> None:
    """Commit the spliced fragment DIRECTLY, as a write vend reaching ``_versions/`` can ([[LH-202]]) — through a root plant."""
    if base != estate.victim:
        return
    fragment = lance.FragmentMetadata.from_json(json.dumps(_spliced_fragment(estate, base)))
    attacker = estate.open(estate.attacker)
    lance.LanceDataset.commit(estate.attacker, lance.LanceOperation.Append([fragment]), read_version=attacker.version, storage_options=estate.key)
    rows = estate.open(estate.attacker).to_table().column("ssn").to_pylist()
    assert "222-victim" in rows, f"precondition: the splice makes the victim's rows readable through the attacker, got {rows}"


def _refused(resp: Any, code: int) -> None:  # noqa: ANN401 — an httpx.Response from the TestClient
    assert resp.status_code == (409 if code == _INVALID_TABLE_STATE else 400), f"{resp.status_code}: {resp.text}"
    assert resp.json()["code"] == code, resp.text


def test_a_commit_through_the_planted_base_is_refused_and_the_table_is_unchanged(estate: _Estate, planted: str) -> None:
    """Closes-when (1). The decoy under the attacker's own ``data/`` is what the file-existence check would find.

    A based file's path resolves through its base, not under the table's ``data/``, so an existence check
    alone answers for the decoy; the refusal of the ``base_id`` itself is what this test can fail on.
    """
    fragment = _spliced_fragment(estate, planted)
    prefix = estate.attacker.removeprefix(f"{estate.root}/")
    for data_file in fragment["files"]:
        estate.s3.put_object(Bucket=estate.bucket, Key=f"{prefix}/data/{data_file['path']}", Body=b"decoy")
    before = estate.open(estate.attacker).version

    resp = estate.client.post("/management/v1/table/db$attacker/commit", json={"fragments": [fragment], "read_version": before})

    _refused(resp, _INVALID_INPUT)
    assert estate.open(estate.attacker).version == before, "the refused commit still minted a version"
    assert "222-victim" not in estate.open(estate.attacker).to_table().column("ssn").to_pylist()


class _ThrottledRecords:
    """An S3 client whose every GET under ``_bases/`` answers SlowDown: the store failing, not the record."""

    def __init__(self, inner: S3Client) -> None:
        self._inner = inner

    def get_object(self, **request: str) -> object:
        if "_bases/" in request.get("Key", ""):
            raise ClientError({"Error": {"Code": "SlowDown", "Message": "Please reduce your request rate."}}, "GetObject")
        return self._inner.get_object(**request)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def _maintained_through(client: TestClient, estate: _Estate, token_file: Path) -> DatasetResult:
    """Maintenance's real unit on the attacker, its credential asked of this catalog's own vend door over HTTP.

    ``token_file`` stands in for the pod's projected `rask-catalog` token: without one maintenance sends no request.
    """

    def _forward(request: httpx.Request) -> httpx.Response:
        answer = client.post(request.url.path, params=dict(request.url.params))
        return httpx.Response(answer.status_code, content=answer.content, headers={"content-type": answer.headers.get("content-type", "")})

    token_file.write_text("sa-token\n")
    settings = estate.maintenance(catalog_url="http://catalog.test", catalog_identity_token_file=str(token_file))
    item = DatasetWorkItem(uri=estate.attacker, table_id="db$attacker", plan=DatasetPlan(older_than=timedelta(days=7)))
    with respx.mock(base_url="http://catalog.test") as catalog:
        catalog.post("/management/v1/table/db$attacker/credentials").mock(side_effect=_forward)
        return sweep.maintain_one_item(item, settings=settings, options=settings.storage_options())


def test_the_vend_and_read_doors_refuse_the_planting_table(estate: _Estate, planted: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Closes-when (2): no credential for the planting table, and no row read through it.

    Nor through the deployment's ambient key: maintenance, the one consumer holding it, stops on the refusal,
    and on a door that could not decide — a 5xx, the catalog's own when the record behind the judge cannot be
    read — rather than signing the rewrite itself.
    """
    _splice(estate, planted)

    _refused(estate.client.post("/management/v1/table/db$attacker/credentials"), _INVALID_TABLE_STATE)
    _refused(estate.client.post("/v1/table/db$attacker/describe", params={"vend_credentials": "true"}), _INVALID_TABLE_STATE)
    _refused(estate.client.post("/v1/table/db$attacker/query", json=_QUERY), _INVALID_TABLE_STATE)
    catalog = TestClient(estate.client.app, raise_server_exceptions=False)
    assert _maintained_through(catalog, estate, tmp_path / "rask-catalog-token").refused_by == "vend_denied", "the refusal became an ambient-key rewrite"

    # The table's own path in another store is not its own: a `file://` base reads the catalog pod's disk. On a
    # table declaring no other base, so nothing but that base's standing decides whether it is judged at all.
    lance.write_dataset(_MINE, f"{estate.root}/plain", storage_options=estate.key, data_storage_version="2.2", enable_stable_row_ids=True)
    assert estate.client.post("/v1/table/db$plain/register", json={"location": "plain"}).status_code == 200
    estate.open(f"{estate.root}/plain").add_bases([lance.DatasetBasePath(f"file:///{estate.bucket}/plain", name="local", is_dataset_root=True)])
    _refused(estate.client.post("/v1/table/db$plain/query", json=_QUERY), _INVALID_TABLE_STATE)

    # An encoded dot segment defeats a SPELLING comparison but not the object store. `<own>/%2e%2e/<victim>`
    # is textually inside db$escape's own root, and pylance 12.0.0 percent-decodes and resolves it to the
    # victim (lh279 r3 a_escape), so a fragment spliced through it reads the victim's rows. Judged own by the
    # spelling the doors would vend and read the victim through db$escape's own credential; the base names no
    # location (a `%` escape), so credentials, describe(vend) and query refuse it 409.
    escape = estate.create("db$escape", rows=_MINE)
    victim_dir = estate.victim.removeprefix(f"{estate.root}/")
    estate.open(escape).add_bases([lance.DatasetBasePath(f"{escape}/%2e%2e/{victim_dir}", name="pin", is_dataset_root=True)])
    handle = estate.open(escape)
    pin = next(key for key, declared in handle._ds.base_paths().items() if declared.name == "pin")
    fragment = json.loads(json.dumps(estate.open(estate.victim).get_fragments()[0].metadata.to_json()))
    for data_file in fragment["files"]:
        data_file["base_id"] = pin
    for meta_key in ("row_id_meta", "created_at_version_meta", "last_updated_at_version_meta"):
        fragment.pop(meta_key, None)
    lance.LanceDataset.commit(
        escape, lance.LanceOperation.Append([lance.FragmentMetadata.from_json(json.dumps(fragment))]), read_version=handle.version, storage_options=estate.key
    )
    assert "222-victim" in estate.open(escape).to_table().column("ssn").to_pylist(), "precondition: the encoded-dot base reads the victim through db$escape"
    _refused(estate.client.post("/management/v1/table/db$escape/credentials"), _INVALID_TABLE_STATE)
    _refused(estate.client.post("/v1/table/db$escape/describe", params={"vend_credentials": "true"}), _INVALID_TABLE_STATE)
    _refused(estate.client.post("/v1/table/db$escape/query", json=_QUERY), _INVALID_TABLE_STATE)

    # A store failing under the record the judge must read: a typed, retryable 503, and the unit stops.
    real_client = storage.s3_client
    monkeypatch.setattr(storage, "s3_client", lambda *args, **kwargs: _ThrottledRecords(real_client(*args, **kwargs)))
    unjudged = catalog.post("/management/v1/table/db$attacker/credentials")
    assert unjudged.status_code == 503 and unjudged.json()["title"] == "ServiceUnavailableError", unjudged.text
    stopped = _maintained_through(catalog, estate, tmp_path / "rask-catalog-token")
    assert (ack_for(stopped), stopped.error_type) == (RETRY, "VendUndecided"), f"a door that could not decide was signed around: {stopped}"


def test_the_victim_is_maintained_and_deletable_under_the_plant(estate: _Estate, planted: str) -> None:
    """Closes-when (3), at each consumer of the protection verdict: the sweep, the catalog's compact door, the purge."""
    settings = estate.maintenance()

    swept = {result.uri: result for result in sweep.run_sweep(settings)}
    for table in (estate.victim, estate.bystander):
        assert swept[table].refused_by != "protected_base", f"{table} is frozen by the plant: {swept[table].refused}"
        assert swept[table].fragments_removed > 0, f"precondition: {table} had fragments to merge, so a pass that ran is visible"
    assert estate.client.post("/management/v1/table/db$victim/maintenance/compact", json={}).status_code == 200

    deleted, files = purge.delete_location(estate.victim, estate.key, protected=purge._estate_base_refs({estate.root}, estate.key, settings=settings))

    assert deleted > 0 and files > 0, "the victim's bytes were kept for a relation nobody sanctioned"


class _Graph:
    """The lineage graph as the reconcile reads it: every version of every table up to the plant, and what it back-fills."""

    def __init__(self, estate: _Estate) -> None:
        self.names = {"db$victim": estate.victim, "db$bystander": estate.bystander, "db$attacker": estate.attacker}
        self.versions = {name: set(range(1, estate.open(uri).version + 1)) for name, uri in self.names.items()}
        self.versions["db$attacker"] = set(range(1, estate.clean_version + 1))

    async def list_datasets(self, namespace: str | None = None, tag: str | None = None) -> list[DatasetSummary]:
        return [DatasetSummary(name=name) for name in self.names]

    async def source_uri(self, name: str) -> str | None:
        return self.names[name]

    async def dropped_at(self, name: str) -> str | None:
        return None

    async def record_observed_drop(self, name: str, uri: str, observed_at: str) -> bool:
        return True

    async def latest_write_version(self, name: str) -> int | None:
        return max(self.versions[name])

    async def write_versions(self, name: str) -> set[int]:
        return set(self.versions[name])

    async def backfill_write(self, name: str, version: int, schema: object | None = None) -> None:
        self.versions[name].add(version)


def test_the_reconcile_names_the_plant(estate: _Estate, planted: str) -> None:
    """Closes-when (4), through the cron's own sweep reading the catalog's control root and configured bases.

    Two axes, because retention separates them. The planting version changes no data counter, so only its
    bases tell it from compaction's; it must be reported, never back-filled as maintenance. And after an
    append and ``cleanup_old_versions(older_than=0)`` that version is gone while the tip still declares the
    base (lh279 p2), so the drift is a state compare against the record.
    """
    settings = LineageSettings.model_validate(
        {
            "LINEAGE_S3_ENDPOINT": estate.url,
            "LINEAGE_S3_ACCESS_KEY_ID": "test",
            "LINEAGE_S3_SECRET_ACCESS_KEY": "test",
            "LINEAGE_S3_BUCKET": estate.bucket,
            "LINEAGE_CONTROL_ROOT": estate.root,
            "LANCE_EXTERNAL_BLOB_BASES": estate.models,
        }
    )
    opts = storage_options(settings)
    graph = _Graph(estate)
    plant_version = estate.open(estate.attacker).version

    report = reconcile_cron.summarize_sweep(asyncio.run(reconcile_cron._sweep(cast(Any, graph), settings, opts)))

    assert report.provenance_holes == {"db$attacker": [plant_version]}, "the planting version was laundered as maintenance"
    assert report.base_drift == {"db$attacker": [normalise(planted)]}

    estate.open(estate.attacker).insert(_MINE)
    estate.open(estate.attacker).cleanup_old_versions(older_than=timedelta(0), error_if_tagged_old_versions=False)
    assert plant_version not in {v["version"] for v in estate.open(estate.attacker).versions()}, "precondition: retention reclaimed the plant"

    report = reconcile_cron.summarize_sweep(asyncio.run(reconcile_cron._sweep(cast(Any, graph), settings, opts)))

    assert report.base_drift == {"db$attacker": [normalise(planted)]}


def _reads_cold(uri: str, key: dict[str, str]) -> list[int]:
    """The rows at ``uri`` from a FRESH interpreter: Lance caches per process, so only a cold read is about the files."""
    script = textwrap.dedent(
        f"""
        import json, lance
        print(json.dumps(sorted(lance.dataset({uri!r}, storage_options={key!r}).to_table().column("id").to_pylist())))
        """
    )
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=180, check=False)
    assert done.returncode == 0, f"{uri} no longer opens cold: {done.stderr[-800:]}"
    return json.loads(done.stdout.strip())


def test_a_recorded_clone_source_is_still_protected(estate: _Estate) -> None:
    """Closes-when (5): the LH-097 relation protects bronze per verb, and only while its pin tag holds.

    Deletion is always refused. Compaction AND version reclamation with ``older_than=0`` are permitted while
    the tag is on bronze — reclamation keeps a tagged version's files, and silver reads through exactly that
    version (lh279 p4) — so silver must still read cold afterwards. Without the tag, compaction is refused.
    A branch is the relation that needs no record: its base is its parent's root, in the parent's store, so
    the parent is neither deleted nor compacted beside it.

    A name the catalog percent-encodes into its location (``bronze tier`` -> ``bronze%20tier``, ``räksmörgås``
    -> ``r%C3%A4ksm%C3%B6rg%C3%A5s``, ``a b`` -> ``a%20b``) is protected exactly as ``bystander`` is. The
    record, the pin and a branch's base carry that spelling while discovery lists the decoded directory the
    store holds (lh279 r6 spellings), so a verdict that compares spellings rather than places reclaims a
    branched parent's versions under its live branch (lh279 rvw finding1).
    """
    bronze = estate.create("db$bronze tier", fragments=3)
    source = estate.open(bronze)
    source.tags.create("silver-src", source.version)
    silver = f"{estate.root}/4e5f6a7b_db$silver%20tier"
    source.shallow_clone(silver, "silver-src", storage_options=estate.key)
    base_registry.claim_bases(
        estate.registry,
        silver,
        [
            base_registry.RecordedBase(
                path=bronze,
                role=base_registry.BaseRole.DERIVED_FROM,
                is_dataset_root=True,
                origin=base_registry.BaseOrigin.SILVER,
                source_table=bronze,
                tag="silver-src",
            )
        ],
    )
    parents = [estate.bystander, estate.create("db$räksmörgås", fragments=3), estate.create("db$a b", fragments=3)]
    for parent in parents:
        estate.open(parent).create_branch("work")
    settings = estate.maintenance()
    refs = purge._estate_base_refs({estate.root}, estate.key, settings=settings)

    for parent in parents:
        assert compact_one(parent, estate.key, timedelta(0), protected=refs).refused_by == "protected_base", f"{parent} was reclaimed under its live branch"
    for location in (bronze, *parents):
        with pytest.raises(purge.ProtectedBaseError):
            purge.delete_location(location, estate.key, protected=refs)
    compacted = compact_one(bronze, estate.key, timedelta(0), protected=refs)
    assert compacted.refused_by is None, compacted.refused
    assert compacted.fragments_removed > 0 and compacted.old_versions_removed > 0, "precondition: bronze was compacted AND reclaimed"
    assert _reads_cold(silver, estate.key) == sorted([1, 2, 3] * 3), "the pinned relation did not keep silver readable"

    estate.open(bronze).tags.delete("silver-src")

    unpinned = compact_one(bronze, estate.key, timedelta(days=7), protected=purge._estate_base_refs({estate.root}, estate.key, settings=settings))
    assert unpinned.refused_by == "protected_base", "a pin with no tag must not permit compacting the source"


def _registered(estate: _Estate, table: str, location: str) -> str:
    """A dataset written at ``location`` with its own directory beneath the approved data base, registered as ``table``, and the record that earned."""
    uri = f"{estate.root}/{location}"
    lance.write_dataset(
        _MINE,
        uri,
        initial_bases=[lance.DatasetBasePath(f"{estate.root}/second/data/{location}", name="second", is_dataset_root=False)],
        storage_options=estate.key,
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    assert estate.client.post(f"/v1/table/{table}/register", json={"location": location}).status_code == 200
    record = base_registry.read_base_record(estate.registry, uri)
    assert record is not None and [entry.role for entry in record.entries] == [base_registry.BaseRole.DATA], f"precondition: {record}"
    return uri


def _purge_expired(estate: _Estate, table: str, location: str) -> None:
    """The maintenance purge of ``table``'s trash record, stamped long expired, under a clean drift report."""
    from maintenance.services.reconcile import CATEGORIES, CategorySkipped, ReconcileReport
    from service_kit.lakehouse import trash

    trash.put(
        estate.root,
        estate.key,
        trash.make_record(table, location=location, dropped_by="user:alice", grace_days=1, now=datetime.now(UTC) - timedelta(days=30)),
    )
    report = ReconcileReport(
        checked_at=datetime.now(UTC).isoformat(),
        counts={category: 0 for category in CATEGORIES if category != "orphan_files"},
        total=0,
        skipped=[CategorySkipped(category="orphan_files", reason="off")],
    )
    settings = estate.maintenance(trash_purge_enabled=True)
    out = asyncio.run(purge.purge_expired_trash(settings, report=report, control_root=estate.root, data_roots={estate.root}))
    assert [record.id for record in out.purged] == [table], f"precondition: the purge reclaimed {table}: {out}"


def test_a_table_location_is_exclusive(estate: _Estate) -> None:
    """The register door's location-exclusivity (Lakekeeper's rule): the only thing keeping ``_bases/`` out of every vend.

    The ``dir`` backend accepts a register at the namespace root, at a control prefix and at another table's
    directory (lh279 m4), and a write vend covers the registered prefix (lh279 s1). It also accepts a spelling
    that opens another table's directory: pylance 12.0.0 drops the tab from ``x/.\\t./<victim>`` (and a line
    feed or carriage return likewise) and resolves the ``..`` left behind (lh279 r4 register_probe). Each
    segment is judged as it reads after one percent-decode, so ``%2e%2e`` is refused as ``..`` and ``%5Fbases``
    as ``_bases``. A location over the model registry is refused too ([[LH-204]]): it is opened by explicit
    URI and never registered, so a table there would be vended every model's history.

    A table whose NAME the catalog percent-encodes into its location (``räksmörgås`` ->
    ``r%C3%A4ksm%C3%B6rg%C3%A5s``, ``growth%`` -> ``growth%25``) is still its own: it is created with a
    readable record, and a branch of it, whose manifest declares the root spelled so, is vended.

    And a location belongs to the table registered there only while that table exists: a table DESTROYED
    — by the catalog's destructive drop, its namespace's destructive cascade, the maintenance purge of
    its trash record, or a warehouse delete that clears its trash and purges its bucket — takes its base
    record with its bytes, or the next table registered at the location would inherit bases nobody judged
    for it.
    """
    from catalog.services import warehouses
    from service_kit.lakehouse import trash

    victim = estate.victim.removeprefix(f"{estate.root}/")
    for location in (".", "_bases", "%5Fbases", victim, f"{victim}/tree", f"x/.\t./{victim}", f"x/.\n./{victim}", f"x/%2e%2e/{victim}", "medallion/models/m"):
        resp = estate.client.post("/v1/table/db$squatter/register", json={"location": location})
        assert resp.status_code == 400, f"{location!r}: {resp.status_code} {resp.text}"
        assert estate.client.post("/v1/table/db$squatter/exists").status_code == 404, f"{location!r}: a refused registration must be detached"
        assert estate.client.post("/v1/table/db$squatter/query", json=_QUERY).status_code == 404, f"{location!r}: the victim reads through the refused id"
    lance.write_dataset(_MINE, f"{estate.root}/fresh", storage_options=estate.key, data_storage_version="2.2", enable_stable_row_ids=True)
    assert estate.client.post("/v1/table/db$fresh/register", json={"location": "fresh"}).status_code == 200, "the control: a location of its own registers"
    for named in ("db$räksmörgås", "db$growth%"):
        location = estate.create(named)
        estate.open(location).create_branch("work")
        vend = estate.client.post(f"/management/v1/table/{quote(named, safe='$')}/credentials", params={"branch": "work"})
        assert vend.status_code == 200, f"{named!r} at {location!r}: {vend.status_code} {vend.text}"
        assert base_registry.read_base_record(estate.registry, location) is not None, f"{named!r}: the create door wrote no record for {location!r}"

    dropped = _registered(estate, "db$dropped", "dropped")
    assert estate.client.post("/v1/table/db$dropped/drop", params={"purge": "true"}).status_code == 200
    assert estate.client.post("/v1/namespace/gone/create", json={}).status_code == 200
    cascaded = _registered(estate, "gone$cascaded", "cascaded")
    assert estate.client.post("/v1/namespace/gone/drop", params={"purge": "true"}, json={"behavior": "Cascade"}).status_code == 200
    purged = _registered(estate, "db$purged", "purged")
    assert estate.client.post("/v1/table/db$purged/drop").status_code == 200, "a recoverable drop, which keeps the record for an undrop"
    assert base_registry.read_base_record(estate.registry, purged) is not None, "precondition: the recoverable drop kept the record"
    _purge_expired(estate, "db$purged", purged)

    for destroyed in (dropped, cascaded, purged):
        assert base_registry.read_base_record(estate.registry, destroyed) is None, f"{destroyed} left its base record behind"

    # The fifth destroy path: a table recoverably dropped inside a warehouse, whose trash record a
    # warehouse delete clears (warehouses.delete_warehouse_record -> _clear_trash_under) and whose bytes a
    # purge_bucket then removes. The maintenance purge never sees it, so the base record leaks unless the
    # warehouse delete forgets it. Driven at delete_warehouse_record with the bucket as the id, which is
    # its fallback when no warehouse record exists — the seam the review named, without the admin API.
    warehoused = _registered(estate, "db$warehoused", "warehoused")
    trash.put(estate.root, estate.key, trash.make_record("db$warehoused", location=warehoused, dropped_by="user:alice", grace_days=1))
    assert base_registry.read_base_record(estate.registry, warehoused) is not None, "precondition: the trashed table kept its record"
    warehouses.delete_warehouse_record(estate.root, estate.key, estate.bucket)
    assert base_registry.read_base_record(estate.registry, warehoused) is None, "the warehouse delete left the trashed table's base record behind"
