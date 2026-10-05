"""[[LH-202]] A vended credential reaches only the files its rung may change, and a writer still lands rows.

THE LAYOUT THE FORMAT FIXES DECIDES THE GRANT. `lance_docs/file_format.md` places a table's files in
named directories: `data/` (data files, and blob sidecars under `data/<stem>/`), `_versions/` (the
manifests, so every commit), `_transactions/`, `_deletions/`, `_indices/`, `_refs/` (branch and tag
pointers), `tree/<branch>/` (each branch's own directories) and `_mem_wal/`. A `can_write_data` holder
appends: its client writes data files and the catalog's `/commit` folds them into a version under the
catalog's own credential. So the writer tier is read on the table plus `PutObject` on `data/` (and on
rask's ingest ledger), and nothing that commits, deletes or moves a ref. Whole-prefix write and delete
belong to `can_maintain`, whose compaction rewrites files and whose cleanup deletes versions.

MEASURED ON PYLANCE 12.0.0 before choosing the allow-list (`LANCE_LOG=lance::events::file_audit` and a
listing before and after): `write_fragments` creates `data/<file>.lance` and, for a blob v2 column,
`data/<file>/<n>.blob`; on a branch handle, the same under `tree/<branch>/data/`. Nothing else. The
second test keeps that measurement live: every object the writer's `write_fragments` creates must be one
the policy it was vended allows.

DRIVEN THROUGH THE REAL DOOR AND THE REAL VENDOR. The catalog runs on a moto server, the vendor is the
estate's `StsVendor`, and its AssumeRole goes to moto's STS; the only thing added is a recorder of the
session policy that call carried. moto's S3 does not enforce session policies, so the policy is judged
with moto's IAM evaluator (`IAMPolicy`), which applies IAM's own wildcard semantics. The store's own
403s are the deployed probe's job.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import boto3
import lance
import pyarrow as pa
import pytest
from fastapi.testclient import TestClient
from moto.iam.access_control import IAMPolicy, PermissionResult

from catalog.core.vending import StsVendor
from service_kit.lancekit.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


_SCHEMA = pa.schema([("id", pa.int64()), lance.blob_field("payload")])
_TABLE = "db$pages"


def _rows(ids: list[int], blob_bytes: int = 16) -> pa.Table:
    return pa.table({"id": pa.array(ids, pa.int64()), "payload": lance.blob_array([bytes(blob_bytes) for _ in ids])}, schema=_SCHEMA)


class _Estate:
    """The catalog on moto, one table and a branch of it created through the door, and every session policy vended."""

    def __init__(self, client: TestClient, s3: Any, bucket: str, policies: list[str]) -> None:
        self.client, self.s3, self.bucket, self.policies = client, s3, bucket, policies
        assert client.post("/v1/namespace/db/create", json={}).status_code == 200
        self.location = self._create(_TABLE)
        created = client.post(f"/v1/table/{_TABLE}/branches/create", json={"name": "b1"})
        assert created.status_code == 200, created.text

    def _create(self, table: str) -> str:
        created = self.client.post(f"/v1/table/{table}/create", content=encode_arrow_stream(_rows([0])), headers={"Content-Type": ARROW_STREAM_MEDIA_TYPE})
        assert created.status_code == 200, created.text
        return str(created.json()["location"])

    def vend(self, tier: str, branch: str = "") -> tuple[dict[str, str], dict[str, Any]]:
        """The storage options the door vends, and the session policy the vendor sent to STS for them."""
        params = {"tier": tier, **({"branch": branch} if branch else {})}
        vended = self.client.post(f"/management/v1/table/{_TABLE}/credentials", params=params)
        assert vended.status_code == 200, vended.text
        assert vended.json()["mode"] == "direct", vended.text
        return vended.json()["credentials"]["storage_options"], json.loads(self.policies[-1])

    def keys(self) -> set[str]:
        return {o["Key"] for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket) for o in page.get("Contents", [])}


@pytest.fixture
def estate(moto_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Estate]:
    bucket = f"lh202-{uuid.uuid4().hex[:10]}"
    s3 = boto3.client("s3", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    s3.create_bucket(Bucket=bucket)
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
    from catalog.api.dependencies import get_vendor
    from catalog.core.config import get_settings

    get_settings.cache_clear()
    from catalog.main import app

    sts = boto3.client("sts", endpoint_url=moto_url, aws_access_key_id="test", aws_secret_access_key="test", region_name="us-east-1")
    policies: list[str] = []

    def _assume_role(**kwargs: Any) -> dict[str, object]:
        policies.append(str(kwargs["Policy"]))
        return sts.assume_role(**kwargs)

    vendor = StsVendor(role_arn="arn:aws:iam::123456789012:role/lance-vend", region="us-east-1", endpoint=moto_url, assume_role=_assume_role)
    app.dependency_overrides[get_vendor] = lambda: vendor
    with TestClient(app) as client:
        yield _Estate(client, s3, bucket, policies)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _permits(policy: dict[str, Any], action: str, bucket: str, key: str) -> bool:
    """IAM's verdict on ``action`` over ``bucket/key``, by moto's evaluator.

    moto turns a Resource into a regex without escaping it, so the ``$`` a table directory carries would
    read as an end anchor; it is swapped for a character IAM gives no meaning, on both sides alike.
    """
    document = json.dumps(policy).replace("$", "~")
    return IAMPolicy(document).is_action_permitted(action, f"arn:aws:s3:::{bucket}/{key.replace('$', '~')}") == PermissionResult.PERMITTED


def _within(location: str, bucket: str) -> str:
    return location.removeprefix(f"s3://{bucket}/").rstrip("/")


_Tier = Literal["read", "write", "maintain"]


@pytest.mark.parametrize(
    ("tier", "branch", "action", "relative", "allowed"),
    [
        pytest.param("read", "", "s3:GetObject", "_versions/1.manifest", True, id="read-reads"),
        pytest.param("read", "", "s3:PutObject", "data/f.lance", False, id="read-cannot-write"),
        pytest.param("write", "", "s3:GetObject", "_versions/1.manifest", True, id="write-reads-the-table"),
        pytest.param("write", "", "s3:PutObject", "data/f.lance", True, id="write-data-file"),
        pytest.param("write", "", "s3:PutObject", "data/f/1.blob", True, id="write-blob-sidecar"),
        pytest.param("write", "", "s3:AbortMultipartUpload", "data/f.lance", True, id="write-aborts-its-upload"),
        pytest.param("write", "", "s3:PutObject", "_ingest_staging/run/m.json", True, id="write-ingest-ledger"),
        pytest.param("write", "", "s3:DeleteObject", "_ingest_staging/run/m.json", True, id="write-purges-ingest-ledger"),
        pytest.param("write", "", "s3:DeleteObject", "data/f.lance", False, id="write-cannot-delete-data"),
        pytest.param("write", "", "s3:PutObject", "_versions/9.manifest", False, id="write-cannot-commit"),
        pytest.param("write", "", "s3:DeleteObject", "_versions/1.manifest", False, id="write-cannot-delete-a-version"),
        pytest.param("write", "", "s3:PutObject", "_transactions/9-x.txn", False, id="write-cannot-write-a-transaction"),
        pytest.param("write", "", "s3:PutObject", "_deletions/0-1-1.arrow", False, id="write-cannot-write-a-deletion"),
        pytest.param("write", "", "s3:PutObject", "_indices/u/index.idx", False, id="write-cannot-write-an-index"),
        pytest.param("write", "", "s3:PutObject", "_refs/tags/t.json", False, id="write-cannot-move-a-tag"),
        pytest.param("write", "", "s3:PutObject", "_refs/branches/b2.json", False, id="write-cannot-forge-a-branch"),
        pytest.param("write", "", "s3:PutObject", "tree/b1/data/f.lance", False, id="write-main-cannot-reach-a-branch"),
        pytest.param("write", "", "s3:PutObject", "_mem_wal/0/x", False, id="write-cannot-write-the-mem-wal"),
        pytest.param("write", "b1", "s3:PutObject", "tree/b1/data/f.lance", True, id="branch-write-data-file"),
        pytest.param("write", "b1", "s3:PutObject", "tree/b1/_versions/9.manifest", False, id="branch-write-cannot-commit"),
        pytest.param("write", "b1", "s3:PutObject", "tree/b1/_transactions/9-x.txn", False, id="branch-write-cannot-write-a-transaction"),
        pytest.param("write", "b1", "s3:PutObject", "data/f.lance", False, id="branch-write-cannot-write-main"),
        pytest.param("maintain", "", "s3:PutObject", "_versions/9.manifest", True, id="maintain-commits"),
        pytest.param("maintain", "", "s3:PutObject", "data/f.lance", True, id="maintain-rewrites"),
        pytest.param("maintain", "", "s3:DeleteObject", "_versions/1.manifest", True, id="maintain-cleans-up-versions"),
        pytest.param("maintain", "", "s3:DeleteObject", "tree/b1/data/f.lance", True, id="maintain-cleans-up-branches"),
    ],
)
def test_a_vend_permits_only_what_its_tier_may_change(estate: _Estate, tier: _Tier, branch: str, action: str, relative: str, allowed: bool) -> None:
    _, policy = estate.vend(tier, branch)

    assert _permits(policy, action, estate.bucket, f"{_within(estate.location, estate.bucket)}/{relative}") is allowed
    # The prefix SIBLING (`<table>_secret/`): an IAM `*` matches across `/`, so a resource that dropped the
    # table's trailing delimiter would reach every key that merely starts with the table's directory name.
    sibling = f"{_within(estate.location, estate.bucket)}_secret/{relative}"
    assert not _permits(policy, action, estate.bucket, sibling), "a prefix sibling of the table is never reached"


@pytest.mark.parametrize("branch", ["", "b1"], ids=["main", "branch"])
def test_a_writer_lands_rows_with_only_what_its_vend_allows(estate: _Estate, branch: str) -> None:
    options, policy = estate.vend("write", branch)
    handle = lance.dataset(estate.location, storage_options=options)
    if branch:
        handle = handle.checkout_version((branch, None))
    before = estate.keys()

    fragments = lance.fragment.write_fragments(_rows([1, 2], blob_bytes=200_000), handle, storage_options=options)
    written = sorted(estate.keys() - before)
    committed = estate.client.post(
        f"/management/v1/table/{_TABLE}/commit",
        params={"branch": branch} if branch else {},
        json={"fragments": [f.to_json() for f in fragments], "read_version": handle.version},
    )

    assert committed.status_code == 200, committed.text
    assert written and all(_permits(policy, "s3:PutObject", estate.bucket, key) for key in written), written
    main = lance.dataset(estate.location, storage_options=options)
    landed = main.checkout_version((branch, None)) if branch else main
    assert sorted(landed.to_table(columns=["id"]).column("id").to_pylist()) == [0, 1, 2]
    assert main.count_rows() == (1 if branch else 3), "a branch commit leaves main as it was"
