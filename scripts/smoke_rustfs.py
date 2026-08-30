"""rustfs (S3-compatible) storage smoke — proves rask's storage works against a REAL
rustfs backend, not just moto.

Run (with rustfs up on :9000 via `make rustfs-up`):
    make smoke-rustfs

or manually against any S3-compatible endpoint (rustfs / MinIO / AWS):
    RASK_S3_ENDPOINT_URL=http://localhost:9000 \
    AWS_ACCESS_KEY_ID=rustfsadmin AWS_SECRET_ACCESS_KEY=rustfsadmin \
    RASK_S3_INSECURE=1 uv run python scripts/smoke_rustfs.py

It exercises the SAME code the services use:
  - `storage.s3_client()` (boto3 — the volumes/core path): bucket + object round-trip.
  - LanceDB over `s3://` (the search/catalog path — object_store, the strictest
    S3-compat surface): create a table, read it back.
Exits non-zero on any failure. No credentials are hard-coded; everything is env-driven
(the agnostic invariant — swap rustfs/MinIO/AWS with env only).
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Callable

from dotenv import load_dotenv


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
    from storage import configured_endpoint, s3_client

    # `configured_endpoint` rather than a local env chain: this value both reports the backend and
    # decides whether the smoke runs at all, so a copy that omits an alias refuses a backend
    # `s3_client()` would have reached — a green-looking refusal, the worst answer available here.
    endpoint = configured_endpoint()
    print(f"endpoint: {endpoint}", flush=True)
    print(f"creds set: {bool(os.getenv('AWS_ACCESS_KEY_ID') and os.getenv('AWS_SECRET_ACCESS_KEY'))}", flush=True)
    if not endpoint:
        print("RASK_S3_ENDPOINT_URL not set — point it at rustfs (`make rustfs-up` serves :9000).", flush=True)
        return 2

    bucket = os.getenv("RASK_SMOKE_BUCKET", "rask-rustfs-smoke")
    key = f"smoke/{uuid.uuid4().hex}.txt"
    body = b"hello rustfs from rask storage"
    ok = True

    # ---- boto3 path: the storage.s3_client the volumes/core services use ----------
    print("\n== boto3 path (storage.s3_client) ==", flush=True)
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

    # ---- LanceDB path: search/catalog tables over s3:// (object_store) -------------
    print("\n== LanceDB path (s3:// — search/catalog) ==", flush=True)
    try:
        import lancedb
    except Exception as e:
        print(f"  SKIP  lancedb not importable here: {e}", flush=True)
    else:
        # Same storage_options shape as service_kit.config.lance_storage_options().
        storage_opts = {
            "aws_endpoint": endpoint,
            "aws_access_key_id": os.environ["AWS_ACCESS_KEY_ID"],
            "aws_secret_access_key": os.environ["AWS_SECRET_ACCESS_KEY"],
            "aws_region": os.getenv("AWS_REGION", "us-east-1"),
            "aws_virtual_hosted_style_request": "false",
            "allow_http": "true" if endpoint.startswith("http://") else "false",
        }
        if os.getenv("RASK_S3_INSECURE", "").lower() in ("1", "true", "yes"):
            storage_opts["allow_invalid_certificates"] = "true"
        uri = f"s3://{bucket}/lance-smoke"

        def lance_roundtrips() -> None:
            db = lancedb.connect(uri, storage_options=storage_opts)
            db.create_table(
                "t",
                data=[{"id": 1, "text": "rustfs"}, {"id": 2, "text": "ok"}],
                mode="overwrite",
            )
            n = db.open_table("t").count_rows()
            assert n == 2, f"expected 2 rows, got {n}"

        ok &= _check("lancedb create + read over s3://", lance_roundtrips)

    print("\n" + ("ALL PASS — rustfs works with rask storage." if ok else "FAILURES above (see ^)."), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
