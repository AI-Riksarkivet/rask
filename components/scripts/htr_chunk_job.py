"""Transcribe a chunk's pages via the cluster /htr endpoint, write ALTO to S3.

Submitted by the viewer orchestrator as a Ray job (the entrypoint runs on the
head node, where the Serve proxy is reachable at localhost:8000). Self-contained
— NO rask imports; needs only boto3 (installed via the job runtime_env) + stdlib.

S3 creds come from the env handed to the job: AWS_ACCESS_KEY_ID/SECRET (derived
from HCP_USERNAME/PASSWORD by the viewer and passed through), HCP_ENDPOINT,
HCP_INSECURE. For each --batch: list <cache-bucket>/<batch>/ *.jpg; pages whose
ALTO already exists in <output>/<batch>/ are skipped (resumable); otherwise GET
the image, POST it to /htr/transcribe, and write the returned ALTO XML.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import logging
import os
import sys
import urllib.request
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore.config import Config


log = logging.getLogger("htr_chunk_job")


def bucket_name(uri: str) -> str:
    return urlparse(uri).netloc if uri.startswith("s3://") else uri


def is_jpg(key: str) -> bool:
    return key.lower().endswith(".jpg")


def out_key(jpg_key: str) -> str:
    """`<batch>/<stem>.jpg` → `<batch>/<stem>.xml` (output is keyed identically)."""
    batch, _, fname = jpg_key.partition("/")
    stem = fname.rsplit(".", 1)[0]
    return f"{batch}/{stem}.xml"


def s3_client() -> Any:
    kwargs: dict = {
        "endpoint_url": os.environ.get("HCP_ENDPOINT"),
        "config": Config(
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "adaptive"},
        ),
    }
    if os.environ.get("HCP_INSECURE", "").lower() in ("1", "true", "yes"):
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        kwargs["verify"] = False
    return boto3.client("s3", **kwargs)


def list_keys(s3: Any, bucket: str, prefix: str, suffix_ok) -> list[str]:
    out: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        out.extend(o["Key"] for o in page.get("Contents", []) if suffix_ok(o["Key"]))
    return out


def transcribe(endpoint: str, img: bytes) -> str:
    req = urllib.request.Request(endpoint, data=img, headers={"Content-Type": "application/octet-stream"}, method="POST")
    with urllib.request.urlopen(req, timeout=600) as resp:
        return resp.read().decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-bucket", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--endpoint", default="http://localhost:8000/htr/transcribe")
    ap.add_argument("--batch", action="append", required=True)
    ap.add_argument("--concurrency", type=int, default=8)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    s3 = s3_client()
    out_bucket = bucket_name(a.output)

    # Plan the work: every jpg page across the requested batches, minus pages
    # whose ALTO already exists (resumable). One output listing per batch.
    todo: list[str] = []
    for batch in a.batch:
        done = {k for k in list_keys(s3, out_bucket, f"{batch}/", lambda k: k.lower().endswith(".xml"))}
        pages = list_keys(s3, a.cache_bucket, f"{batch}/", is_jpg)
        todo.extend(k for k in pages if out_key(k) not in done)
        log.info("batch %s: %d pages, %d already done", batch, len(pages), len(pages) - sum(out_key(k) not in done for k in pages))
    log.info("htr_chunk_job: %d pages to transcribe (concurrency=%d)", len(todo), a.concurrency)

    def work(key: str) -> None:
        img = s3.get_object(Bucket=a.cache_bucket, Key=key)["Body"].read()
        alto = transcribe(a.endpoint, img)
        s3.put_object(Bucket=out_bucket, Key=out_key(key), Body=alto.encode("utf-8"), ContentType="application/xml")

    ok = fail = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        futs = {ex.submit(work, k): k for k in todo}
        for f in concurrent.futures.as_completed(futs):
            try:
                f.result()
                ok += 1
            except Exception as exc:
                fail += 1
                log.warning("page %s failed: %s", futs[f], str(exc)[:200])
    log.info("htr_chunk_job done: ok=%d fail=%d", ok, fail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
