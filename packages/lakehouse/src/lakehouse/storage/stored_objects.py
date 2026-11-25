"""The objects under one prefix, listed, read and deleted by key, the way the store keeps them.

On S3 the object client lists and deletes by key (``DeleteObjects``, a thousand keys a request, or one ``DeleteObject``
after a ``HeadObject``), because pyarrow's ``delete_file`` and ``delete_dir`` write a directory marker into the parent
(pyarrow 25.0.0: HEAD, DELETE, then a PUT of ``<parent>/``), which maintenance's own policy denies it under ``_bases/`` and
the other record prefixes ([[LH-535]]). A store that matches the Deny's ``*/_bases/*`` as AWS does refuses that PUT;
RustFS 1.0.3 does not match ``*`` against an empty suffix and admits the bare marker while refusing every record key
beneath it (measured 2026-10-28), so the refusal shows only on a conformant store. A local root is listed and deleted
through pyarrow's filesystem.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from typing import Any, Final

import pyarrow.fs as pafs
from pydantic import BaseModel, ConfigDict

from lakehouse.storage.client import MAX_POOL_CONNECTIONS, S3Client
from lakehouse.storage.conditional_records import store_clock
from lakehouse.storage.errors import ObjectNotFoundError, StoreRefusedError, StoreUnansweredError, store_call, unanswered_code
from lakehouse.storage.objectfs import fs_and_base
from lakehouse.storage.options import StorageOptions, object_client, on_s3
from lakehouse.storage.uri import s3_listing, s3_object


#: The most keys one S3 DeleteObjects request takes: what :func:`delete_objects` sends at a time, and what a caller
#: deleting a listing it does not hold whole hands it at a time.
DELETE_BATCH: Final = 1000

#: How many objects :func:`identities` asks the store about at once: as many as one client's pool keeps.
_IDENTITY_READS: Final = MAX_POOL_CONNECTIONS

#: How much of a local object :func:`identities` hashes per read.
_HASH_CHUNK: Final = 1 << 20

#: The logger every object :func:`delete_objects` removes is recorded on, one line per object ([[LH-232]]). Lance writes
#: its own deletes as ``lance::events::file_audit`` lines on stderr when ``LANCE_LOG`` asks for them; rask's deletes by
#: key are ones Lance never sees, so this records them in the same shape (``mode="delete" type=... path=...``), and one
#: query of the general log table for ``file_audit`` and ``mode="delete"`` reads every file a pass removed. Its ``path``
#: is the store path, ``<bucket>/<key>`` on S3, where Lance's names the key alone.
FILE_AUDIT_LOGGER: Final = "lakehouse.file_audit"
_file_audit = logging.getLogger(FILE_AUDIT_LOGGER)
log = logging.getLogger(__name__)

#: A Lance table's directories, each with the kind Lance's ``file_audit`` line names its files by.
_LAYOUT_KINDS: Final = (("_versions", "manifest"), ("_indices", "index"), ("_deletions", "deletion"), ("_transactions", "transaction"), ("data", "data"))


class StoredObject(BaseModel):
    """One object under a listed prefix."""

    model_config = ConfigDict(frozen=True)

    #: Its store path: ``<bucket>/<key>`` on S3, the absolute path on a local root.
    path: str
    size: int
    modified: datetime


def list_under(uri: str, storage_options: StorageOptions) -> list[StoredObject]:
    """Every object beneath ``uri``, at any depth; ``[]`` when nothing is there.

    Any ``s3://`` root is listed through the object client, endpoint or none, so the listing asks the store for
    ``<key>/`` alone, what a session scoped to the location may list, and for the whole bucket where the location is
    its root.

    Raises:
        StorageError: The store refused the listing, or did not answer it.
    """
    return list(iter_under(uri, storage_options))


def iter_under(uri: str, storage_options: StorageOptions) -> Iterator[StoredObject]:
    """:func:`list_under` a page at a time, a thousand objects a request on S3, so a caller that stops early has asked
    the store for no more than it read.

    Raises:
        StorageError: The store refused a page of the listing, or did not answer it.
    """
    if on_s3(uri):
        bucket, pages = _s3_pages(uri, storage_options)
        for page in pages:
            yield from _on_page(bucket, page)
        return
    fs, base = fs_and_base(uri, storage_options)
    for info in fs.get_file_info(pafs.FileSelector(base.rstrip("/"), recursive=True, allow_not_found=True)):
        if info.type == pafs.FileType.File and info.mtime is not None:
            yield StoredObject(path=info.path, size=int(info.size or 0), modified=info.mtime if info.mtime.tzinfo else info.mtime.replace(tzinfo=UTC))


class StoreListing(BaseModel):
    """The objects under one prefix, and the store's clock when it listed them."""

    model_config = ConfigDict(frozen=True)

    objects: list[StoredObject]
    #: The store's clock when it answered the listing's first page (:func:`lakehouse.storage.conditional_records.store_clock`), the local
    #: clock on a local root, read before the listing: an object's age is this, less the stamp the same store put on it.
    at: datetime


def list_under_at(uri: str, storage_options: StorageOptions) -> StoreListing:
    """:func:`list_under`, with the store's clock at the listing, so an object's age is read off the one clock that
    stamped it and never off a pod's (ADR 0179).

    Raises:
        StorageError: The store refused a page of the listing, or did not answer it.
    """
    if not on_s3(uri):
        at = datetime.now(UTC)
        return StoreListing(objects=list_under(uri, storage_options), at=at)
    bucket, pages = _s3_pages(uri, storage_options)
    # A paginator always answers a first page, an empty listing's included.
    first = next(pages)
    return StoreListing(objects=[*_on_page(bucket, first), *(*_on_page(bucket, page) for page in pages)], at=store_clock(first))


def _s3_pages(uri: str, storage_options: StorageOptions) -> tuple[str, Iterator[Mapping[str, Any]]]:
    """The bucket ``uri`` names, and the pages of a listing of everything beneath its key, each request mapped by
    :func:`store_call`."""
    bucket, under = s3_listing(uri)
    doing = f"listing {uri}"
    with store_call(doing):
        pages = iter(object_client(storage_options).get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=under))
    return bucket, _answered_pages(doing, pages)


def _answered_pages(doing: str, pages: Iterator[Mapping[str, Any]]) -> Iterator[Mapping[str, Any]]:
    """Each of ``pages``, the request the paginator makes for each one under :func:`store_call`."""
    while True:
        with store_call(doing):
            page = next(pages, None)
        if page is None:
            return
        yield page


def _on_page(bucket: str, page: Mapping[str, Any]) -> Iterator[StoredObject]:
    for entry in page.get("Contents", []):
        yield StoredObject(path=f"{bucket}/{entry['Key']}", size=int(entry.get("Size", 0)), modified=entry["LastModified"])


class ObjectIdentity(BaseModel):
    """One object as the store holds it now: its ETag and its size."""

    model_config = ConfigDict(frozen=True)

    #: The store's ETag on S3, unquoted; on a local root the SHA-256 of the bytes, which means the same thing, "these
    #: bytes", as a local record's ETag does in :mod:`lakehouse.storage.conditional_records`.
    etag: str
    size: int


def identities(uri: str, storage_options: StorageOptions, keys: Sequence[str]) -> dict[str, ObjectIdentity]:
    """Each object at ``<uri>/<key>`` the store holds, by key, as it holds it now; an absent one is left out.

    ``uri`` is the store's own spelling of a root (``s3://<bucket>/<key>`` or a local path), never decoded here. An S3
    object is read with one HEAD, sixteen at a time; a local one is hashed.

    Raises:
        StorageError: The store refused a read, or did not answer it.
    """
    if not keys:
        return {}
    read = _s3_identity(uri, storage_options) if on_s3(uri) else _local_identity(uri, storage_options)
    with ThreadPoolExecutor(max_workers=min(_IDENTITY_READS, len(keys))) as pool:
        found = list(pool.map(read, keys))
    return {key: identity for key, identity in zip(keys, found, strict=True) if identity is not None}


def listed_identities(uri: str, storage_options: StorageOptions, keys: Sequence[str]) -> dict[str, ObjectIdentity]:
    """Each object at ``<uri>/<key>`` the store holds, as :func:`identities` answers it, read off one listing of ``uri``.

    An S3 ``ListObjectsV2`` page names a thousand objects with the same ETag and size a HEAD answers, so asking about every
    file a table's commits recorded costs a request per thousand objects under its root rather than one per file. A local
    root is hashed key by key, as :func:`identities` does.

    Raises:
        StorageError: The store refused the listing, or did not answer it.
    """
    if not keys:
        return {}
    if not on_s3(uri):
        return identities(uri, storage_options, keys)
    bucket, under = s3_listing(uri)
    wanted = set(keys)
    found: dict[str, ObjectIdentity] = {}
    with store_call(f"listing {bucket}/{under}"):
        for page in object_client(storage_options).get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=under):
            found.update(_listed_on_page(page, under=under, wanted=wanted))
    return found


def _listed_on_page(page: Mapping[str, Any], *, under: str, wanted: set[str]) -> dict[str, ObjectIdentity]:
    """Each object a ``ListObjectsV2`` ``page`` names whose key under ``under`` is one of ``wanted``, by that key."""
    found: dict[str, ObjectIdentity] = {}
    for entry in page.get("Contents", []):
        if (key := str(entry["Key"]).removeprefix(under)) in wanted:
            found[key] = ObjectIdentity(etag=str(entry["ETag"]).strip('"'), size=int(entry.get("Size", 0)))
    return found


def _s3_identity(uri: str, storage_options: StorageOptions) -> Callable[[str], ObjectIdentity | None]:
    client = object_client(storage_options)

    def read(key: str) -> ObjectIdentity | None:
        bucket, full_key = s3_object(uri, key)
        try:
            with store_call(f"reading {bucket}/{full_key}"):
                head = client.head_object(Bucket=bucket, Key=full_key)
        except ObjectNotFoundError:
            return None
        return ObjectIdentity(etag=str(head["ETag"]).strip('"'), size=int(head["ContentLength"]))

    return read


def _local_identity(uri: str, storage_options: StorageOptions) -> Callable[[str], ObjectIdentity | None]:
    fs, base = fs_and_base(uri, storage_options)

    def read(key: str) -> ObjectIdentity | None:
        digest, size = hashlib.sha256(), 0
        try:
            with fs.open_input_stream(f"{base}/{key}") as stream:
                while chunk := stream.read(_HASH_CHUNK):
                    digest.update(chunk)
                    size += len(chunk)
        except FileNotFoundError:
            return None
        return ObjectIdentity(etag=digest.hexdigest(), size=size)

    return read


def delete_object(uri: str, storage_options: StorageOptions, key: str) -> bool:
    """Delete the object at ``<uri>/<key>``; ``False`` when there was none.

    Any ``s3://`` root takes the object client, endpoint or none: a vend naming no endpoint is AWS proper, the store whose
    policy refuses the marker, and a missing endpoint resolves as ``records``' conditional writes resolve it.

    Raises:
        StorageError: The store refused the read or the delete, or did not answer it.
    """
    if not on_s3(uri):
        fs, base = fs_and_base(uri, storage_options)
        try:
            fs.delete_file(f"{base}/{key}")
        except FileNotFoundError:
            return False
        return True
    bucket, full_key = s3_object(uri, key)
    client = object_client(storage_options)
    try:
        with store_call(f"deleting {bucket}/{full_key}"):
            client.head_object(Bucket=bucket, Key=full_key)
            client.delete_object(Bucket=bucket, Key=full_key)
    except ObjectNotFoundError:
        return False
    return True


def remove_object(uri: str, storage_options: StorageOptions, key: str) -> None:
    """Delete the object at ``<uri>/<key>`` in one request, whether or not it is there: S3 answers a DELETE of an absent key
    as one of a present one, so a caller that needs no answer to "was it there" spends no read on it (:func:`delete_object`
    spends a HEAD).

    Raises:
        StorageError: The store refused the delete, or did not answer it.
    """
    if not on_s3(uri):
        fs, base = fs_and_base(uri, storage_options)
        try:
            fs.delete_file(f"{base}/{key}")
        except FileNotFoundError:
            return
        return
    bucket, full_key = s3_object(uri, key)
    with store_call(f"deleting {bucket}/{full_key}"):
        object_client(storage_options).delete_object(Bucket=bucket, Key=full_key)


def _file_kind(path: str) -> str:
    """What a store path is in a Lance table's layout, as Lance's own ``file_audit`` line names it: ``manifest``, ``data``,
    ``index``, ``deletion`` or ``transaction`` by the directory it lies in (``lance_docs/lance/format/table/layout.md``),
    and ``object`` for anything else (a blob sidecar under ``data/`` is ``data``)."""
    directories = path.split("/")[:-1]
    for directory, kind in _LAYOUT_KINDS:
        if directory in directories:
            return kind
    return "object"


def delete_objects(uri: str, storage_options: StorageOptions, paths: list[str]) -> None:
    """Delete the store ``paths``, every one beneath ``uri``'s bucket or root, by key, and record each one deleted.

    Any ``s3://`` root takes the object client, endpoint or none: pyarrow's delete puts the deleted object's parent back as
    a directory marker (measured on pyarrow 25.0.0), a write outside what a session scoped to the location may make. A path
    already absent is deleted, as S3 answers it. Each path the store deleted is written on :data:`FILE_AUDIT_LOGGER` as it
    goes, so a delete that stops part-way records what it removed; one the store refused is not. An absent path is no
    error: S3's DeleteObjects answers it as deleted, so it is recorded there, while a local root records only a path it
    found. The paths may lie in several buckets; each bucket's are sent in requests of their own.

    Raises:
        StorageError: The store refused a delete, or did not answer it.
    """
    if not paths:
        return
    if not on_s3(uri):
        _delete_local(uri, storage_options, paths)
        return
    client = object_client(storage_options)
    by_bucket: dict[str, list[str]] = {}
    for path in paths:
        bucket, _, key = path.partition("/")
        by_bucket.setdefault(bucket, []).append(key)
    for bucket, keys in by_bucket.items():
        for start in range(0, len(keys), DELETE_BATCH):
            _delete_batch(client, bucket, keys[start : start + DELETE_BATCH])


def _delete_local(uri: str, storage_options: StorageOptions, paths: list[str]) -> None:
    """Delete each local path that is there, recording each one deleted."""
    fs, _ = fs_and_base(uri, storage_options)
    for path in paths:
        with suppress(FileNotFoundError):
            fs.delete_file(path)
            _record_deleted(path)


def _delete_batch(client: S3Client, bucket: str, keys: list[str]) -> None:
    """One DeleteObjects request for ``keys`` in ``bucket``, recording each key the store deleted.

    A key the store answered with a code saying it did not get to it (:func:`lakehouse.storage.errors.unanswered_code`)
    is an outage a retry may clear; any other per-key error is its refusal. The keys it did delete are recorded first.

    Raises:
        StorageError: The store failed the request as a whole, or a key of it: ``StoreUnansweredError`` where it did
            not get to one, ``StoreRefusedError`` where it refused one and got to the rest.
    """
    with store_call(f"deleting {len(keys)} key(s) in {bucket}"):
        answered = client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True})
    failed = answered.get("Errors") or []
    failed_keys = {str(error.get("Key")) for error in failed}
    for key in keys:
        if key not in failed_keys:
            _record_deleted(f"{bucket}/{key}")
    if not failed:
        return
    message = f"deleting {len(keys)} key(s) in {bucket}: the store failed {len(failed)} of them: {failed[:3]}"
    if any(unanswered_code(str(error.get("Code"))) for error in failed):
        raise StoreUnansweredError(message, response=answered)
    raise StoreRefusedError(message, response=answered)


@contextmanager
def recording_removed_transactions(ref_root: str, storage_options: StorageOptions) -> Generator[None]:
    """Record each ``_transactions/`` file a Lance cleanup of the ref at ``ref_root`` removes, one line per file.

    Lance's ``file_audit`` lines name every data, deletion and index file and every manifest its cleanup deletes, and no
    transaction file (measured on pylance 13.0.0: a cleanup that removed 5 transaction files wrote 0 lines for them, its
    other 12 deletes one line each), and nothing left once they are gone names them. So the ref's ``_transactions/`` is
    listed before the cleanup and again after it, part-way or not, and each file gone is recorded as rask's own deletes
    are. A file gone that the cleanup did not remove (another reclaim of the same ref meanwhile) is a removed file too.

    Raises:
        StorageError: The store refused the listing before the cleanup, or did not answer it.
    """
    directory = f"{ref_root.rstrip('/')}/_transactions"
    before = _paths_under(directory, storage_options)
    try:
        yield
    finally:
        _record_gone(directory, storage_options, before)


def _record_gone(directory: str, storage_options: StorageOptions, before: set[str]) -> None:
    """Record each of ``before`` no longer under ``directory``; a listing that fails records nothing and says so."""
    try:
        after = _paths_under(directory, storage_options)
    except OSError:
        log.exception("file_audit_transactions_unrecorded", extra={"directory": directory, "listed_before": len(before)})
        return
    for path in sorted(before - after):
        _record_deleted(path)


def _paths_under(uri: str, storage_options: StorageOptions) -> set[str]:
    """The store path of every object beneath ``uri``.

    Raises:
        StorageError: The store refused the listing, or did not answer it.
    """
    return {stored.path for stored in iter_under(uri, storage_options)}


def _record_deleted(path: str) -> None:
    """One line on :data:`FILE_AUDIT_LOGGER` for a store path rask deleted, in the shape of Lance's own delete line."""
    kind = _file_kind(path)
    _file_audit.info(
        f'target="{FILE_AUDIT_LOGGER}" mode="delete" type="{kind}" path="{path}"',
        extra={"file_audit.mode": "delete", "file_audit.type": kind, "file_audit.path": path},
    )


__all__ = [
    "DELETE_BATCH",
    "FILE_AUDIT_LOGGER",
    "ObjectIdentity",
    "StoreListing",
    "StoredObject",
    "delete_object",
    "delete_objects",
    "identities",
    "iter_under",
    "list_under",
    "list_under_at",
    "listed_identities",
    "recording_removed_transactions",
    "remove_object",
]
