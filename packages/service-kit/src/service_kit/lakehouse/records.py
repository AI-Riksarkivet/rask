"""Conditional-create for control-root JSON records — the STORE arbitrates, never a prior read.

Every registry write used to be a plain ``open_output_stream`` overwrite, which made each guard in
front of one check-then-act: two concurrent creates of one warehouse id under different projects
both read "absent" and the last writer won — on exactly the tenant-isolation guards
(the same defect class as ``open_python-audit.md`` CAT-CORE-05).
The store demonstrably honors put-if-not-exists (``tests/e2e-py/test_object_store_cas_e2e.py``
drives ``If-None-Match: *`` against RustFS with contended writers — it is the primitive Lance's own
manifest commits stand on), so the id-minting doors route through this ONE seam instead of relying
on a read that can go stale between the check and the write.

Two branches, one contract:

- ``s3://`` roots: ``put_object(..., IfNoneMatch="*")`` on a ``storage.s3_client`` — the store rejects
  the second create with 412 ``PreconditionFailed``. pyarrow's ``S3FileSystem`` cannot express a
  conditional PUT, which is why this write (alone among the registry IO) goes through an S3 client.
- local roots (dev/tests): ``open(path, "xb")`` — ``O_CREAT|O_EXCL``, the OS arbitrates. Same
  exactly-one-winner semantics, so the unit suite proves the door logic without object storage.

A refused create raises :class:`RecordExistsError`; the caller decides what a lost race means
(idempotent convergence on an identical record, or the spec's AlreadyExists/ConcurrentModification).

The MUTATION half (F4) lives here too, as :func:`mutate_json` — this module used to say a
read-modify-write "needs a separate seam", and it does need different mechanics, but not a different
home: splitting them invited exactly the mistake the create branch was written to prevent, a caller
reaching for the create primitive because it was the only conditional thing on offer.

An RMW cannot be arbitrated by put-if-not-exists — the key exists, that is the point — so it is
conditioned on the record's ETag instead: read it, apply the mutation to the record you actually
read, and write back only if nobody else has written since. A lost race re-reads and re-applies
rather than failing, because the mutations here are field-level (flip ``status``, toggle
``protected``) and almost always commute; what must never happen is a whole stale record landing on
top of a fresh one and silently reverting a field the caller never looked at.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
from collections.abc import Callable
from typing import Any

import pyarrow.fs as pafs

from service_kit.lakehouse.objectfs import StorageOptions


log = logging.getLogger(__name__)


class RecordExistsError(Exception):
    """Create refused: the key already exists — the STORE said so, not a possibly-stale read."""


def _s3_client(storage_options: StorageOptions) -> Any:  # noqa: ANN401 — boto3 client has no public stub
    """The S3 client the three conditional branches share, built through ``storage.s3_client``.

    ``packages/storage`` is the estate's canonical S3 seam and owns, in one place, everything a
    hand-rolled client silently forfeits: path-style addressing (matching
    ``lance_storage_options(virtual_hosted=False)`` — RustFS/MinIO reject virtual-hosted signing with
    403 SignatureDoesNotMatch), s3v4, adaptive retries, and connect/read timeouts. The timeouts matter
    most here: these doors are on the synchronous path of every warehouse and project mint, and
    botocore's default is to wait forever.

    The registry's ``storage_options`` carry a per-warehouse ``region`` and, when the caller holds
    vended credentials, a ``session_token``; both are forwarded. A dropped region pins the warehouse
    to ``AWS_REGION``, and a dropped token makes a scoped credential sign as an unknown identity.

    ``storage`` is imported at call time so a test can replace the SEAM rather than this wrapper —
    patching the wrapper would let a regression back to a raw client pass unnoticed — and so the
    local-root branch below needs no S3 stack at all.
    """
    from storage import s3_client

    return s3_client(
        storage_options.get("endpoint"),
        access_key=storage_options.get("access_key_id"),
        secret_key=storage_options.get("secret_access_key"),
        session_token=storage_options.get("session_token"),
        region=storage_options.get("region") or "us-east-1",
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


class RecordChangedError(Exception):
    """Conditional replace refused: the record moved under us between the read and the write."""


class RecordMissingError(Exception):
    """Mutation asked for a record that does not exist. Distinct from a lost race on purpose — one
    means "somebody else wrote", the other means "there is nothing here to mutate"."""


# How many read→apply→write rounds `mutate_json` will run before giving up. Contention here is two
# admins (or an admin and a GitOps reconcile) touching ONE warehouse record, so rounds are cheap and
# collisions are rare; the bound exists to turn a pathological livelock into an honest error rather
# than to absorb sustained load.
_MUTATE_ATTEMPTS = 5


def _s3_parts(root_uri: str, key: str) -> tuple[str, str]:
    base = root_uri[len("s3://") :].rstrip("/")
    bucket, _, prefix = base.partition("/")
    return bucket, (f"{prefix}/{key}" if prefix else key)


def read_json(root_uri: str, storage_options: StorageOptions, key: str) -> tuple[dict[str, Any], str] | None:
    """The record at ``key`` and its ETag, or ``None`` when absent.

    The ETag is the concurrency token :func:`mutate_json` writes back under. On S3 it is the store's
    own; on a local root it is a content hash, which has the same meaning ("the bytes I read") and
    the same failure behaviour, without needing the filesystem to invent one.
    """
    if root_uri.startswith("s3://"):
        from botocore.exceptions import ClientError

        bucket, full_key = _s3_parts(root_uri, key)
        try:
            got = _s3_client(storage_options).get_object(Bucket=bucket, Key=full_key)
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code", "")) in {"NoSuchKey", "404"}:
                return None
            raise
        return json.loads(got["Body"].read().decode("utf-8")), str(got["ETag"]).strip('"')

    fs, path = pafs.FileSystem.from_uri(root_uri)
    if not isinstance(fs, pafs.LocalFileSystem):
        raise NotImplementedError(f"conditional read is not supported for root {root_uri!r} ({type(fs).__name__})")
    target = os.path.join(path.rstrip("/"), key)
    try:
        with open(target, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return None
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def mutate_json(
    root_uri: str,
    storage_options: StorageOptions,
    key: str,
    mutate: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    attempts: int = _MUTATE_ATTEMPTS,
) -> dict[str, Any]:
    """Read ``key``, apply ``mutate``, write it back only if nobody else wrote in between.

    THE BUG THIS CLOSES (diff2 F4). Every mutable registry write was
    ``get → mutate a dict → put`` with no precondition, so a quarantine could be silently lifted by
    interleaving rather than by anyone calling ``/activate``: a GitOps re-POST reads the record at
    t0 (status=active), an operator deactivates at t1, and the re-POST's put lands at t2 carrying
    the status it read at t0. The record it writes is not wrong in any field it *meant* to change —
    it is stale in a field it merely carried forward. That is why the existing carry-forward comment
    ("a GitOps reconcile would otherwise silently lift a quarantine") reads as a defence and is one,
    but only against the SEQUENTIAL case.

    ``mutate`` receives the record as it was actually read and returns the record to write; it may
    be called more than once, so it must be a pure function of its argument and must not carry state
    in from an earlier read. That signature is the guard rail: a caller cannot accidentally write a
    record it fetched somewhere else, because it never supplies one.

    Raises :class:`RecordMissingError` if the key is absent, and :class:`RecordChangedError` if
    ``attempts`` rounds all lose the race.
    """
    for _ in range(attempts):
        current = read_json(root_uri, storage_options, key)
        if current is None:
            raise RecordMissingError(f"record {key!r} does not exist under {root_uri!r}")
        record, etag = current
        updated = mutate(dict(record))
        try:
            _replace_json(root_uri, storage_options, key, updated, etag=etag)
        except RecordChangedError:
            continue  # somebody else wrote; re-read and re-apply to THEIR record
        return updated
    raise RecordChangedError(f"record {key!r} under {root_uri!r} kept changing across {attempts} attempts")


def _replace_json(root_uri: str, storage_options: StorageOptions, key: str, record: dict[str, Any], *, etag: str) -> None:
    """Overwrite ``key`` iff its current ETag is ``etag``. Raises :class:`RecordChangedError` if not.

    Private because the read and the write must not be separable by a caller: handing out a bare
    conditional put invites the read to drift from the write, which is the F4 shape all over again.
    Go through :func:`mutate_json`.
    """
    body = json.dumps(record).encode("utf-8")

    if root_uri.startswith("s3://"):
        from botocore.exceptions import ClientError

        bucket, full_key = _s3_parts(root_uri, key)
        try:
            _s3_client(storage_options).put_object(Bucket=bucket, Key=full_key, Body=body, IfMatch=etag)
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if status in {409, 412} or code in {"PreconditionFailed", "ConditionalRequestConflict"}:
                raise RecordChangedError(f"record {key!r} changed under {root_uri!r}") from exc
            raise
        return

    fs, path = pafs.FileSystem.from_uri(root_uri)
    if not isinstance(fs, pafs.LocalFileSystem):
        raise NotImplementedError(f"conditional replace is not supported for root {root_uri!r} ({type(fs).__name__})")
    target = os.path.join(path.rstrip("/"), key)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    # An exclusive lock across the compare AND the swap. A bare re-read-then-write would leave the
    # same window this function exists to close, only narrower — and a narrower race is the kind that
    # survives testing and fails in production. `flock` serializes cooperating writers on one host,
    # which is exactly the scope of a local root (dev + the unit suite); s3:// is the multi-replica
    # path and it has the store's own precondition above.
    with open(target, "a+b") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.seek(0)
            if hashlib.sha256(fh.read()).hexdigest() != etag:
                raise RecordChangedError(f"record {key!r} changed under {root_uri!r}")
            fh.seek(0)
            fh.truncate()
            fh.write(body)
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
