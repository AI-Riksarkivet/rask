"""[[LH-279]] A base a writer planted in its own manifest is refused, reported, and freezes nothing — one test per clause.

THE PLANT is the confused-deputy write the row closes: with the table's own write credential (here the
estate key, as a write vend could), ``add_bases`` lands an ``UpdateBases`` commit naming another table's
root or the bucket root. Measured on pylance 12.0.0 (lh279 m2/m4/p1/p3): the planting table answered a
query with the victim's rows, ``/commit`` accepted a fragment spliced through the base, and the sweep and
the purge refused the victim as a "shallow-clone source".

ONE ESTATE, ON MOTO: a fresh bucket per test holding the catalog's ``dir`` namespace, its control root
(the shipped chart roots both at the bucket) and the configured external blob base ``models/``; the
real catalog app over its HTTP doors; the real sweep and purge pre-pass over the same bucket. Every table below is created through the catalog's create door, so its base record is written by
the production writer. Each plant is asserted to be in the manifest before anything is judged, because a
fixture that fails to plant proves nothing.

The clauses (design.md §3), each one test:

(1) ``/commit`` of a fragment spliced through the planted base is refused and the table is unchanged;
(2) ``/credentials``, ``describe(vend)`` and ``query_table`` refuse the planting table 409;
(3) the victim is compacted by the sweep and at the catalog's compact door, and deletable by the purge;
(4) the maintenance pass names the plant: the tip's base is reported as unrecorded;
(5) a shallow clone registered through the door keeps the source version it reads by a tag (ADR 0198 §1);
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
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import boto3
import httpx
import lance
import pyarrow as pa
import pytest
import respx
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from lance_namespace import InvalidTableStateError, LanceNamespace, TableNotFoundError
from pydantic import SecretStr

import lakehouse.storage as storage
from catalog.core.config import get_settings
from catalog.services import clone_endings as clone_endings_door
from catalog.services import dataplane, native
from lakehouse import base_registry, clone_endings, clone_sources, location_claims, records, trash
from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from lakehouse.base_refs import normalise
from lakehouse.features import manifest_base_path_refs
from lakehouse.governed.audit import AUDIT_LOGGER, AuditAction, AuditOutcome
from lakehouse.governed.store_credential import S3KeyPair
from lakehouse.naming import UUID_PROPERTY
from lakehouse.objectfs import lance_storage_options
from lakehouse.storage import S3Client
from lakehouse.storage_options import StorageOptions
from maintenance.core.config import MaintenanceSettings
from maintenance.services import clone_endings as clone_endings_audit
from maintenance.services import purge, table_pass
from maintenance.services.due import TableIds
from maintenance.services.expiry import DatasetResult
from maintenance.services.naming import claim_store, policy_owners
from maintenance.services.plan import PlanContext
from tests.integration.maintenance_vend_doors import CATALOG_URL, catalog_doors


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
        self.key = lance_storage_options(url, S3KeyPair(access_key_id="test", secret_access_key=SecretStr("test")), "us-east-1")
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
        return lance.dataset(uri, storage_options=self.key.in_clear())

    def namespace_uuid(self, namespace: str) -> str:
        resp = self.client.post(f"/v1/namespace/{namespace}/describe", json={})
        assert resp.status_code == 200, resp.text
        return str(resp.json()["properties"][UUID_PROPERTY])

    def table_uuid(self, table: str) -> str:
        resp = self.client.post(f"/v1/table/{quote(table, safe='$')}/describe", json={})
        assert resp.status_code == 200, resp.text
        return str(resp.json()["properties"][UUID_PROPERTY])

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
    pin = next(key for key, declared in estate.open(estate.attacker).base_paths().items() if declared.name == "pin")
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
    lance.LanceDataset.commit(estate.attacker, lance.LanceOperation.Append([fragment]), read_version=attacker.version, storage_options=estate.key.in_clear())
    rows = estate.open(estate.attacker).to_table().column("ssn").to_pylist()
    assert "222-victim" in rows, f"precondition: the splice makes the victim's rows readable through the attacker, got {rows}"


def _refused(resp: Any, code: int) -> None:  # noqa: ANN401 — an httpx.Response from the TestClient
    assert resp.status_code == (409 if code == _INVALID_TABLE_STATE else 400), f"{resp.status_code}: {resp.text}"
    assert resp.headers["content-type"] == "application/problem+json", resp.headers
    assert resp.json()["code"] == code, resp.text


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
    """Maintenance's real pass on the attacker, its credential asked of this catalog's own vend door over HTTP.

    ``token_file`` stands in for the pod's projected `rask-catalog` token: without one maintenance sends no request.
    """

    def _forward(request: httpx.Request) -> httpx.Response:
        answer = client.post(request.url.path, params=dict(request.url.params))
        return httpx.Response(answer.status_code, content=answer.content, headers={"content-type": answer.headers.get("content-type", "")})

    token_file.write_text("sa-token\n")
    # Every version but the current one reclaimable, and one enough to expire: the pass asks for a credential.
    settings = estate.maintenance(catalog_url="http://catalog.test", catalog_identity_token_file=str(token_file), min_versions_to_expire=1)
    now = datetime.now(UTC)
    # The namespace's uuid as its `__manifest` row holds it, read here through the catalog's describe door; the
    # table's is in the claim its create took (ADR 0173 §8).
    ids = TableIds(by_location={normalise(estate.attacker): "db$attacker"}, namespace_uuids={"db": estate.namespace_uuid("db")})
    readings = table_pass.PassReadings(
        table_ids=ids,
        context=PlanContext(policies=[], now=now, older_than=timedelta(0)),
        table="db$attacker",
        owners=policy_owners(ids, "db$attacker", claims=claim_store(settings)),
    )
    # Asked only when the pass finds something to write; a refusal's `vend_denied` is the door's own answer.
    with respx.mock(base_url="http://catalog.test", assert_all_called=False) as catalog:
        catalog.post("/management/v1/table/db$attacker/credentials").mock(side_effect=_forward)
        return table_pass.maintain_dataset(estate.attacker, readings=readings, settings=settings)


def test_the_vend_and_read_doors_refuse_the_planting_table(estate: _Estate, planted: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Closes-when (2): no credential for the planting table, and no row read through it.

    Nor through the deployment's ambient key: maintenance, the one consumer holding it, stops on the refusal,
    and on a door that could not decide — a 5xx, the catalog's own when the record behind the judge cannot be
    read — rather than signing the pass itself.
    """
    _splice(estate, planted)

    _refused(estate.client.post("/management/v1/table/db$attacker/credentials"), _INVALID_TABLE_STATE)
    _refused(estate.client.post("/v1/table/db$attacker/describe", params={"vend_credentials": "true"}), _INVALID_TABLE_STATE)
    _refused(estate.client.post("/v1/table/db$attacker/query", json=_QUERY), _INVALID_TABLE_STATE)
    catalog = TestClient(estate.client.app, raise_server_exceptions=False)
    assert _maintained_through(catalog, estate, tmp_path / "rask-catalog-token").refused_by == "vend_denied", "the refusal became an ambient-key pass"

    # The table's own path in another store is not its own: a `file://` base reads the catalog pod's disk. On a
    # table declaring no other base, so nothing but that base's standing decides whether it is judged at all.
    lance.write_dataset(_MINE, f"{estate.root}/plain", storage_options=estate.key.in_clear(), data_storage_version="2.2", enable_stable_row_ids=True)
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
    pin = next(key for key, declared in handle.base_paths().items() if declared.name == "pin")
    fragment = json.loads(json.dumps(estate.open(estate.victim).get_fragments()[0].metadata.to_json()))
    for data_file in fragment["files"]:
        data_file["base_id"] = pin
    for meta_key in ("row_id_meta", "created_at_version_meta", "last_updated_at_version_meta"):
        fragment.pop(meta_key, None)
    lance.LanceDataset.commit(
        escape,
        lance.LanceOperation.Append([lance.FragmentMetadata.from_json(json.dumps(fragment))]),
        read_version=handle.version,
        storage_options=estate.key.in_clear(),
    )
    assert "222-victim" in estate.open(escape).to_table().column("ssn").to_pylist(), "precondition: the encoded-dot base reads the victim through db$escape"
    _refused(estate.client.post("/management/v1/table/db$escape/credentials"), _INVALID_TABLE_STATE)
    _refused(estate.client.post("/v1/table/db$escape/describe", params={"vend_credentials": "true"}), _INVALID_TABLE_STATE)
    _refused(estate.client.post("/v1/table/db$escape/query", json=_QUERY), _INVALID_TABLE_STATE)

    # A `?` names no location: the store keeps a `?` spelling under a re-encoded name (measured on pylance 13.0.0, [[LH-435]]),
    # so comparing the spelling cannot say where the base opens. Inside db$marked's own root as spelled, it is refused 409
    # at each door, by the same rule as the escape above ([[LH-510]]).
    marked = estate.create("db$marked", rows=_MINE)
    estate.open(marked).add_bases([lance.DatasetBasePath(f"{marked}/sub?x", name="pin", is_dataset_root=False)])
    declared = [ref.path for ref in manifest_base_path_refs(estate.open(marked))]
    assert any(path.rstrip("/").endswith(("sub?x", "sub%3Fx")) for path in declared), f"precondition: the base must be declared, got {declared}"
    _refused(estate.client.post("/management/v1/table/db$marked/credentials"), _INVALID_TABLE_STATE)
    _refused(estate.client.post("/v1/table/db$marked/describe", params={"vend_credentials": "true"}), _INVALID_TABLE_STATE)
    _refused(estate.client.post("/v1/table/db$marked/query", json=_QUERY), _INVALID_TABLE_STATE)

    # A store failing under the record the judge must read: a typed, retryable 503, and the pass stops as a failure.
    real_client = storage.s3_client
    monkeypatch.setattr(storage, "s3_client", lambda *args, **kwargs: _ThrottledRecords(real_client(*args, **kwargs)))
    unjudged = catalog.post("/management/v1/table/db$attacker/credentials")
    assert unjudged.status_code == 503 and unjudged.json()["title"] == "ServiceUnavailableError", unjudged.text
    stopped = _maintained_through(catalog, estate, tmp_path / "rask-catalog-token")
    assert (stopped.error or "").startswith("maintain:") and stopped.error_type == "VendUndecided", f"a door that could not decide was signed around: {stopped}"


def test_the_victim_is_maintained_and_deletable_under_the_plant(estate: _Estate, planted: str) -> None:
    """Closes-when (3), at each consumer of a table's bytes: its own pass and the purge. A table's pass reads no other
    table's manifest, so another table's plant cannot freeze it, and the purge deletes what its record names."""
    settings = estate.maintenance()

    for table, location in (("db$victim", estate.victim), ("db$bystander", estate.bystander)):
        (result,) = table_pass.run_table_pass(table_pass.TableTask(uuid=estate.table_uuid(table), location=location), settings).results
        assert result.refused is None, f"{table} is frozen by the plant: {result.refused}"
        assert result.due == "due", f"precondition: {table} had fragments to merge, so its pass lists it for compaction"

    deleted, files = purge.delete_location(estate.victim, estate.key)

    assert deleted > 0 and files > 0, "the victim's bytes were kept for a relation nobody sanctioned"


def test_the_maintenance_pass_names_the_plant(estate: _Estate, planted: str, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Closes-when (4), on the planting table's own pass: the planted base is reported as unrecorded.

    A state compare against the table's base record, not a version count: after an append and
    ``cleanup_old_versions(older_than=0)`` the planting version is gone while the tip still declares the base
    (lh279 p2), and the base is still named.
    """
    catalog = TestClient(estate.client.app, raise_server_exceptions=False)
    plant_version = estate.open(estate.attacker).version

    def unrecorded() -> set[tuple[str, str]]:
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="maintenance.services.table_pass"):
            _maintained_through(catalog, estate, tmp_path / "rask-catalog-token")
        found = [record for record in caplog.records if record.message == "maintenance_unrecorded_base"]
        return {(normalise(str(getattr(record, "referrer", ""))), normalise(base)) for record in found for base in getattr(record, "bases", [])}

    assert (normalise(estate.attacker), normalise(planted)) in unrecorded(), "the planted base was not reported"

    estate.open(estate.attacker).insert(_MINE)
    estate.open(estate.attacker).cleanup_old_versions(older_than=timedelta(0), error_if_tagged_old_versions=False)
    assert plant_version not in {v["version"] for v in estate.open(estate.attacker).versions()}, "precondition: retention reclaimed the plant"

    assert (normalise(estate.attacker), normalise(planted)) in unrecorded(), "the tip still declares the base, and it went unreported"


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


#: What the registered clone reads: its source's three fragments of ``_ROWS``.
_CLONED_IDS = sorted([1, 2, 3] * 3)


def _registered_clone(estate: _Estate, table: str = "db$silver", source: str = "db$bronze tier") -> tuple[str, str]:
    """A ``source`` made through the catalog and a shallow clone of its latest version registered as ``table``: the
    source's location and the clone's.

    The source's name is one the catalog percent-encodes into its location (``bronze tier``), as the claim, the tag and
    the clone's base all spell it while the store keeps the decoded directory (lh279 r6 spellings).
    """
    bronze = estate.create(source, fragments=3)
    clone = f"{estate.root}/silver"
    source = estate.open(bronze)
    source.shallow_clone(clone, source.version, storage_options=estate.key.in_clear())
    registered = estate.client.post(f"/v1/table/{table}/register", json={"location": "silver"})
    assert registered.status_code == 200, registered.text
    return bronze, clone


def test_a_registered_clone_reads_after_its_source_is_cleaned(estate: _Estate) -> None:
    """Closes-when (c) of ADR 0198 §1: the register door tags the source version a shallow clone reads, so the clone
    reads cold after its source is overwritten, compacted and cleaned at age 0."""
    bronze, clone = _registered_clone(estate)
    lance.write_dataset(_MINE, bronze, mode="overwrite", storage_options=estate.key.in_clear())
    estate.open(bronze).optimize.compact_files()

    estate.open(bronze).cleanup_old_versions(older_than=timedelta(0), delete_unverified=True, error_if_tagged_old_versions=False)

    assert _reads_cold(clone, estate.key.in_clear()) == _CLONED_IDS, "the tag did not keep the version the clone reads"


def _cleaned_at_age_zero(estate: _Estate, source: str) -> None:
    """Overwrite ``source`` and reclaim every version nothing keeps, as its maintenance would once they aged out."""
    lance.write_dataset(_MINE, source, mode="overwrite", storage_options=estate.key.in_clear())
    estate.open(source).cleanup_old_versions(older_than=timedelta(0), delete_unverified=True, error_if_tagged_old_versions=False)


def test_a_clone_of_a_clone_reads_after_the_clone_between_is_deregistered(estate: _Estate) -> None:
    """A clone of a clone reads the first source's files where they are, so its registration tags that source too: the
    end of the clone between them releases only that clone's own tag."""
    bronze, silver = _registered_clone(estate)
    gold = f"{estate.root}/gold"
    between = estate.open(silver)
    between.shallow_clone(gold, between.version, storage_options=estate.key.in_clear())
    assert estate.client.post("/v1/table/db$gold/register", json={"location": "gold"}).status_code == 200
    assert estate.client.post("/v1/table/db$silver/deregister", json={}).status_code == 200

    _cleaned_at_age_zero(estate, bronze)

    assert _reads_cold(gold, estate.key.in_clear()) == _CLONED_IDS, "the first source kept no version for the clone of its clone"


def test_a_clone_registered_again_once_its_first_version_is_gone_keeps_its_source(estate: _Estate) -> None:
    """A clone's own expiry removes the version whose transaction names its source, so a later registration of it tags
    the source from the catalog's record of the first: it is never governed with no tag."""
    bronze, clone = _registered_clone(estate)
    for _ in range(2):
        estate.open(clone).insert(_MINE)
    estate.open(clone).cleanup_old_versions(older_than=timedelta(0), delete_unverified=True, error_if_tagged_old_versions=False)
    held = _reads_cold(clone, estate.key.in_clear())
    assert estate.client.post("/v1/table/db$silver/deregister", json={}).status_code == 200
    registered = estate.client.post("/v1/table/db$silver/register", json={"location": "silver"})

    _cleaned_at_age_zero(estate, bronze)

    assert registered.status_code == 200, registered.text
    assert _reads_cold(clone, estate.key.in_clear()) == held, "the clone was registered again with no tag on its source"


def test_a_clone_of_a_source_branch_is_registered_and_keeps_the_branch_version(estate: _Estate) -> None:
    """A clone made from a source's branch names the branch's directory, inside the source's root: its registration tags
    the branch version on the source table, and the clone reads after the source is cleaned."""
    bronze = estate.create("db$bronze", fragments=3)
    clone = f"{estate.root}/silver"
    estate.open(bronze).create_branch("exp", estate.open(bronze).version, storage_options=estate.key.in_clear())
    branch = estate.open(bronze).checkout_version(("exp", None))
    branch.shallow_clone(clone, ("exp", branch.version), storage_options=estate.key.in_clear())

    registered = estate.client.post("/v1/table/db$silver/register", json={"location": "silver"})

    assert registered.status_code == 200, registered.text
    tag = estate.open(bronze).tags.list()[clone_sources.tag_of(clone)]
    assert (tag["branch"], tag["version"]) == ("exp", branch.version)
    _cleaned_at_age_zero(estate, bronze)
    assert _reads_cold(clone, estate.key.in_clear()) == _CLONED_IDS


def test_a_source_a_registered_clone_reads_is_not_destroyed(estate: _Estate) -> None:
    """A drop destroying a table's bytes is refused while a registered clone reads through them."""
    _, clone = _registered_clone(estate)

    refused = estate.client.post("/v1/table/db$bronze%20tier/drop", params={"purge": "true"}, json={})

    _refused(refused, _INVALID_TABLE_STATE)
    assert _reads_cold(clone, estate.key.in_clear()) == _CLONED_IDS, "the refused drop changed what the clone reads"


def test_a_cascade_destroying_a_source_a_clone_outside_it_reads_is_refused(estate: _Estate) -> None:
    """A namespace cascade that would destroy a table's bytes is refused while a registered clone outside the namespace
    reads through them (``clone_sources.destroy_order``), before it destroys anything: its other tables stand."""
    assert estate.client.post("/v1/namespace/lake/create", json={}).status_code == 200
    others = ["lake$alpha", "lake$omega"]
    for table in others:
        estate.create(table)
    _, clone = _registered_clone(estate, source="lake$bronze tier")

    refused = estate.client.post("/v1/namespace/lake/drop", params={"purge": "true"}, json={"behavior": "Cascade"})

    _refused(refused, _INVALID_TABLE_STATE)
    standing = [estate.client.post(f"/v1/table/{table}/describe", json={}).status_code for table in others]
    assert standing == [200, 200], "the refused cascade destroyed a table first"
    assert _reads_cold(clone, estate.key.in_clear()) == _CLONED_IDS, "the refused cascade changed what the clone reads"


def test_a_trashed_source_a_clone_reads_is_vended_no_session_to_delete_its_bytes(estate: _Estate) -> None:
    """The trash's reclaim door vends no session deleting a released table's bytes while a registered clone reads them
    (``trash_reclaim.clone_refusal``), and notes the refusal on the record."""
    _, clone = _registered_clone(estate)
    assert estate.client.post("/v1/table/db$bronze%20tier/drop", json={}).status_code == 200
    (record,) = trash.named(estate.root, estate.key, "db$bronze tier", kind="table")
    past = datetime.now(UTC) - timedelta(days=1)
    trash.put(estate.root, estate.key, {**record, "expires_at": past.isoformat(), trash.RELEASED_AT: past.isoformat()})

    refused = estate.client.post(f"/management/v1/trash/table/{trash.object_uuid(record)}/credentials", json={})

    _refused(refused, _INVALID_TABLE_STATE)
    (noted,) = trash.named(estate.root, estate.key, "db$bronze tier", kind="table")
    assert (noted["attempts"], noted["last_refusal"]["reason"]) == (
        1,
        "1 registered clone(s) read the table's files; its bytes go once those clones are dropped or deregistered",
    )
    assert _reads_cold(clone, estate.key.in_clear()) == _CLONED_IDS


@pytest.mark.parametrize(("purge", "tags"), [pytest.param("true", 0, id="a-destroyed-clone-frees-it"), pytest.param("false", 1, id="a-trashed-clone-keeps-it")])
def test_a_clones_end_frees_its_source(estate: _Estate, purge: str, tags: int) -> None:
    """The clone's own end deletes the tag it holds, so its source keeps no version for it. A clone in the trash can be
    undropped, so its source keeps the version it reads until the trash ends the clone."""
    bronze, _ = _registered_clone(estate)

    dropped = estate.client.post("/v1/table/db$silver/drop", params={"purge": purge}, json={})

    assert dropped.status_code == 200, dropped.text
    assert len([tag for tag in estate.open(bronze).tags.list() if tag.startswith(clone_sources.TAG_PREFIX)]) == tags


def _a_store_that_will_not_delete_a_tag(source_location: str, tag: str, *, storage_options: StorageOptions) -> bool:
    raise OSError(f"the store did not answer the delete of {tag} on {source_location}")


def _clone_tags(estate: _Estate, source: str) -> list[str]:
    return [tag for tag in estate.open(source).tags.list() if tag.startswith(clone_sources.TAG_PREFIX)]


def _finished_by_maintenance(estate: _Estate) -> clone_endings_audit.CloneEndingsReport:
    """Maintenance's audit step asking the real catalog to finish every clone's ending it lists, once a claim's lease has
    passed since each was filed."""
    settings = estate.maintenance(catalog_url=CATALOG_URL)
    with catalog_doors(estate.client):
        return asyncio.run(clone_endings_audit.finish_clone_endings(settings))


def _ended_by(estate: _Estate, path: str, params: dict[str, str]) -> int:
    """The status a clone's end at ``path`` answers; an error the door does not handle is the 500 a server answers it with."""
    body = {"behavior": "Cascade"} if path.startswith("/v1/namespace/") else {}
    try:
        return estate.client.post(path, params=params, json=body).status_code
    except OSError:
        return 500


@pytest.mark.parametrize(
    ("table", "path", "params", "answered"),
    [
        pytest.param("db$silver", "/v1/table/db$silver/deregister", {}, 503, id="deregister"),
        pytest.param("db$silver", "/v1/table/db$silver/drop", {"purge": "true"}, 500, id="destroying-drop"),
        pytest.param("clones$silver", "/v1/namespace/clones/drop", {"purge": "true"}, 500, id="destroying-namespace-cascade"),
    ],
)
def test_a_clones_end_that_cannot_delete_its_tag_is_finished_by_maintenance(
    estate: _Estate, monkeypatch: pytest.MonkeyPatch, table: str, path: str, params: dict[str, str], answered: int
) -> None:
    """A door ending a clone files the clone's ending before its row goes (ADR 0198 §1, owner 2026-11-10 "Intent
    record"): a tag the store will not delete at that door is left standing with the ending and the clone's location
    held, so no clone registered there next takes a tag of its name, the door answering as it did; maintenance's audit
    then has the catalog release it, and forget the base record where the door destroyed the bytes, after which the
    source can be destroyed. The fourth
    door, the warehouse cascade, is `test_a_warehouse_cascade_revokes_a_grant_only_once_its_object_is_gone.py`'s."""
    assert estate.client.post("/v1/namespace/clones/create", json={}).status_code == 200
    bronze, clone = _registered_clone(estate, table)
    claims = location_claims.ClaimStore(control_root=estate.root, storage_options=estate.key)
    real_delete = clone_sources.delete_tag
    monkeypatch.setattr(clone_sources, "delete_tag", _a_store_that_will_not_delete_a_tag)
    assert _ended_by(estate, path, params) == answered
    held = location_claims.holder(claims, clone)
    assert (clone_endings.stands_at(claims, clone), len(_clone_tags(estate, bronze))) == (True, 1), "precondition: the end left its tag"
    assert held is not None and held.table == table, "the clone's location was freed with its tag still standing"
    monkeypatch.setattr(clone_sources, "delete_tag", real_delete)
    monkeypatch.setattr(location_claims, "LEASE", timedelta(0))

    finished = _finished_by_maintenance(estate)

    assert ([ending.split("/")[0] for ending in finished.finished], finished.refused) == ([clone_endings.digest_of(clone)], {}), finished
    recorded = base_registry.read_base_record(estate.registry, clone) is not None
    assert (clone_endings.stands_at(claims, clone), _clone_tags(estate, bronze), recorded) == (False, [], path.endswith("/deregister"))
    destroyed = estate.client.post("/v1/table/db$bronze%20tier/drop", params={"purge": "true"}, json={})
    assert destroyed.status_code == 200, destroyed.text


def _claims(estate: _Estate) -> location_claims.ClaimStore:
    return location_claims.ClaimStore(control_root=estate.root, storage_options=estate.key)


def _ending_left_by_a_deregister(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str, str]:
    """A registered clone whose deregister could not delete its tag (503): the source, the clone, and the finish door's
    path for the ending the door left."""
    bronze, clone = _registered_clone(estate)
    real_delete = clone_sources.delete_tag
    monkeypatch.setattr(clone_sources, "delete_tag", _a_store_that_will_not_delete_a_tag)
    assert estate.client.post("/v1/table/db$silver/deregister", json={}).status_code == 503
    monkeypatch.setattr(clone_sources, "delete_tag", real_delete)
    ((digest, filing),) = clone_endings.standing(_claims(estate))
    return bronze, clone, f"/management/v1/clone-endings/{digest}/{filing}/finish"


def _a_backend_that_will_not_deregister(real_call: Callable[..., object]) -> Callable[..., object]:
    def _call(ns: LanceNamespace, operation: str, *args: object) -> object:
        if operation == "deregister_table":
            raise OSError("the namespace backend did not answer")
        return real_call(ns, operation, *args)

    return _call


def _a_backend_that_removes_the_row_then_fails(real_call: Callable[..., object]) -> Callable[..., object]:
    """A backend whose first deregister removes the row and then answers nothing."""
    removed: list[bool] = []

    def _call(ns: LanceNamespace, operation: str, *args: object) -> object:
        if operation == "deregister_table" and not removed:
            real_call(ns, operation, *args)
            removed.append(True)
            raise OSError("the namespace backend answered nothing after removing the row")
        return real_call(ns, operation, *args)

    return _call


def _a_judgement_refusing_the_drop(*_args: object, **_kwargs: object) -> None:
    raise InvalidTableStateError("the table's location is read by a registered clone, so the drop deletes nothing")


@pytest.mark.parametrize(
    ("table", "path"),
    [
        pytest.param("db$silver", "/v1/table/db$silver/drop", id="drop-refused-as-a-clones-source"),
        pytest.param("clones$silver", "/v1/namespace/clones/drop", id="namespace-cascade-refused-by-a-judgement"),
    ],
)
def test_a_door_refused_before_its_row_would_go_files_no_ending(estate: _Estate, monkeypatch: pytest.MonkeyPatch, table: str, path: str) -> None:
    """A destructive drop and a destroying cascade file a clone's ending only once every judgement of the drop has passed,
    right before the row goes: a judgement's refusal files none, so none refuses a registration at the location or is
    asked about by the audit. The clone keeps its row and its tag."""
    assert estate.client.post("/v1/namespace/clones/create", json={}).status_code == 200
    bronze, clone = _registered_clone(estate, table)
    if "namespace" in path:
        monkeypatch.setattr(dataplane, "require_no_clone_reading", _a_judgement_refusing_the_drop)
    else:
        silver = estate.open(clone)
        silver.shallow_clone(f"{estate.root}/gold", silver.version, storage_options=estate.key.in_clear())
        assert estate.client.post("/v1/table/db$gold/register", json={"location": "gold"}).status_code == 200

    status = _ended_by(estate, path, {"purge": "true"})

    described = estate.client.post(f"/v1/table/{table}/describe", json={}).status_code
    held = clone_sources.tag_of(clone) in _clone_tags(estate, bronze)
    assert (status, described, clone_endings.standing(_claims(estate)), held) == (409, 200, [], True)


def test_a_drop_whose_clone_ending_cannot_be_filed_changes_nothing(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """A destructive drop files a clone's ending as its last step before the row goes: a filing the store refuses answers
    503 code 17 with the row in place, no residue record kept for the table, and no ending standing."""
    bronze, clone = _registered_clone(estate)

    def _a_store_that_will_not_file(*_args: object, **_kwargs: object) -> object:
        raise OSError("the control root did not take the ending")

    monkeypatch.setattr(clone_endings, "file", _a_store_that_will_not_file)

    dropped = estate.client.post("/v1/table/db$silver/drop", params={"purge": "true"}, json={})

    described = estate.client.post("/v1/table/db$silver/describe", json={}).status_code
    residue = trash.named(estate.root, estate.key, "db$silver", kind="table")
    assert (dropped.status_code, dropped.json().get("code"), described, residue, clone_endings.standing(_claims(estate))) == (503, 17, 200, [], [])
    assert _clone_tags(estate, bronze) == [clone_sources.tag_of(clone)]


def test_a_registration_refused_by_another_requests_fence_releases_nothing(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """A registration whose take of the location's claim is refused (another request fences the claim of the clone whose
    row is gone) took no claim, so its undo releases none: the claim still records the clone's uuid, held and fenced,
    after it and after a second registration of the same name refused the same way."""
    _, clone = _registered_clone(estate)
    claims = _claims(estate)
    owned = location_claims.holder(claims, clone)
    assert owned is not None and owned.uuid is not None
    real_call = native.call
    monkeypatch.setattr(native, "call", _a_backend_that_removes_the_row_then_fails(real_call))
    with pytest.raises(OSError, match="after removing the row"):
        estate.client.post("/v1/table/db$silver/deregister", json={})
    monkeypatch.setattr(native, "call", real_call)
    read_as = location_claims.read(claims, clone)
    location_claims.fence(claims, clone, read_as, table="db$silver", segments=["db", "silver"], token="f" * 32, is_gone=lambda _claim: False)

    answers = [estate.client.post("/v1/table/db$silver/register", json={"location": "silver"}) for _ in range(2)]

    held = location_claims.holder(claims, clone)
    assert [(answer.status_code, answer.json().get("code")) for answer in answers] == [(409, 14), (409, 14)], [answer.text for answer in answers]
    assert held is not None and (held.uuid, held.fence.token if held.fence else None) == (owned.uuid, "f" * 32)
    assert estate.client.post("/v1/table/db$silver/describe", json={}).status_code == 404


def test_a_destroying_cascade_releases_only_the_claim_of_the_table_it_identified(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """A table's bytes can take longer to go than the fence a destroying namespace cascade holds its claim by stands. Once
    that fence has lapsed the same name can take the location's claim, under a uuid of its own: the cascade releases the
    claim only under the uuid it listed the table by, and the new holder keeps it."""
    assert estate.client.post("/v1/namespace/scratch/create", json={}).status_code == 200
    location = estate.create("scratch$t")
    real_forget = base_registry.forget_base_record
    taken: list[str] = []

    def _forget_then_the_same_name_takes_the_location(registry: base_registry.BaseRegistry, at: str, *, table: str) -> bool:
        forgotten = real_forget(registry, at, table=table)
        taken.append(str(uuid.uuid4()))
        location_claims.take(_claims(estate), at, table, ["scratch", "t"], is_gone=lambda _claim: True, uuid=taken[0])
        return forgotten

    monkeypatch.setattr(base_registry, "forget_base_record", _forget_then_the_same_name_takes_the_location)
    # No fence outlives the read that finds it: the cascade's has lapsed by the time its bytes are gone.
    monkeypatch.setattr(location_claims, "LEASE", timedelta(0))

    dropped = estate.client.post("/v1/namespace/scratch/drop", params={"purge": "true"}, json={"behavior": "Cascade"})

    held = location_claims.holder(_claims(estate), location)
    assert dropped.status_code == 200, dropped.text
    assert (len(taken), held.uuid if held is not None else None) == (1, taken[0] if taken else None)


def test_a_finish_releases_nothing_when_the_clones_claim_moved_after_its_read(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """The finish acts only on the claim it read, fenced under that read's ETag: a registration of the same name taking
    the location (a new uuid) after the read answers the finish 409 (code 14), the new holder keeps the location, and the
    tag of its name stands."""
    bronze, clone, finish = _ending_left_by_a_deregister(estate, monkeypatch)
    monkeypatch.setattr(location_claims, "LEASE", timedelta(0))
    real_read = location_claims.read
    taken: list[str] = []

    def _read_then_a_registration_takes_the_location(store: location_claims.ClaimStore, location: str) -> location_claims.ClaimRead | None:
        found = real_read(store, location)
        if not taken:
            taken.append(str(uuid.uuid4()))
            location_claims.take(store, location, "db$silver", ["db", "silver"], is_gone=lambda _claim: True, uuid=taken[0])
        return found

    monkeypatch.setattr(location_claims, "read", _read_then_a_registration_takes_the_location)

    answered = estate.client.post(finish)

    monkeypatch.setattr(location_claims, "read", real_read)
    held = location_claims.holder(_claims(estate), clone)
    assert (answered.status_code, answered.json().get("code")) == (409, 14), answered.text
    assert (held.uuid if held is not None else None, clone_endings.stands_at(_claims(estate), clone)) == (taken[0], True)
    assert _clone_tags(estate, bronze) == [clone_sources.tag_of(clone)]


def test_a_finish_leaves_the_ending_of_a_clone_its_door_is_still_ending(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """A finish landing while a destructive drop of the clone is still at work (its ending filed and its residue record
    keeping the clone standing at its location) releases nothing and leaves the ending, so when the drop then fails to delete the
    tag, a later finish releases it."""
    bronze, clone = _registered_clone(estate)
    monkeypatch.setattr(location_claims, "LEASE", timedelta(0))
    settings = get_settings()
    real_destroy, real_delete = dataplane.destroy_table, clone_sources.delete_tag
    during: list[str] = []

    def _destroy_with_a_finish_inside(ns: LanceNamespace, *args: Any, **kwargs: Any) -> object:  # noqa: ANN401 — destroy_table's own arguments, passed on
        file_the_ending = kwargs["before_removal"]

        def _file_then_finish() -> None:
            file_the_ending()
            ((digest, filing),) = clone_endings.standing(_claims(estate))
            try:
                clone_endings_door.finish(ns, settings, digest, filing, token=uuid.uuid4().hex)
            except InvalidTableStateError as refused:
                during.append(str(refused))

        kwargs["before_removal"] = _file_then_finish
        return real_destroy(ns, *args, **kwargs)

    monkeypatch.setattr(dataplane, "destroy_table", _destroy_with_a_finish_inside)
    monkeypatch.setattr(clone_sources, "delete_tag", _a_store_that_will_not_delete_a_tag)
    assert _ended_by(estate, "/v1/table/db$silver/drop", {"purge": "true"}) == 500
    monkeypatch.setattr(clone_sources, "delete_tag", real_delete)
    left = (len(during), clone_endings.stands_at(_claims(estate), clone), len(_clone_tags(estate, bronze)))

    finished = _finished_by_maintenance(estate)

    assert left == (1, True, 1), f"the finish inside the drop answered {during}"
    assert (len(finished.finished), _clone_tags(estate, bronze)) == (1, [])


def _a_backend_that_removes_the_row_and_answers_not_found(real_call: Callable[..., object]) -> Callable[..., object]:
    """A backend whose first deregister removes the row and then answers TableNotFound, as when another request removed
    the row between the door's describe and its own deregister."""
    removed: list[bool] = []

    def _call(ns: LanceNamespace, operation: str, *args: object) -> object:
        if operation == "deregister_table" and not removed:
            real_call(ns, operation, *args)
            removed.append(True)
            raise TableNotFoundError("another request removed the row first")
        return real_call(ns, operation, *args)

    return _call


#: What a finish of an ending whose clone still stands answers: refused inside STANDING_BOUND, kept past it.
_KEPT = ((409, 19), {"tags_released": 0, "kept": True})
#: What a finish of an ending whose clone's row is gone answers: the tag released, at once.
_RELEASED = ((200, None), {"tags_released": 1, "kept": False})


@pytest.mark.parametrize(
    ("table", "path", "backend", "answered", "finished"),
    [
        pytest.param("db$silver", "/v1/table/db$silver/deregister", _a_backend_that_will_not_deregister, 500, _KEPT, id="deregister-failed"),
        pytest.param("db$silver", "/v1/table/db$silver/drop", _a_backend_that_will_not_deregister, 500, _KEPT, id="drop-failed"),
        pytest.param("clones$silver", "/v1/namespace/clones/drop", _a_backend_that_will_not_deregister, 500, _KEPT, id="namespace-cascade-failed"),
        pytest.param(
            "db$silver", "/v1/table/db$silver/drop", _a_backend_that_removes_the_row_and_answers_not_found, 404, _RELEASED, id="drop-answered-not-found"
        ),
        pytest.param(
            "clones$silver",
            "/v1/namespace/clones/drop",
            _a_backend_that_removes_the_row_and_answers_not_found,
            200,
            _RELEASED,
            id="namespace-cascade-answered-not-found",
        ),
    ],
)
def test_a_door_whose_native_call_was_attempted_leaves_its_ending_for_the_finish(
    estate: _Estate,
    monkeypatch: pytest.MonkeyPatch,
    table: str,
    path: str,
    backend: Callable[[Callable[..., object]], Callable[..., object]],
    answered: int,
    finished: tuple[tuple[int, int | None], dict[str, object]],
) -> None:
    """A door whose native call failed leaves its ending, whatever the failure, since no read tells it whether the row it
    then finds is the clone's: a call that failed with the row in place, and one answered TableNotFound once the row was
    gone (another request removed it). Where the clone still stands, the finish refuses the ending while the door may
    still be at work, until STANDING_BOUND has passed since the filing, and then removes it with nothing released, so it
    is not asked about for ever; where the row is gone, the finish releases the tag. The warehouse cascade's cases are
    `test_a_warehouse_cascade_revokes_a_grant_only_once_its_object_is_gone.py`'s."""
    assert estate.client.post("/v1/namespace/clones/create", json={}).status_code == 200
    bronze, clone = _registered_clone(estate, table)
    real_call = native.call
    monkeypatch.setattr(native, "call", backend(real_call))
    status = _ended_by(estate, path, {"purge": "true"} if "/drop" in path else {})
    monkeypatch.setattr(native, "call", real_call)
    ((digest, filing),) = clone_endings.standing(_claims(estate))
    monkeypatch.setattr(location_claims, "LEASE", timedelta(0))
    first = estate.client.post(f"/management/v1/clone-endings/{digest}/{filing}/finish")
    monkeypatch.setattr(clone_endings_door, "STANDING_BOUND", timedelta(0))

    last = first if first.status_code == 200 else estate.client.post(f"/management/v1/clone-endings/{digest}/{filing}/finish")

    (refused, done) = finished
    tags = 1 if done["kept"] else 0
    assert (status, (first.status_code, first.json().get("code")), last.json()) == (answered, refused, done), first.text
    assert (clone_endings.stands_at(_claims(estate), clone), len(_clone_tags(estate, bronze))) == (False, tags)


def test_a_finish_keeps_the_base_record_of_bytes_a_later_deregister_left_in_place(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """A destructive drop whose native call failed leaves its ending, meaning to forget the base record with the bytes;
    the clone then deregistered keeps the record for the bytes it leaves in place. The finish forgets a record only where
    no dataset stands at the location any more, so it keeps this one, and ends the ending with the tags gone."""
    bronze, clone = _registered_clone(estate)
    real_call = native.call
    monkeypatch.setattr(native, "call", _a_backend_that_will_not_deregister(real_call))
    assert _ended_by(estate, "/v1/table/db$silver/drop", {"purge": "true"}) == 500
    monkeypatch.setattr(native, "call", real_call)
    assert estate.client.post("/v1/table/db$silver/deregister", json={}).status_code == 200
    monkeypatch.setattr(location_claims, "LEASE", timedelta(0))

    finished = _finished_by_maintenance(estate)

    recorded = base_registry.read_base_record(estate.registry, clone) is not None
    assert (len(finished.finished), recorded, clone_endings.standing(_claims(estate)), _clone_tags(estate, bronze)) == (1, True, [], [])


def _marked_by_another_finish(estate: _Estate, _monkeypatch: pytest.MonkeyPatch) -> None:
    """Filed an hour ago, past a claim's lease, and marked just now by a finish still at work."""
    ((digest, filing),) = clone_endings.standing(_claims(estate))
    found = clone_endings.read(_claims(estate), digest, filing)
    assert found is not None
    filed_earlier = found.ending.model_copy(update={"filed_at": found.at - timedelta(hours=1)})
    key = f"{clone_endings.PREFIX}/{digest}/{filing}.json"
    records.replace_json(estate.root, estate.key, key, filed_earlier.model_dump(mode="json"), etag=found.etag)
    again = clone_endings.read(_claims(estate), digest, filing)
    assert again is not None
    clone_endings.mark(_claims(estate), again, token=uuid.uuid4().hex)


def _changed_under_its_mark(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(location_claims, "LEASE", timedelta(0))
    real_mark = clone_endings.mark

    def _another_write_lands_first(store: location_claims.ClaimStore, read_as: clone_endings.EndingRead, *, token: str) -> None:
        real_mark(store, read_as, token=uuid.uuid4().hex)
        real_mark(store, read_as, token=token)

    monkeypatch.setattr(clone_endings, "mark", _another_write_lands_first)


@pytest.mark.parametrize(
    ("arrange", "code"),
    [
        pytest.param(lambda _estate, _monkeypatch: None, 19, id="filed-within-a-claims-lease"),
        pytest.param(_marked_by_another_finish, 19, id="marked-by-another-finish"),
        pytest.param(_changed_under_its_mark, 14, id="changed-under-its-mark"),
    ],
)
def test_a_finish_that_may_not_act_releases_nothing_and_leaves_the_ending(
    estate: _Estate, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, arrange: Callable[[_Estate, pytest.MonkeyPatch], None], code: int
) -> None:
    """The finish acts on an ending only once a claim's lease has passed since its filing (the door's own fence holds that
    long), never while another finish's mark is younger than a lease, and never on an ending written since its read; each
    refusal is recorded on the audit trail."""
    bronze, clone, finish = _ending_left_by_a_deregister(estate, monkeypatch)
    arrange(estate, monkeypatch)
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)

    answered = estate.client.post(finish)

    audited = [r.__dict__.get("audit.outcome") for r in caplog.records if r.__dict__.get("audit.action") == AuditAction.CLONE_ENDING_FINISH]
    assert (answered.status_code, answered.json().get("code"), audited) == (409, code, [AuditOutcome.FAILURE]), answered.text
    assert (clone_endings.stands_at(_claims(estate), clone), len(_clone_tags(estate, bronze))) == (True, 1)


def _a_tag_the_store_will_not_delete(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    real_delete = clone_sources.delete_tag
    monkeypatch.setattr(clone_sources, "delete_tag", _a_store_that_will_not_delete_a_tag)
    assert estate.client.post("/v1/table/db$silver/deregister", json={}).status_code == 503
    monkeypatch.setattr(clone_sources, "delete_tag", real_delete)


def _a_backend_that_removed_the_row_and_failed(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    real_call = native.call
    monkeypatch.setattr(native, "call", _a_backend_that_removes_the_row_then_fails(real_call))
    with pytest.raises(OSError, match="after removing the row"):
        estate.client.post("/v1/table/db$silver/deregister", json={})
    monkeypatch.setattr(native, "call", real_call)


@pytest.mark.parametrize(
    "ended_by",
    [
        pytest.param(_a_tag_the_store_will_not_delete, id="a-deregister-whose-tag-delete-failed"),
        pytest.param(_a_backend_that_removed_the_row_and_failed, id="a-deregister-whose-native-call-removed-the-row-and-failed"),
    ],
)
def test_a_registration_while_a_clones_ending_stands_is_refused_until_it_is_finished(
    estate: _Estate, monkeypatch: pytest.MonkeyPatch, ended_by: Callable[[_Estate, pytest.MonkeyPatch], None]
) -> None:
    """A registration at a location whose clone's ending stands is refused 409 (code 14) rather than adopting the ending:
    the clone registered there would hold tags of the same names, which the finish would release. The refused
    registration leaves no row and frees only the claim it took. A door whose native call removed the row and failed
    leaves its ending too, never judging whether the row it then describes is the clone's. Once the finish has released
    the old clone's tags, the registration is admitted, and its own tag stands."""
    bronze, clone = _registered_clone(estate)
    ended_by(estate, monkeypatch)
    monkeypatch.setattr(location_claims, "LEASE", timedelta(0))

    refused = estate.client.post("/v1/table/db$silver/register", json={"location": "silver"})
    left = (estate.client.post("/v1/table/db$silver/describe", json={}).status_code, location_claims.holder(_claims(estate), clone))
    _finished_by_maintenance(estate)
    admitted = estate.client.post("/v1/table/db$silver/register", json={"location": "silver"})

    assert (refused.status_code, refused.json().get("code"), left) == (409, 14, (404, None)), refused.text
    assert admitted.status_code == 200, admitted.text
    assert _clone_tags(estate, bronze) == [clone_sources.tag_of(clone)], "the registration after the finish holds no tag on its source"


def test_a_trashed_clone_whose_end_cannot_delete_its_tag_keeps_its_location(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """The end of a clone in the trash forgets its base record, and with it its tag on the source, before it frees the
    clone's location: a tag the store will not delete leaves the location held, as at the deregister door."""
    _, clone = _registered_clone(estate)
    assert estate.client.post("/v1/table/db$silver/drop", json={}).status_code == 200
    record = trash.named(estate.root, estate.key, "db$silver", kind="table")[0]
    monkeypatch.setattr(clone_sources, "delete_tag", _a_store_that_will_not_delete_a_tag)

    with pytest.raises(OSError, match="did not answer the delete"):
        trash.end(estate.root, estate.key, record, kind="table", uuid=str(trash.object_uuid(record)))

    held = location_claims.holder(location_claims.ClaimStore(control_root=estate.root, storage_options=estate.key), clone)
    assert held is not None and held.table == "db$silver", "the clone's location was freed with its tag still standing"


def test_a_late_end_of_a_former_clone_keeps_the_tag_of_the_clone_there_now(estate: _Estate) -> None:
    """A clone's tag is named for its location, so a clone registered there since holds a tag of the same name: an end of
    the table that held the location before, landing late, leaves that tag while the location's claim names the clone."""
    bronze, clone = _registered_clone(estate)

    released = base_registry.release_clone_sources(estate.registry, clone, holder="db$gone")

    assert (released, clone_sources.tag_of(clone) in estate.open(bronze).tags.list()) == ([], True)


def test_a_late_end_of_a_former_clone_keeps_the_record_of_the_clone_there_now(estate: _Estate) -> None:
    """The base record is keyed by the location as the tag is named for it: a late end of the table that held the
    location before leaves the record of the clone the claim names now, which is what sanctions that clone's source and
    names the tags its own end deletes."""
    _, clone = _registered_clone(estate)

    forgotten = base_registry.forget_base_record(estate.registry, clone, table="db$gone")

    assert (forgotten, base_registry.read_base_record(estate.registry, clone) is not None) == (False, True)


def test_a_register_undone_with_a_tag_the_store_keeps_leaves_the_clones_location_held(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """A registration whose ownership seed fails is undone tags first, as every other end of a clone is: a tag the store
    will not delete leaves the location held, so no clone registered there next takes it while that tag stands."""
    from catalog.api import fga_deps

    bronze = estate.create("db$bronze", fragments=3)
    clone = f"{estate.root}/silver"
    source = estate.open(bronze)
    source.shallow_clone(clone, source.version, storage_options=estate.key.in_clear())

    async def _no_store(*_args: object, **_kwargs: object) -> None:
        raise OSError("the authorization store did not take the grant")

    monkeypatch.setattr(fga_deps, "seed_ownership", _no_store)
    monkeypatch.setattr(clone_sources, "delete_tag", _a_store_that_will_not_delete_a_tag)

    with pytest.raises(OSError, match="did not take the grant"):
        estate.client.post("/v1/table/db$silver/register", json={"location": "silver"})

    held = location_claims.holder(location_claims.ClaimStore(control_root=estate.root, storage_options=estate.key), clone)
    assert held is not None and held.table == "db$silver", "the undone registration freed the location with its tag still standing"


def test_a_location_whose_holder_is_gone_is_registered_under_another_name(estate: _Estate, monkeypatch: pytest.MonkeyPatch) -> None:
    """A claim whose table no longer resolves to the location (an end that released nothing) is taken over once its
    lease lapses, by whichever name registers there next: the location is not kept for the name that left it."""
    uri = f"{estate.root}/left"
    lance.write_dataset(_MINE, uri, storage_options=estate.key.in_clear(), data_storage_version="2.2", enable_stable_row_ids=True)
    claims = location_claims.ClaimStore(control_root=estate.root, storage_options=estate.key)
    location_claims.take(claims, uri, "db$gone", ["db", "gone"], is_gone=lambda _claim: True, uuid=str(uuid.uuid4()))
    monkeypatch.setattr(location_claims, "LEASE", timedelta(0))

    registered = estate.client.post("/v1/table/db$found/register", json={"location": "left"})

    held = location_claims.holder(claims, uri)
    assert (registered.status_code, held.table if held is not None else None) == (200, "db$found"), registered.text


def _registered(estate: _Estate, table: str, location: str) -> str:
    """A dataset written at ``location`` with its own directory beneath the approved data base, registered as ``table``, and the record that earned."""
    uri = f"{estate.root}/{location}"
    lance.write_dataset(
        _MINE,
        uri,
        initial_bases=[lance.DatasetBasePath(f"{estate.root}/second/data/{location}", name="second", is_dataset_root=False)],
        storage_options=estate.key.in_clear(),
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    assert estate.client.post(f"/v1/table/{table}/register", json={"location": location}).status_code == 200
    record = base_registry.read_base_record(estate.registry, uri)
    assert record is not None and [entry.role for entry in record.entries] == [base_registry.BaseRole.DATA], f"precondition: {record}"
    return uri


def _purge_expired(estate: _Estate, table: str, location: str) -> None:
    """The maintenance purge of the trash record ``table``'s recoverable drop filed, re-stamped long expired, under a clean drift report."""
    from lakehouse import trash
    from maintenance.services.reconcile import CATEGORIES, ReconcileReport

    (filed,) = trash.named(estate.root, estate.key, table)
    record = trash.make_record(
        table, uuid=str(filed["uuid"]), location=location, dropped_by="user:alice", grace_days=1, now=datetime.now(UTC) - timedelta(days=30)
    )
    # Released, as the catalog's release door leaves a record the purge may act on.
    trash.put(estate.root, estate.key, {**record, trash.RELEASED_AT: (datetime.now(UTC) - timedelta(days=1)).isoformat()})
    report = ReconcileReport(
        checked_at=datetime.now(UTC).isoformat(),
        counts=dict.fromkeys(CATEGORIES, 0),
        total=0,
    )
    settings = estate.maintenance(trash_purge_enabled=True, catalog_url=CATALOG_URL)
    with catalog_doors(estate.client):
        out = asyncio.run(purge.purge_expired_trash(settings, report=report, control_root=estate.root, data_roots={estate.root}))
    assert [record.id for record in out.purged] == [table], f"precondition: the purge reclaimed {table}: {out}"


def test_a_table_location_is_exclusive(estate: _Estate) -> None:
    """The register door's location-exclusivity: the only thing keeping ``_bases/`` out of every vend.

    The ``dir`` backend accepts a register at the namespace root, at a control prefix and at another table's
    directory (lh279 m4), and a write vend covers the registered prefix (lh279 s1). It also accepts a spelling
    that opens another table's directory: pylance 12.0.0 drops the tab from ``x/.\\t./<victim>`` (and a line
    feed or carriage return likewise) and resolves the ``..`` left behind (lh279 r4 register_probe). Each
    segment is judged as it reads after one percent-decode, so ``%2e%2e`` is refused as ``..`` and ``%5Fbases``
    as ``_bases``.

    A ``?``, or its encoded ``%3F``, names no location either, by the spelling rule every judge asks: the store keeps it
    under a re-encoded name that holds no table, so an id registered there is not found by query or count (measured on
    pylance 13.0.0, dir backend). A ``*`` or a ``${``, raw or encoded (``%2A``, ``%24%7B``), is refused because a location
    is the prefix every credential for its table is scoped by, where IAM reads ``*`` as a wildcard ([[LH-437]]) and ``${``
    as the start of a policy variable ([[LH-526]]).

    Nor may a location equal, contain or sit under another registered table's ([[LH-504]]): a destructive drop
    of the outer table would delete the inner one's bytes. Only the namespace's object index answers it, so the
    door asks once the backend has recorded the registration and detaches the later of the two. ``outer/inner``
    is held by ``db$inner``'s claim, which refuses in the same words: the answer says the location is taken and
    never which table holds it, since the caller may hold no grant on that table ([[LH-436]]).

    Nor may a location be a uuid directly under the namespace root ([[LH-520]]): that is where the catalog lays out the
    tables it makes, named after each table's uuid (ADR 0173 §5), so a registrar may not take one, even one no table
    holds.

    A table registered at a location the catalog answers percent-encoded (``räksmörgås`` ->
    ``r%C3%A4ksm%C3%B6rg%C3%A5s``, ``growth%`` -> ``growth%25``) is still its own: it is registered with a
    readable record, and a branch of it, whose manifest declares the root spelled so, is vended.

    And a location belongs to the table registered there only while that table exists: a table DESTROYED
    — by the catalog's destructive drop, its namespace's destructive cascade or the maintenance purge of
    its trash record — takes its base record with its bytes, or the next table registered at the location
    would inherit bases nobody judged for it. A warehouse delete ends its trashed tables' base records too,
    pinned at its door by `test_a_warehouse_cascade_revokes_a_grant_only_once_its_object_is_gone.py`.
    """
    victim = estate.victim.removeprefix(f"{estate.root}/")
    spellings = (".", "_bases", "%5Fbases", f"x/.\t./{victim}", f"x/.\n./{victim}", f"x/%2e%2e/{victim}", f"{victim}?x", f"{victim}%3Fx")
    for location in spellings:
        resp = estate.client.post("/v1/table/db$squatter/register", json={"location": location})
        _refused(resp, _INVALID_INPUT)
        assert estate.client.post("/v1/table/db$squatter/exists").status_code == 404, f"{location!r}: a refused registration must be detached"
        assert estate.client.post("/v1/table/db$squatter/query", json=_QUERY).status_code == 404, f"{location!r}: the victim reads through the refused id"
    for location, held in ((f"{victim}*x", ["*"]), (f"{victim}%2Ax", ["*"]), (f"{victim}${{x}}", ["${"]), (f"{victim}%24%7Bx%7D", ["${"])):
        resp = estate.client.post("/v1/table/db$squatter/register", json={"location": location})
        _refused(resp, _INVALID_INPUT)
        detail = (
            f"register location {location!r} holds IAM policy syntax {held}: every credential vended for the table names its location, "
            "where IAM reads '*' and '?' as wildcards that widen the credential to sibling objects and '${' as a policy variable"
        )
        assert resp.json()["detail"] == detail, resp.text
        assert estate.client.post("/v1/table/db$squatter/exists").status_code == 404, f"{location!r}: a refused registration must be detached"
        assert estate.client.post("/v1/table/db$squatter/query", json=_QUERY).status_code == 404, f"{location!r}: the victim reads through the refused id"
    lance.write_dataset(_MINE, f"{estate.root}/outer/inner", storage_options=estate.key.in_clear(), data_storage_version="2.2", enable_stable_row_ids=True)
    assert estate.client.post("/v1/table/db$inner/register", json={"location": "outer/inner"}).status_code == 200, (
        "the control: a location of its own registers"
    )
    for location in ("outer", "outer/inner", "outer/inner/tree"):
        resp = estate.client.post("/v1/table/db$squatter/register", json={"location": location})
        _refused(resp, _INVALID_INPUT)
        assert resp.json()["detail"] == f"register location {location!r} equals, contains or sits under another table's location", resp.text
        assert estate.client.post("/v1/table/db$squatter/exists").status_code == 404, f"{location!r}: a refused registration must be detached"
        assert estate.client.post("/v1/table/db$squatter/query", json=_QUERY).status_code == 404, f"{location!r}: the victim reads through the refused id"
    planted = str(uuid.uuid4())
    lance.write_dataset(_MINE, f"{estate.root}/{planted}", storage_options=estate.key.in_clear(), data_storage_version="2.2", enable_stable_row_ids=True)
    resp = estate.client.post("/v1/table/db$squatter/register", json={"location": planted})
    _refused(resp, _INVALID_INPUT)
    assert resp.json()["detail"] == (
        f"register location {planted!r} is in a uuid directory directly under the namespace root, where the catalog lays out the tables it makes"
    )
    assert estate.client.post("/v1/table/db$squatter/exists").status_code == 404, "a refused registration must attach nothing"
    for name in ("räksmörgås", "growth%"):
        named = f"db${name}"
        lance.write_dataset(
            _ROWS, f"{estate.root}/spelled/{quote(name)}", storage_options=estate.key.in_clear(), data_storage_version="2.2", enable_stable_row_ids=True
        )
        assert estate.client.post(f"/v1/table/{quote(named, safe='$')}/register", json={"location": f"spelled/{name}"}).status_code == 200
        location = str(estate.client.post(f"/v1/table/{quote(named, safe='$')}/describe", json={}).json()["location"])
        estate.open(location).create_branch("work")
        vend = estate.client.post(f"/management/v1/table/{quote(named, safe='$')}/credentials", params={"branch": "work"})
        assert vend.status_code == 200, f"{named!r} at {location!r}: {vend.status_code} {vend.text}"
        assert base_registry.read_base_record(estate.registry, location) is not None, f"{named!r}: the register door wrote no record for {location!r}"

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
