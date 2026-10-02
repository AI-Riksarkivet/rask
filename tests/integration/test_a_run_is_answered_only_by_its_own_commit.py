"""[[LH-280]] A run is answered only by the commit that carried its own data, whoever else names its run id.

A RUN ID IS THE CALLER'S WORD. Ingest mints it from a project and an idempotency key, ``GET /v1/ingests``
lists it to every project member, and any writer of the table can put it on a commit through ``/commit``
as itself. Measured on pylance 12.0.0 through this fixture before the fix: the run's own commit was
answered with the claimer's version, so its rows never landed and its finalize reported another writer's
version as its own. A version's transaction record is likewise its committer's word: the catalog may
recognize a run's commit only in what the version's manifest holds.

ONE ESTATE, ON MOTO: the catalog app with OIDC on and its service door open to two subjects, ``ingest``,
whose run it is, and ``mallory``, another writer of the same table. FGA is off: the claim is who wrote,
not who may. The run speaks through ingest's own catalog client, making the calls ``finalize_run`` and its
prior-commit probe make, so what the client is answered is what the run reports.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import boto3
import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient

from ingest.catalog_service import CatalogError, CatalogServiceClient
from service_kit.lakehouse.objectfs import lance_storage_options
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


_SHARED = "the-shared-app-token"
_RUN = "run-8d1f"
_SCHEMA = pa.schema([("id", pa.int64())])
_SEED, _DOOR_CLAIM, _RUNS_OWN = [0], [666], [1, 2, 3]


def _as(subject: str) -> dict[str, str]:
    """The service door's two headers: the Dapr app token and the subject it vouches for."""
    return {"dapr-api-token": _SHARED, "x-lance-service-identity": subject}


def _rows(ids: list[int]) -> pa.Table:
    return pa.table({"id": pa.array(ids, pa.int64())}, schema=_SCHEMA)


class _Table:
    """``db$pages``, created through the door as ``ingest``, and the estate key a write vend stands in for."""

    def __init__(self, url: str, client: TestClient) -> None:
        self.client = client
        self.key = lance_storage_options(url, "test", "test", "us-east-1")
        assert client.post("/v1/namespace/db/create", json={}, headers=_as("ingest")).status_code == 200
        created = client.post(
            "/v1/table/db$pages/create", content=encode_arrow_stream(_rows(_SEED)), headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE, **_as("ingest")}
        )
        assert created.status_code == 200, created.text
        self.location = str(created.json()["location"])
        #: The version the run read when it started, before anyone named its id.
        self.read_version = self.open().version
        #: The run's own fragments, staged and not yet committed.
        self.runs_own = self.staged(_RUNS_OWN)

    def open(self, version: int | None = None) -> lance.LanceDataset:
        return lance.dataset(self.location, version=version, storage_options=self.key)

    def staged(self, ids: list[int]) -> list[lance.FragmentMetadata]:
        """Fragments written straight under the table's ``data/``, as a vended writer writes them."""
        return lance.fragment.write_fragments(_rows(ids), self.location, storage_options=self.key, data_storage_version="2.2", enable_stable_row_ids=True)

    def ids(self, version: int) -> list[int]:
        return sorted(self.open(version).to_table().column("id").to_pylist())


@pytest.fixture
def table(moto_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Table]:
    bucket = f"lh280-{uuid.uuid4().hex[:10]}"
    boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1").create_bucket(Bucket=bucket)
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{bucket}",
        "LANCE_CONTROL_ROOT": f"s3://{bucket}",
        "LANCE_S3_ENDPOINT": moto_url,
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "RASK_OIDC_ENABLED": "true",
        "RASK_OIDC_ISSUER": "https://idp.invalid",
        "RASK_OIDC_AUDIENCE": "rask",
        "LANCE_SERVICE_SUBJECTS": "ingest,mallory",
        "APP_API_TOKEN": _SHARED,
        # Ingest's catalog client: the door it calls and the identity it presents there.
        "RASK_CATALOG_URL": "http://testserver",
        "RASK_CATALOG_APP_TOKEN": _SHARED,
        "RASK_CATALOG_SERVICE_IDENTITY": "ingest",
    }.items():
        monkeypatch.setenv(key, value)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app) as client:
        # The TestClient IS an httpx.Client, so ingest's pooled client reaches this app in-process.
        monkeypatch.setattr("ingest.http.shared_client", lambda: client)
        yield _Table(moto_url, client)
    get_settings.cache_clear()


class _RecordListsTheRun:
    """The real dataset, except that one version's transaction record lists the run's fragments.

    Stands in for a version whose record and manifest disagree: pylance writes only consistent pairs, and
    the record is the committer's word, so recognition must not take it alone.
    """

    def __init__(self, inner: lance.LanceDataset, version: int, fragments: list[lance.FragmentMetadata]) -> None:
        self._inner, self._version, self._fragments = inner, version, fragments

    def read_transaction(self, version: int) -> lance.Transaction | None:
        recorded = self._inner.read_transaction(version)
        if version != self._version or recorded is None:
            return recorded
        return lance.Transaction(read_version=recorded.read_version, operation=lance.LanceOperation.Append(self._fragments), uuid=recorded.uuid)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


@pytest.fixture
def claimed(table: _Table, monkeypatch: pytest.MonkeyPatch) -> _Table:
    """Mallory claims the run before it commits: through the door under its run id, at a version whose record lists the run's fragments."""
    door = table.client.post(
        "/management/v1/table/db$pages/commit",
        json={"fragments": [f.to_json() for f in table.staged(_DOOR_CLAIM)], "read_version": table.read_version, "run_id": _RUN},
        headers=_as("mallory"),
    )
    assert door.status_code == 200, door.text
    claim_version = table.open().version
    real = lance.dataset
    monkeypatch.setattr(lance, "dataset", lambda *a, **kw: _RecordListsTheRun(real(*a, **kw), claim_version, table.runs_own))
    assert table.ids(claim_version) == sorted(_SEED + _DOOR_CLAIM), "precondition: the claim landed, or nothing below is tested"
    return table


def _ingest() -> CatalogServiceClient:
    return CatalogServiceClient(_SCHEMA, base_url="http://testserver")


def test_a_run_id_another_writer_claimed_does_not_answer_the_runs_commit(claimed: _Table) -> None:
    """Closes-when, the door: with the claim in place the run's commit lands its rows exactly once, at the version it is answered."""
    fragments = [json.dumps(fragment.to_json()) for fragment in claimed.runs_own]

    version, _ = _ingest().commit("db", "pages", fragments, claimed.read_version, _RUN)

    assert claimed.ids(version) == sorted(_SEED + _DOOR_CLAIM + _RUNS_OWN)


def test_a_run_that_committed_nothing_is_not_answered_by_a_claim(claimed: _Table) -> None:
    """Closes-when, ingest: the prior-commit probe of a run with nothing left to commit names no version.

    ``_prior_commit`` asks exactly this, an empty commit carrying the run id, and reads a refusal as "no
    prior commit", so ``finalize_run`` reports "nothing to commit" instead of another writer's rows as the
    run's own.
    """
    with pytest.raises(CatalogError, match=r"refused the commit \(400\)"):
        _ingest().commit("db", "pages", [], 0, _RUN)
