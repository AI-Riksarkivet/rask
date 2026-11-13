"""#3-A — live per-warehouse physical isolation against the DEPLOYED catalog.

Proves the completion condition end to end: a project holds two warehouses, each on a bucket the catalog creates at
RUNTIME through the API (not the chart's bucket hook), and a table created under a namespace bound to the first lands in
the first bucket and is ABSENT from the second.

Both warehouses are this module's own, made per run in a scratch project (`scratch_tenant`) and removed with their
buckets at the end, so no run leaves a warehouse on the estate and no shared project gains one while it runs. The
buckets are listed with the store's own pair, which the runner hands it. Run via
``bash scripts/e2e_live.sh tests/e2e-py/test_warehouses_e2e.py``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pyarrow as pa
import pyarrow.fs as pafs
import pytest
from credentials import Credential, credential
from scratch_tenant import CATALOG, DEX, Tenant, ok, provisioned

from lakehouse.arrow_ipc import ARROW_STREAM_MEDIA_TYPE, encode_arrow_stream


S3 = os.environ.get("LANCE_E2E_S3", "")
S3_ACCESS_KEY = credential(Credential.S3_ACCESS_KEY, "rustfsadmin")
S3_SECRET_KEY = credential(Credential.S3_SECRET_KEY, "rustfsadmin")

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(not (CATALOG and DEX and S3), reason="set LANCE_E2E_CATALOG_URL, LANCE_E2E_DEX and LANCE_E2E_S3 (a deployed estate)"),
]


def _list_bucket(bucket: str) -> list[str]:
    fs = pafs.S3FileSystem(access_key=S3_ACCESS_KEY, secret_key=S3_SECRET_KEY, endpoint_override=S3, scheme="http", region="")
    return [info.path for info in fs.get_file_info(pafs.FileSelector(bucket, allow_not_found=True, recursive=True))]


@pytest.fixture(scope="module")
def tenant() -> Iterator[Tenant]:
    yield from provisioned("whiso", grants=())


@pytest.fixture(scope="module")
def second_warehouse(tenant: Tenant) -> Iterator[str]:
    """A second warehouse of the scratch project, on a bucket of its own; deleted with its bucket at the end."""
    warehouse = f"{tenant.project}-b"
    answer = ok(tenant.admin.http.post(f"{CATALOG}/management/v1/warehouses", json={"id": warehouse, "project": tenant.project}, timeout=30))
    assert answer["root_uri"] == f"s3://{warehouse}", f"a second warehouse was not given a bucket of its own: {answer}"
    try:
        yield warehouse
    finally:
        params = {"cascade": "true", "purge_bucket": "true"}
        ok(tenant.admin.http.delete(f"{CATALOG}/management/v1/warehouses/{warehouse}", params=params, timeout=120))


def test_a_table_lands_in_its_warehouses_bucket_and_in_no_other(tenant: Tenant, second_warehouse: str) -> None:
    table = f"{tenant.bronze}$e2etbl"

    created = ok(
        tenant.admin.http.post(
            f"{CATALOG}/v1/table/{table}/create",
            data=encode_arrow_stream(pa.table({"id": pa.array([1, 2, 3], pa.int64())})),
            headers={"content-type": ARROW_STREAM_MEDIA_TYPE},
            timeout=60,
        )
    )
    tenant.tables.append(table)

    # The table lives where its create answered: `<namespace root>/<table uuid>` (ADR 0173 §5), so the uuid directory is
    # what each bucket is searched for.
    bucket, _, key = str(created["location"]).removeprefix("s3://").partition("/")
    assert bucket == tenant.project, f"a table under a namespace bound to {tenant.project} was located in {bucket!r}"
    assert any(path.startswith(f"{bucket}/{key}/_versions/") for path in _list_bucket(bucket)), f"no manifest of {table} in {bucket}"
    assert not any(key in path for path in _list_bucket(second_warehouse)), f"{table}'s bytes reached the other warehouse's bucket"
