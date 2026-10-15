"""A stock client commits on a branch on the live estate ([[LH-490]], ADR 0164).

The table's owner creates a branch through the spec's `CreateTableBranch` (`lance_docs/ns_catalog/spec.yaml`). A job
holding only `writer` on the table's namespace then appends, deletes and updates on the branch with stock pylance
through the namespace: it opens the branch from the table's own `DescribeTable`, stages each commit under
`tree/<branch>/` on the credential that describe vended, and the catalog judges and finalizes it at `version/create`
with `branch`. Main stays at its version. A commit the judge refuses leaves the branch at its version and answers a
typed spec error, and the store refuses the job's credential a final manifest on the branch and a ref.

The Dex users and the scratch tenant are `scratch_tenant`'s; the tenant is deleted at the end.

Run with `bash scripts/e2e_live.sh tests/e2e-py/test_a_stock_client_commits_on_a_branch.py`, which discovers every
address from the release, or `make e2e-byo-branch` with the addresses set.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import lance
import lance_namespace
import pyarrow as pa
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from scratch_tenant import CATALOG, DEX, Identity, Tenant, provisioned


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.byo_branch,
    pytest.mark.skipif(not (CATALOG and DEX), reason="set LANCE_E2E_CATALOG_URL and LANCE_E2E_DEX (a deployed estate)"),
]

#: The field metadata that makes ``id`` the table's (unenforced) primary key, which the judge holds every commit to.
_PRIMARY_KEY = {"lance-schema:unenforced-primary-key": "true"}


@pytest.fixture(scope="module")
def tenant() -> Iterator[Tenant]:
    yield from provisioned("branch", grants=("writer",))


def _keyed(ids: list[int], values: list[str]) -> pa.Table:
    schema = pa.schema([pa.field("id", pa.int64(), nullable=False, metadata=_PRIMARY_KEY), pa.field("v", pa.string())])
    return pa.table({"id": ids, "v": values}, schema=schema)


def _rows(dataset: lance.LanceDataset) -> list[tuple[int, str]]:
    table = dataset.to_table().sort_by("id")
    return list(zip(table["id"].to_pylist(), table["v"].to_pylist(), strict=True))


def _branch(identity: Identity, table_id: list[str]) -> lance.LanceDataset:
    """The head of ``work``, opened as a stock client opens it: the table's own ``DescribeTable``, then the ref."""
    return lance.dataset(namespace_client=identity.namespace(), table_id=table_id).checkout_version(("work", None))


def _vended_store(identity: Identity, table_id: list[str]) -> tuple[Any, str, str]:
    """An S3 client on the credential the table's ``DescribeTable`` vends ``identity``, the table's bucket and its prefix."""
    described = identity.namespace().describe_table(lance_namespace.DescribeTableRequest(id=table_id))
    vended = described.storage_options or {}
    bucket, _, prefix = str(described.location).removeprefix("s3://").partition("/")
    store = boto3.client(
        "s3",
        endpoint_url=vended["endpoint"],
        aws_access_key_id=vended["aws_access_key_id"],
        aws_secret_access_key=vended["aws_secret_access_key"],
        aws_session_token=vended["aws_session_token"],
        region_name=vended.get("region", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}),
    )
    return store, bucket, prefix.strip("/")


def _staged_manifests(store: Any, bucket: str, versions: str) -> list[str]:
    """The keys of the staged manifests (``<n>.manifest-<uuid>``) under ``versions``."""
    listed = store.list_objects_v2(Bucket=bucket, Prefix=versions).get("Contents", [])
    return [str(item["Key"]) for item in listed if ".manifest-" in str(item["Key"])]


def _put_refused(store: Any, bucket: str, key: str) -> str:
    """The store's error code for a put of ``key`` on the vended credential, or ``""`` when the put lands."""
    try:
        store.put_object(Bucket=bucket, Key=key, Body=b"not a manifest")
    except ClientError as exc:
        return str(exc.response.get("Error", {}).get("Code", ""))
    return ""


def test_a_stock_client_commits_on_a_branch_and_main_is_unchanged(tenant: Tenant) -> None:
    admin, job = tenant.admin, tenant.job
    table_id = [tenant.bronze, "people"]
    table = "$".join(table_id)
    lance.write_dataset(
        _keyed([1, 2, 3], ["a", "b", "c"]),
        namespace_client=admin.namespace(),
        table_id=table_id,
        mode="create",
        data_storage_version="2.2",
        enable_stable_row_ids=True,
    )
    tenant.tables.append(table)
    admin.namespace().create_table_branch(lance_namespace.CreateTableBranchRequest(id=table_id, name="work"))

    # 1. The writer commits on the branch with stock pylance, each commit staged under tree/work/ on its vended credential.
    lance.write_dataset(_keyed([4], ["d"]), _branch(job, table_id), mode="append")
    _branch(job, table_id).delete("id = 2")
    _branch(job, table_id).update({"v": "'A'"}, where="id = 1")
    work = _branch(job, table_id)
    assert (work.version, _rows(work)) == (4, [(1, "A"), (3, "c"), (4, "d")])
    main = lance.dataset(namespace_client=job.namespace(), table_id=table_id)
    assert (main.version, _rows(main)) == (1, [(1, "a"), (2, "b"), (3, "c")])

    # 2. A commit the judge refuses (a key the branch holds) leaves the branch at its version and answers a typed error.
    with pytest.raises(OSError):
        lance.write_dataset(_keyed([1], ["again"]), _branch(job, table_id), mode="append")
    store, bucket, prefix = _vended_store(job, table_id)
    (staged,) = _staged_manifests(store, bucket, f"{prefix}/tree/work/_versions/")
    answer = job.http.post(f"{CATALOG}/v1/table/{table}/version/create", json={"version": 5, "branch": "work", "manifest_path": staged}, timeout=60)
    assert (answer.status_code, answer.json().get("code")) == (400, 13), answer.text
    assert _branch(job, table_id).version == 4

    # 3. The store refuses the writer's credential a final manifest on the branch and a ref.
    assert _put_refused(store, bucket, f"{prefix}/tree/work/_versions/{(1 << 64) - 1 - 9}.manifest") == "AccessDenied"
    assert _put_refused(store, bucket, f"{prefix}/_refs/branches/forged.json") == "AccessDenied"
