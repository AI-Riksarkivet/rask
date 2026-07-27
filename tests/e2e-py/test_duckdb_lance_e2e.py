"""DuckDB reads lance-ns's Lance datasets directly via the core ``lance`` extension — proving an external
SQL engine can consume the governed lakehouse output, over the SAME S3 store, in the exact on-disk format
pylance 8.0.0 writes. This is the "queryable by DuckDB" merge-readiness condition (the rask lakehouse is
Lance-on-S3; downstream consumers must be able to read it without a bespoke reader).

The extension is DuckDB's CORE ``lance`` extension — ``INSTALL lance;``
(github.com/lance-format/lance-duckdb). It registers a ``TYPE LANCE`` secret for object-store credentials
and a ``__lance_scan(uri)`` table function. Our catalog datasets carry no ``.lance`` suffix, so DuckDB's
suffix-based replacement scan doesn't fire — the explicit ``__lance_scan`` is the read path (verified live:
it also exposes ``lance_fts`` / ``lance_vector_search`` / ``lance_hybrid_search``, but count/projection over
``__lance_scan`` is the format-interop contract).

Two facts this pins so a merge can't silently break them:
  1. FORMAT COMPAT — the DuckDB extension reads what pylance 8.0.0 writes (count + values + schema all match
     a pylance read of the same dataset). The extension has historically lagged lance-core; this catches it.
  2. OBJECT-STORE PATH — a ``TYPE LANCE`` secret pointed at the deployed RustFS (custom ENDPOINT + ALLOW_HTTP)
     reaches an ``s3://`` dataset, the same store the cascade writes to.

Run: part of ``make e2e-ci`` / ``scripts/e2e_stack.sh`` (port-forwards RustFS, exports ``LANCE_E2E_S3_*``).
Writes under the ``__duckdb_probe/`` prefix — the compaction sweep skips ``__`` prefixes, so this never
collides with the lakehouse or gets GC'd. Skips cleanly when the store env is unset or the extension can't be
fetched (offline CI).
"""

from __future__ import annotations

import os
import uuid

import boto3
import lance
import pyarrow as pa
import pytest
from botocore.config import Config


ENDPOINT = os.environ.get("LANCE_E2E_S3_ENDPOINT", "")
ACCESS_KEY = os.environ.get("LANCE_E2E_S3_ACCESS_KEY", "rustfsadmin")
SECRET_KEY = os.environ.get("LANCE_E2E_S3_SECRET_KEY", "rustfsadmin")
BUCKET = os.environ.get("LANCE_E2E_S3_BUCKET", "lance-catalog")
PREFIX = "__duckdb_probe"

pytestmark = [pytest.mark.e2e, pytest.mark.duckdb]


def _storage_options() -> dict[str, str]:
    # The keys pylance/the app pass to write to RustFS: explicit endpoint, path-style, plain HTTP.
    return {
        "aws_access_key_id": ACCESS_KEY,
        "aws_secret_access_key": SECRET_KEY,
        "aws_endpoint": ENDPOINT,
        "aws_region": "us-east-1",
        "virtual_hosted_style_request": "false",
        "allow_http": "true",
    }


def _s3():
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    )


def _cleanup(prefix: str) -> None:
    s3 = _s3()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix):
        objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objs:
            s3.delete_objects(Bucket=BUCKET, Delete={"Objects": objs})


@pytest.fixture(scope="module")
def duckdb_con():
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    try:
        con.execute("INSTALL lance;")  # DuckDB CORE extension (github.com/lance-format/lance-duckdb)
        con.execute("LOAD lance;")
    except Exception as exc:
        pytest.skip(f"duckdb lance extension unavailable (offline?): {exc}")
    # A TYPE LANCE secret forwards object-store settings to lance-core. ENDPOINT MUST carry the scheme
    # (object_store panics on a scheme-less URI); ALLOW_HTTP for the plain-HTTP RustFS/MinIO endpoint.
    con.execute(
        f"CREATE SECRET rfs (TYPE LANCE, ACCESS_KEY_ID '{ACCESS_KEY}', "
        f"SECRET_ACCESS_KEY '{SECRET_KEY}', REGION 'us-east-1', "
        f"ENDPOINT '{ENDPOINT}', ALLOW_HTTP true);"
    )
    return con


@pytest.mark.skipif(not ENDPOINT, reason="set LANCE_E2E_S3_ENDPOINT (scripts/e2e_stack.sh)")
def test_duckdb_reads_pylance_dataset_over_s3(duckdb_con) -> None:
    run = uuid.uuid4().hex[:8]
    uri = f"s3://{BUCKET}/{PREFIX}/ds_{run}"
    tbl = pa.table({"id": [1, 2, 3, 4], "kind": ["a", "b", "a", "c"], "score": [1.5, 2.5, 3.5, 4.5]})
    lance.write_dataset(tbl, uri, storage_options=_storage_options())
    try:
        # 1. FORMAT COMPAT: DuckDB's row count equals a pylance read of the same dataset.
        truth = lance.dataset(uri, storage_options=_storage_options()).count_rows()
        n = duckdb_con.execute(f"SELECT count(*) FROM __lance_scan('{uri}')").fetchone()[0]
        assert n == truth == 4

        # 2. Projection + aggregation return the correct values (real column read, not just a count).
        got = duckdb_con.execute(f"SELECT kind, count(*) c, sum(score) s FROM __lance_scan('{uri}') GROUP BY kind ORDER BY kind").fetchall()
        assert got == [("a", 2, 5.0), ("b", 1, 2.5), ("c", 1, 4.5)]

        # 3. The Lance schema is surfaced to DuckDB in column order.
        cols = [c[0] for c in duckdb_con.execute(f"DESCRIBE SELECT * FROM __lance_scan('{uri}')").fetchall()]
        assert cols == ["id", "kind", "score"]
    finally:
        _cleanup(f"{PREFIX}/ds_{run}")
