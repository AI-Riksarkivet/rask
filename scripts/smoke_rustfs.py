"""rustfs (S3-compatible) storage smoke — proves rask's storage works against a REAL
rustfs backend, not just moto.

Run (Dagger starts a RustFS service for the life of the smoke):
    make smoke-rustfs

or manually against any S3-compatible endpoint (RustFS / AWS / any S3):
    RASK_S3_ENDPOINT_URL=http://localhost:9000 \
    AWS_ACCESS_KEY_ID=rustfsadmin AWS_SECRET_ACCESS_KEY=rustfsadmin \
    RASK_S3_INSECURE=1 uv run python scripts/smoke_rustfs.py

It exercises the SAME code the lakehouse uses:
  - `lakehouse.storage.s3_client()` (boto3, the client the registries' conditional writes use): bucket + object round-trip.
  - pylance over `s3://` (Lance's object_store, the catalog's data plane and the strictest S3-compat
    surface): create a 2.2 table with stable row ids, as the catalog's create does, and read it back.
Exits non-zero on any failure. No credentials are hard-coded; everything is env-driven
(the agnostic invariant — swap RustFS/AWS/any S3 with env only).
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Callable

import lance
import pyarrow as pa
from dotenv import load_dotenv

from lakehouse.endpoint_scheme import allow_http_for
from lakehouse.storage import configured_endpoint, s3_client
from lakehouse.storage.client import AwsRegionSettings, S3EnvSettings


def _check(label: str, fn: Callable[[], object]) -> bool:
    try:
        fn()
        print(f"  PASS  {label}", flush=True)
        return True
    except Exception as e:
        print(f"  FAIL  {label}: {type(e).__name__}: {str(e)[:240]}", flush=True)
        return False


def main() -> int:
    load_dotenv()
    # `configured_endpoint` rather than a local env chain: this value both reports the backend and
    # decides whether the smoke runs at all, so a copy that omits an alias refuses a backend
    # `s3_client()` would have reached — a green-looking refusal, the worst answer available here.
    endpoint = configured_endpoint()
    print(f"endpoint: {endpoint}", flush=True)
    print(f"creds set: {bool(os.getenv('AWS_ACCESS_KEY_ID') and os.getenv('AWS_SECRET_ACCESS_KEY'))}", flush=True)
    if not endpoint:
        print("RASK_S3_ENDPOINT_URL not set — point it at an S3 endpoint (`make smoke-rustfs` runs this against RustFS).", flush=True)
        return 2

    bucket = os.getenv("RASK_SMOKE_BUCKET", "rask-rustfs-smoke")
    key = f"smoke/{uuid.uuid4().hex}.txt"
    body = b"hello rustfs from rask storage"
    ok = True

    # ---- boto3 path: lakehouse.storage.s3_client, the lakehouse's S3 client ----
    print("\n== boto3 path (lakehouse.storage.s3_client) ==", flush=True)
    c = s3_client()

    def ensure_bucket() -> None:
        try:
            c.head_bucket(Bucket=bucket)
        except Exception:
            c.create_bucket(Bucket=bucket)

    def list_has_key() -> None:
        resp = c.list_objects_v2(Bucket=bucket, Prefix="smoke/")
        keys = [o["Key"] for o in resp.get("Contents", [])]
        assert key in keys, f"{key!r} not in {keys}"

    def get_roundtrips() -> None:
        got = c.get_object(Bucket=bucket, Key=key)["Body"].read()
        assert got == body, f"body mismatch: {got!r}"

    ok &= _check("create/ensure bucket", ensure_bucket)
    ok &= _check("put_object", lambda: c.put_object(Bucket=bucket, Key=key, Body=body))
    ok &= _check("list_objects_v2 sees the key", list_has_key)
    ok &= _check("get_object round-trips bytes", get_roundtrips)
    ok &= _check("head_object", lambda: c.head_object(Bucket=bucket, Key=key))
    ok &= _check("delete_object", lambda: c.delete_object(Bucket=bucket, Key=key))

    # ---- Lance path: a table over s3:// through Lance's object_store -------------
    print("\n== Lance path (s3:// — the catalog's data plane) ==", flush=True)
    storage_opts = {
        "aws_endpoint": endpoint,
        "aws_access_key_id": os.environ["AWS_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["AWS_SECRET_ACCESS_KEY"],
        "aws_region": AwsRegionSettings().aws_region,
        "aws_virtual_hosted_style_request": "false",
        "allow_http": allow_http_for(endpoint),
    }
    if S3EnvSettings().insecure:
        storage_opts["allow_invalid_certificates"] = "true"
    # A fresh path per run, so every run is a create: stable row ids are create-time-only
    # (`lance_docs/lance/format/table/row_id_lineage.md` § Enabling Stable Row IDs), and a write onto an earlier run's
    # table would not be the create the catalog makes.
    uri = f"s3://{bucket}/lance-smoke/{uuid.uuid4().hex}"

    def lance_roundtrips() -> None:
        table = pa.table({"id": [1, 2], "text": ["rustfs", "ok"]})
        lance.write_dataset(table, uri, mode="create", storage_options=storage_opts, data_storage_version="2.2", enable_stable_row_ids=True)
        ds = lance.dataset(uri, storage_options=storage_opts)
        read = (ds.count_rows(), ds.data_storage_version, ds.has_stable_row_ids)
        assert read == (2, "2.2", True), f"expected 2 rows at file version 2.2 with stable row ids, read {read}"

    ok &= _check("pylance create + read over s3://", lance_roundtrips)

    print("\n" + ("ALL PASS — rustfs works with rask storage." if ok else "FAILURES above (see ^)."), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
