"""Standalone S3 smoke test — head-bucket + list, against any backend (HCP / MinIO / rustfs / AWS).

Run:
    HCP_INSECURE=1 uv run python scripts/smoke_s3.py [bucket] [prefix]
or with creds via .env at the repo root.
"""

import os
import sys
import time

from dotenv import load_dotenv


def main() -> None:
    load_dotenv()
    # The client the estate actually ships: `storage.s3_client` resolves endpoint,
    # insecure/CA flags and credentials — including the per-client HCP bridge — so
    # smoking IT smokes the production path. `derive_hcp_creds` is pure (it returns
    # the pair, mutating nothing), so it serves only the diagnostic print here.
    from storage import derive_hcp_creds, s3_client

    endpoint = os.getenv("RASK_S3_ENDPOINT_URL") or os.getenv("S3_ENDPOINT_URL") or os.getenv("HCP_ENDPOINT")
    print(f"endpoint: {endpoint}", flush=True)
    if os.getenv("AWS_ACCESS_KEY_ID"):
        creds_source = "env AWS_*"
    elif derive_hcp_creds() is not None:
        creds_source = "derived from HCP_USERNAME/PASSWORD (applied per client)"
    else:
        creds_source = "NONE RESOLVED — requests will be unsigned"
    print(f"creds: {creds_source}", flush=True)

    # `s3_client` reads RASK_S3_CA_BUNDLE / S3_CA_BUNDLE; HCP_CA_BUNDLE is this
    # script's documented legacy spelling, so bridge it (this process only).
    if (ca := os.getenv("HCP_CA_BUNDLE")) and not os.getenv("RASK_S3_CA_BUNDLE"):
        os.environ["RASK_S3_CA_BUNDLE"] = ca

    print("building client...", flush=True)
    t0 = time.perf_counter()
    c = s3_client()
    print(f"  built in {time.perf_counter() - t0:.2f}s", flush=True)

    bucket = sys.argv[1] if len(sys.argv) > 1 else "images-batch"
    prefix = sys.argv[2] if len(sys.argv) > 2 else "A0060198/"
    print(f"\nProbing bucket={bucket!r} prefix={prefix!r}", flush=True)

    print("\n[1] head_bucket:", flush=True)
    t0 = time.perf_counter()
    try:
        c.head_bucket(Bucket=bucket)
        print(f"  OK ({time.perf_counter() - t0:.2f}s)", flush=True)
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "?")
        print(f"  FAIL ({time.perf_counter() - t0:.2f}s): {type(e).__name__} code={code}", flush=True)
        print(f"  msg: {str(e)[:200]}", flush=True)

    print("\n[2] list_objects_v2 (max 5):", flush=True)
    t0 = time.perf_counter()
    try:
        resp = c.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=5)
        keys = [obj["Key"] for obj in resp.get("Contents", [])]
        print(f"  OK ({time.perf_counter() - t0:.2f}s) — {len(keys)} keys, IsTruncated={resp.get('IsTruncated')}", flush=True)
        for k in keys:
            print(f"    {k}", flush=True)
    except Exception as e:
        code = getattr(e, "response", {}).get("Error", {}).get("Code", "?")
        print(f"  FAIL ({time.perf_counter() - t0:.2f}s): {type(e).__name__} code={code}", flush=True)
        print(f"  msg: {str(e)[:200]}", flush=True)


if __name__ == "__main__":
    main()
