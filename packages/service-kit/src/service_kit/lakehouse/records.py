"""Conditional-create for control-root JSON records — the STORE arbitrates, never a prior read.

Every registry write used to be a plain ``open_output_stream`` overwrite, which made each guard in
front of one check-then-act: two concurrent creates of one warehouse id under different projects
both read "absent" and the last writer won — on exactly the tenant-isolation guards
(the same defect class as `docs/adr/0047-the-python-estate-audit-2026-08-07-2026-09-05.md "The Python estate audit"` CAT-CORE-05).
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
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import pyarrow.fs as pafs
from pydantic import BaseModel, ConfigDict

from service_kit.lakehouse.objectfs import StorageOptions


log = logging.getLogger(__name__)


class RecordExistsError(Exception):
    """Create refused: the key already exists — the STORE said so, not a possibly-stale read."""


def _s3_client(storage_options: StorageOptions) -> Any:  # noqa: ANN401 — boto3 client has no public stub
    """The S3 client the three conditional branches share, built through ``storage.s3_client``.

    ``packages/storage`` is the estate's canonical S3 seam and owns, in one place, everything a
    hand-rolled client silently forfeits: path-style addressing (matching
    ``lance_storage_options(virtual_hosted=False)`` — RustFS rejects virtual-hosted signing with
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

    _access_key, _secret_key, _session_token = storage_options.credential_in_clear()
    return s3_client(
        storage_options.options.get("endpoint"),
        access_key=_access_key,
        secret_key=_secret_key,
        session_token=_session_token,
        region=storage_options.options.get("region") or "us-east-1",
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
    # Written aside, then LINKED into place: `link` fails when the target exists, so the OS still arbitrates
    # exactly one winner, and the record appears whole. An `O_EXCL` open followed by a write let a reader
    # that lost the create read the empty file in between (measured: JSONDecodeError in 1 of ~6 runs of
    # eight concurrent claims, [[LH-204]]); an S3 PUT is atomic and has no such window.
    staged = f"{target}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    with open(staged, "xb") as fh:
        fh.write(body)
    try:
        os.link(staged, target)
    except FileExistsError as exc:
        raise RecordExistsError(f"record {key!r} already exists under {root_uri!r}") from exc
    finally:
        os.unlink(staged)


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


class Stored(BaseModel):
    """A record as the store holds it: its body, and the ETag a write acting on it must still match."""

    model_config = ConfigDict(frozen=True)

    record: dict[str, Any]
    etag: str


class StoreRead(BaseModel):
    """What one read found at a key (the stored record, or none) and the store's own clock at that read."""

    model_config = ConfigDict(frozen=True)

    stored: Stored | None
    #: The store's clock when it answered: an S3 response's ``Date`` (RFC 9110 §6.6.1 has every origin server with a
    #: clock send one, on a 404 too), the local clock on a local root. A lease is stamped and judged on this clock alone,
    #: so no two pods' clocks are ever compared.
    at: datetime


def _store_clock(response: Mapping[str, Any]) -> datetime:
    """The ``Date`` an S3 response carries; the local clock, logged, for a store that sends none."""
    header = response.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("date")
    if header:
        return parsedate_to_datetime(header).astimezone(UTC)
    log.warning("store_answered_no_date", extra={"reason": "a lease is timed on this pod's clock while the store sends no Date"})
    return datetime.now(UTC)


def read_json_at(root_uri: str, storage_options: StorageOptions, key: str) -> StoreRead:
    """The record at ``key`` with its ETag, or none, and the store's clock at the read: what a lease is timed by.

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
                return StoreRead(stored=None, at=_store_clock(exc.response))
            raise
        body = json.loads(got["Body"].read().decode("utf-8"))
        return StoreRead(stored=Stored(record=body, etag=str(got["ETag"]).strip('"')), at=_store_clock(got))

    fs, path = pafs.FileSystem.from_uri(root_uri)
    if not isinstance(fs, pafs.LocalFileSystem):
        raise NotImplementedError(f"conditional read is not supported for root {root_uri!r} ({type(fs).__name__})")
    target = os.path.join(path.rstrip("/"), key)
    try:
        with open(target, "rb") as fh:
            # Shared against the conditional replace's exclusive lock, which truncates before it writes.
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
            try:
                raw = fh.read()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except FileNotFoundError:
        return StoreRead(stored=None, at=datetime.now(UTC))
    return StoreRead(stored=Stored(record=json.loads(raw.decode("utf-8")), etag=hashlib.sha256(raw).hexdigest()), at=datetime.now(UTC))


def read_json(root_uri: str, storage_options: StorageOptions, key: str) -> tuple[dict[str, Any], str] | None:
    """The record at ``key`` and its ETag, or ``None`` when absent (:func:`read_json_at`, for a reader that needs no clock)."""
    stored = read_json_at(root_uri, storage_options, key).stored
    return None if stored is None else (stored.record, stored.etag)


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


def replace_json(root_uri: str, storage_options: StorageOptions, key: str, record: dict[str, Any], *, etag: str) -> None:
    """Overwrite ``key`` with ``record`` iff the record there is still the one read with ``etag``, once and never retried.

    For a writer that judged the record at an EARLIER read and must act on that judgement or not at all (a door
    acting on the claim it resolved when the request reached it, ADR 0173 §7): ``record`` is computed from the read
    whose ``etag`` it names, so a write landing since that read refuses this one rather than being re-judged. A
    writer that may re-judge the current record goes through :func:`mutate_json`.

    Raises:
        RecordChangedError: The record changed since that read.
    """
    _replace_json(root_uri, storage_options, key, record, etag=etag)


def _replace_json(root_uri: str, storage_options: StorageOptions, key: str, record: dict[str, Any], *, etag: str) -> None:
    """Overwrite ``key`` iff its current ETag is ``etag``. Raises :class:`RecordChangedError` if not.

    Private because the read and the write must not be separable by a caller: handing out a bare
    conditional put invites the read to drift from the write, which is the F4 shape all over again.
    Go through :func:`mutate_json`, or :func:`replace_json` with the ETag of the read the record was computed from.
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
            # Flushed under the lock: a buffered write otherwise reaches the file when the `with` closes it, after
            # the unlock, and a reader taking the shared lock in between reads the truncated, empty record (measured:
            # JSONDecodeError in 8 of 30 runs of eight concurrent renames handing one location's claim over, [[LH-519]]).
            fh.flush()
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
