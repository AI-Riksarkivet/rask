"""[[LH-209]] An external blob base is registered per table and only on request, and no vend grants one.

THE ESTATE is the real catalog app on a moto bucket whose ``models/`` prefix holds a victim model's
weights, with ``LANCE_EXTERNAL_BLOB_BASES`` naming that prefix (the chart's default) and a separate
corpus bucket, and the lifespan's own STS vendor, whose ``AssumeRole`` call is captured so the session
policy it would send is what the test reads. Measured before the fix: every create registered the
``models/`` base, so a plain create's read vend granted ``ListBucket`` and ``GetObject`` on
``<bucket>/models/*``, and a create whose rows named the victim's weights by ``Blob.from_uri`` was
accepted.

A base is requested the way ingest's catalog client requests it: the create door's ``external_blob_base``
parameter. The commit-side half (a fragment pointing at the victim through a base the writer planted) is
a case in ``test_a_forged_fragment_is_refused_and_the_table_is_unchanged.py``, beside every other forged
fragment.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import boto3
import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from lance.blob import Blob, blob_array

from medallion.services.catalog_register import ensure_stage_output
from medallion.services.compute import read_upstream
from service_kit.lakehouse import blobs
from service_kit.lakehouse.objectfs import lance_storage_options
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream
from storage import S3Client


_INVALID_INPUT = 13
_INVALID_TABLE_STATE = 19


class _Estate:
    def __init__(self, url: str, client: TestClient, bucket: str, corpus: str) -> None:
        self.client = client
        self.bucket = bucket
        self.corpus = corpus
        #: The estate key a writer's write vend stands in for.
        self.key = lance_storage_options(url, "test", "test", "us-east-1")
        self.s3: S3Client = boto3.client("s3", endpoint_url=url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
        self.victim = f"s3://{bucket}/models/victim/tok/weights.bin"
        self.policies: list[dict[str, Any]] = []
        assert client.post("/v1/namespace/db/create", json={}).status_code == 200

    def create(self, table: str, rows: pa.Table, external_blob_base: str | None) -> Any:  # noqa: ANN401 — the httpx response
        return self.client.post(
            f"/v1/table/{quote(table, safe='$')}/create",
            content=encode_arrow_stream(rows),
            params={"external_blob_base": external_blob_base} if external_blob_base else None,
            headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE},
        )

    def capture(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN401 — boto3's AssumeRole keyword arguments
        self.policies.append(json.loads(str(kwargs["Policy"])))
        return {"Credentials": {"AccessKeyId": "AK", "SecretAccessKey": "SK", "SessionToken": "ST"}}


@pytest.fixture
def estate(moto_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Estate]:
    bucket, corpus = f"lh209-{uuid.uuid4().hex[:10]}", f"lh209c-{uuid.uuid4().hex[:10]}"
    s3 = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    for name in (bucket, corpus):
        s3.create_bucket(Bucket=name)
    s3.put_object(Bucket=bucket, Key="models/victim/tok/weights.bin", Body=b"VICTIM-WEIGHTS")
    for key, value in {
        "LANCE_REST_IMPL": "dir",
        "LANCE_REST_ROOT": f"s3://{bucket}",
        "LANCE_CONTROL_ROOT": f"s3://{bucket}",
        "LANCE_S3_ENDPOINT": moto_url,
        "LANCE_S3_ACCESS_KEY_ID": "test",
        "LANCE_S3_SECRET_ACCESS_KEY": "test",
        "LANCE_CONTROL_EMIT_ENABLED": "false",
        "LANCE_VENDING_MODE": "sts",
        "LANCE_S3_STS_ENDPOINT": moto_url,
        "LANCE_S3_ASSUME_ROLE_ARN": "arn:aws:iam::000000000000:role/lance-vend",
        "LANCE_EXTERNAL_BLOB_BASES": f"s3://{bucket}/models/,s3://{corpus}/media/",
    }.items():
        monkeypatch.setenv(key, value)
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    with TestClient(app) as client:
        made = _Estate(moto_url, client, bucket, corpus)
        app.state.vendor._assume_role = made.capture  # noqa: SLF001 — the injection point the lifespan's vendor does not expose
        yield made
    get_settings.cache_clear()


@pytest.mark.parametrize(
    "requested",
    [
        pytest.param(None, id="a-plain-create"),
        # Its base is a pointer base the blob door serves: sanctioned, never granted, and never a reason to withhold the direct credential.
        pytest.param("s3://{corpus}/media/pages/", id="a-create-naming-an-approved-external-base"),
    ],
)
def test_a_create_is_vended_nothing_outside_its_table_prefix(estate: _Estate, requested: str | None) -> None:
    rows = pa.table({"id": pa.array([1], pa.int64()), "payload": blob_array([b"inline-bytes"])})
    created = estate.create("db$plain", rows, requested.format(corpus=estate.corpus) if requested else None)
    assert created.status_code == 200, created.text
    prefix = str(created.json()["location"]).removeprefix(f"s3://{estate.bucket}/").rstrip("/")

    vended = estate.client.post("/management/v1/table/db$plain/credentials", params={"tier": "read"})

    assert vended.status_code == 200, vended.text
    assert vended.json()["mode"] == "direct", vended.text
    [policy] = estate.policies
    granted = [(statement["Resource"], statement.get("Condition", {}).get("StringLike", {}).get("s3:prefix")) for statement in policy["Statement"]]
    assert granted == [(f"arn:aws:s3:::{estate.bucket}", [f"{prefix}/*"]), (f"arn:aws:s3:::{estate.bucket}/{prefix}/*", None)]


@pytest.mark.parametrize(
    ("requested", "refusal"),
    [
        pytest.param(None, "outside the dataset root and any registered external base", id="a-create-naming-no-base"),
        pytest.param("s3://{bucket}/models/victim/", "overlaps storage the catalog governs", id="a-create-naming-the-model-tree"),
    ],
)
def test_a_create_pointing_at_another_tenants_object_is_refused(estate: _Estate, requested: str | None, refusal: str) -> None:
    rows = pa.table({"id": pa.array([1], pa.int64()), "payload": blob_array([Blob.from_uri(estate.victim)])})

    created = estate.create("db$thief", rows, requested.format(bucket=estate.bucket) if requested else None)

    assert (created.status_code, created.json().get("code")) == (400, _INVALID_INPUT), created.text
    assert refusal in created.json()["detail"], created.text
    assert estate.client.post("/v1/table/db$thief/describe", json={}).status_code == 404


def test_a_stage_output_takes_its_upstreams_carried_pointers(estate: _Estate, tmp_path: Path) -> None:
    """The cascade forwards an external upstream's pointers into the tier it creates through the catalog: the stage asks
    the create for the upstream's base, and a carried pointer then merges into the new tier and reads back."""
    base = f"s3://{estate.corpus}/media/pages/"
    estate.s3.put_object(Bucket=estate.corpus, Key="media/pages/p-1.bin", Body=b"PAGE-ONE")
    rows = pa.table({"id": pa.array([1], pa.int64()), "payload": blob_array([Blob.from_uri(f"{base}p-1.bin")])})
    bronze = estate.create("db$bronze", rows.schema.empty_table(), base)
    assert bronze.status_code == 200, bronze.text
    bronze_uri = str(bronze.json()["location"])
    lance.write_dataset(rows, bronze_uri, mode="append", storage_options=estate.key)
    upstream = read_upstream(bronze_uri, estate.key)
    token = tmp_path / "token"
    token.write_text("t")

    silver_uri = ensure_stage_output(
        catalog_url="http://testserver",
        table_id="db$silver",
        schema=upstream.schema,
        external_blob_base=upstream.external_base,
        identity_token_file=str(token),
        client=estate.client,
    )

    [descriptor] = lance.dataset(bronze_uri, storage_options=estate.key).to_table(columns=["payload"]).column("payload").to_pylist()
    carried = pa.table({"id": pa.array([1], pa.int64()), "payload": blob_array([blobs.carry_external_descriptor(descriptor, str(upstream.external_base))])})
    lance.dataset(silver_uri, storage_options=estate.key).merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(carried)
    assert [payload for _, payload in lance.dataset(silver_uri, storage_options=estate.key).read_blobs("payload", indices=[0])] == [b"PAGE-ONE"]


def test_a_base_over_the_model_tree_is_sanctioned_by_nothing_but_a_record(estate: _Estate) -> None:
    """A write-vend holder can write any manifest: a base inside the configured ``models/`` entry, planted with
    ``add_bases`` and pointed through, is refused at the blob door, and a dataset declaring one is refused at register."""
    created = estate.create("db$plant", pa.table({"id": pa.array([1], pa.int64()), "payload": blob_array([b"own"])}), None)
    assert created.status_code == 200, created.text
    location = str(created.json()["location"])
    planted = lance.DatasetBasePath(f"s3://{estate.bucket}/models/victim/", name="planted", is_dataset_root=False)
    lance.dataset(location, storage_options=estate.key).add_bases([planted])
    stolen = pa.table({"id": pa.array([2], pa.int64()), "payload": blob_array([Blob.from_uri(estate.victim)])})
    lance.write_dataset(stolen, location, mode="append", storage_options=estate.key)
    lance.write_dataset(
        stolen, f"s3://{estate.bucket}/registered", initial_bases=[planted], storage_options=estate.key, data_storage_version="2.2", enable_stable_row_ids=True
    )

    served = estate.client.get("/management/v1/table/db$plant/blobs", params={"column": "payload", "row": 1})
    registered = estate.client.post("/v1/table/db$registered/register", json={"location": "registered"})

    assert (served.status_code, registered.status_code) == (409, 400), (served.content[:200], registered.text)
    assert (served.json()["code"], registered.json()["code"]) == (_INVALID_TABLE_STATE, _INVALID_INPUT)
