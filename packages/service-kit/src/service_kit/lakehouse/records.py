"""Conditional-create for control-root JSON records — the STORE arbitrates, never a prior read.

Every registry write used to be a plain ``open_output_stream`` overwrite, which made each guard in
front of one check-then-act: two concurrent creates of one warehouse id under different projects
both read "absent" and the last writer won — on exactly the tenant-isolation guards
(``open_lakehouse_diff2.md`` F1; the same defect class as ``open_python-audit.md`` CAT-CORE-05).
The store demonstrably honors put-if-not-exists (``tests/e2e-py/test_object_store_cas_e2e.py``
drives ``If-None-Match: *`` against RustFS with contended writers — it is the primitive Lance's own
manifest commits stand on), so the id-minting doors route through this ONE seam instead of relying
on a read that can go stale between the check and the write.

Two branches, one contract:

- ``s3://`` roots: boto3 ``put_object(..., IfNoneMatch="*")`` — the store rejects the second create
  with 412 ``PreconditionFailed``. pyarrow's ``S3FileSystem`` cannot express a conditional PUT,
  which is why this write (alone among the registry IO) goes through boto3.
- local roots (dev/tests): ``open(path, "xb")`` — ``O_CREAT|O_EXCL``, the OS arbitrates. Same
  exactly-one-winner semantics, so the unit suite proves the door logic without object storage.

A refused create raises :class:`RecordExistsError`; the caller decides what a lost race means
(idempotent convergence on an identical record, or the spec's AlreadyExists/ConcurrentModification).
This module is deliberately create-only: mutable-field read-modify-writes (warehouse ``status``,
protection toggles) need an ETag-conditioned replace — F4, a separate seam — and pretending this
primitive covers them would re-create the overwrite bug one level up. ``#85`` (collapse the four
control-root stores) should build on this seam rather than beside it.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import pyarrow.fs as pafs

from service_kit.lakehouse.objectfs import StorageOptions


log = logging.getLogger(__name__)


class RecordExistsError(Exception):
    """Create refused: the key already exists — the STORE said so, not a possibly-stale read."""


def _s3_client(storage_options: StorageOptions) -> Any:  # noqa: ANN401 — boto3 client has no public stub
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=storage_options.get("endpoint"),
        aws_access_key_id=storage_options.get("access_key_id"),
        aws_secret_access_key=storage_options.get("secret_access_key"),
        region_name=storage_options.get("region") or "us-east-1",
        # Path-style, matching `lance_storage_options(virtual_hosted=False)`: RustFS/MinIO reject
        # virtual-hosted signing with 403 SignatureDoesNotMatch.
        config=Config(s3={"addressing_style": "path"}),
    )


def create_json(root_uri: str, storage_options: StorageOptions, key: str, record: dict[str, Any]) -> None:
    """Write ``record`` as JSON at ``<root_uri>/<key>`` iff the key does not exist.

    Raises :class:`RecordExistsError` when the key is already present (the lost-race signal), and
    lets every OTHER failure (auth, throttling, outage) surface as itself — laundering an outage
    into "already exists" would turn a 503 into a silent 409.
    """
    body = json.dumps(record).encode("utf-8")

    if root_uri.startswith("s3://"):
        from botocore.exceptions import ClientError

        base = root_uri[len("s3://") :].rstrip("/")
        bucket, _, prefix = base.partition("/")
        full_key = f"{prefix}/{key}" if prefix else key
        try:
            _s3_client(storage_options).put_object(Bucket=bucket, Key=full_key, Body=body, IfNoneMatch="*")
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if status == 412 or code == "PreconditionFailed":
                raise RecordExistsError(f"record {key!r} already exists under {root_uri!r}") from exc
            raise
        return

    fs, path = pafs.FileSystem.from_uri(root_uri)
    if not isinstance(fs, pafs.LocalFileSystem):
        # No third backend exists today (s3:// or local). Failing loud beats a check-then-write
        # emulation that would silently re-introduce the race this module exists to close.
        raise NotImplementedError(f"conditional create is not supported for root {root_uri!r} ({type(fs).__name__})")
    target = os.path.join(path.rstrip("/"), key)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        with open(target, "xb") as fh:  # O_CREAT|O_EXCL — the OS arbitrates exactly one winner
            fh.write(body)
    except FileExistsError as exc:
        raise RecordExistsError(f"record {key!r} already exists under {root_uri!r}") from exc
