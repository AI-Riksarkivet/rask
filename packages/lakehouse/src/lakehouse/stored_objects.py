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

from lakehouse.objectfs import fs_and_base
from lakehouse.records import store_clock
from lakehouse.storage.client import MAX_POOL_CONNECTIONS
from lakehouse.storage_options import StorageOptions, object_client, on_s3


#: The most keys one S3 DeleteObjects request takes.
_DELETE_BATCH: Final = 1000

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
    ``<key>/`` alone, what a session scoped to the location may list.
    """
    return list(iter_under(uri, storage_options))


def iter_under(uri: str, storage_options: StorageOptions) -> Iterator[StoredObject]:
    """:func:`list_under` a page at a time, a thousand objects a request on S3, so a caller that stops early has asked
    the store for no more than it read."""
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
    #: The store's clock when it answered the listing's first page (:func:`lakehouse.records.store_clock`), the local
    #: clock on a local root, read before the listing: an object's age is this, less the stamp the same store put on it.
    at: datetime


def list_under_at(uri: str, storage_options: StorageOptions) -> StoreListing:
    """:func:`list_under`, with the store's clock at the listing, so an object's age is read off the one clock that
    stamped it and never off a pod's (ADR 0179)."""
    if not on_s3(uri):
        at = datetime.now(UTC)
        return StoreListing(objects=list_under(uri, storage_options), at=at)
    bucket, pages = _s3_pages(uri, storage_options)
    # A paginator always answers a first page, an empty listing's included.
    first = next(pages)
    return StoreListing(objects=[*_on_page(bucket, first), *(listed for page in pages for listed in _on_page(bucket, page))], at=store_clock(first))


def _s3_pages(uri: str, storage_options: StorageOptions) -> tuple[str, Iterator[Mapping[str, Any]]]:
    """The bucket ``uri`` names, and the pages of a listing of everything beneath its key."""
    bucket, _, key = uri.removeprefix("s3://").partition("/")
    return bucket, iter(object_client(storage_options).get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{key.rstrip('/')}/"))


def _on_page(bucket: str, page: Mapping[str, Any]) -> Iterator[StoredObject]:
    for entry in page.get("Contents", []):
        yield StoredObject(path=f"{bucket}/{entry['Key']}", size=int(entry.get("Size", 0)), modified=entry["LastModified"])


class ObjectIdentity(BaseModel):
    """One object as the store holds it now: its ETag and its size."""

    model_config = ConfigDict(frozen=True)

    #: The store's ETag on S3, unquoted; on a local root the SHA-256 of the bytes, which means the same thing, "these
    #: bytes", as a local record's ETag does in :mod:`lakehouse.records`.
    etag: str
    size: int


def identities(uri: str, storage_options: StorageOptions, keys: Sequence[str]) -> dict[str, ObjectIdentity]:
    """Each object at ``<uri>/<key>`` the store holds, by key, as it holds it now; an absent one is left out.

    ``uri`` is the store's own spelling of a root (``s3://<bucket>/<key>`` or a local path), never decoded here. An S3
    object is read with one HEAD, sixteen at a time; a local one is hashed.

    Raises:
        OSError: The store refused, or could not be reached for, a read.
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
        OSError: The store refused, or could not be reached for, the listing.
    """
    if not keys:
        return {}
    if not on_s3(uri):
        return identities(uri, storage_options, keys)
    from botocore.exceptions import BotoCoreError, ClientError

    bucket, _, prefix = uri.removeprefix("s3://").rstrip("/").partition("/")
    under, wanted = f"{prefix}/" if prefix else "", set(keys)
    found: dict[str, ObjectIdentity] = {}
    try:
        for page in object_client(storage_options).get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=under):
            for entry in page.get("Contents", []):
                if (key := str(entry["Key"]).removeprefix(under)) in wanted:
                    found[key] = ObjectIdentity(etag=str(entry["ETag"]).strip('"'), size=int(entry.get("Size", 0)))
    except ClientError as exc:
        raise OSError(f"the store refused listing {bucket}/{under}: {exc}") from exc
    except BotoCoreError as exc:
        raise OSError(f"the store could not be reached listing {bucket}/{under}: {exc}") from exc
    return found


def _s3_identity(uri: str, storage_options: StorageOptions) -> Callable[[str], ObjectIdentity | None]:
    from botocore.exceptions import BotoCoreError, ClientError

    bucket, _, prefix = uri.removeprefix("s3://").rstrip("/").partition("/")
    client = object_client(storage_options)

    def read(key: str) -> ObjectIdentity | None:
        full_key = f"{prefix}/{key}" if prefix else key
        try:
            head = client.head_object(Bucket=bucket, Key=full_key)
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return None
            raise OSError(f"the store refused reading {bucket}/{full_key}: {exc}") from exc
        except BotoCoreError as exc:
            raise OSError(f"the store could not be reached reading {bucket}/{full_key}: {exc}") from exc
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
        OSError: The store refused, or could not be reached for, the read or the delete.
    """
    if not on_s3(uri):
        fs, base = fs_and_base(uri, storage_options)
        try:
            fs.delete_file(f"{base}/{key}")
        except FileNotFoundError:
            return False
        return True
    from botocore.exceptions import BotoCoreError, ClientError

    bucket, _, prefix = uri.removeprefix("s3://").rstrip("/").partition("/")
    full_key = f"{prefix}/{key}" if prefix else key
    client = object_client(storage_options)
    try:
        client.head_object(Bucket=bucket, Key=full_key)
        client.delete_object(Bucket=bucket, Key=full_key)
    except ClientError as exc:
        if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
            return False
        raise OSError(f"the store refused deleting {bucket}/{full_key}: {exc}") from exc
    except BotoCoreError as exc:
        raise OSError(f"the store could not be reached deleting {bucket}/{full_key}: {exc}") from exc
    return True


def remove_object(uri: str, storage_options: StorageOptions, key: str) -> None:
    """Delete the object at ``<uri>/<key>`` in one request, whether or not it is there: S3 answers a DELETE of an absent key
    as one of a present one, so a caller that needs no answer to "was it there" spends no read on it (:func:`delete_object`
    spends a HEAD).

    Raises:
        OSError: The store refused the delete, or could not be reached.
    """
    if not on_s3(uri):
        fs, base = fs_and_base(uri, storage_options)
        try:
            fs.delete_file(f"{base}/{key}")
        except FileNotFoundError:
            return
        return
    from botocore.exceptions import BotoCoreError, ClientError

    bucket, _, prefix = uri.removeprefix("s3://").rstrip("/").partition("/")
    full_key = f"{prefix}/{key}" if prefix else key
    try:
        object_client(storage_options).delete_object(Bucket=bucket, Key=full_key)
    except ClientError as exc:
        raise OSError(f"the store refused deleting {bucket}/{full_key}: {exc}") from exc
    except BotoCoreError as exc:
        raise OSError(f"the store could not be reached deleting {bucket}/{full_key}: {exc}") from exc


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
        OSError: The store refused a delete.
    """
    if not paths:
        return
    if not on_s3(uri):
        fs, _ = fs_and_base(uri, storage_options)
        for path in paths:
            with suppress(FileNotFoundError):
                fs.delete_file(path)
                _record_deleted(path)
        return
    client = object_client(storage_options)
    by_bucket: dict[str, list[str]] = {}
    for path in paths:
        bucket, _, key = path.partition("/")
        by_bucket.setdefault(bucket, []).append(key)
    for bucket, keys in by_bucket.items():
        for start in range(0, len(keys), _DELETE_BATCH):
            batch = keys[start : start + _DELETE_BATCH]
            answered = client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True})
            refused = answered.get("Errors") or []
            refused_keys = {str(error.get("Key")) for error in refused}
            for key in batch:
                if key not in refused_keys:
                    _record_deleted(f"{bucket}/{key}")
            if refused:
                raise OSError(f"the store refused {len(refused)} delete(s) in {bucket}: {refused[:3]}")


@contextmanager
def recording_removed_transactions(ref_root: str, storage_options: StorageOptions) -> Generator[None]:
    """Record each ``_transactions/`` file a Lance cleanup of the ref at ``ref_root`` removes, one line per file.

    Lance's ``file_audit`` lines name every data, deletion and index file and every manifest its cleanup deletes, and no
    transaction file (measured on pylance 13.0.0: a cleanup that removed 5 transaction files wrote 0 lines for them, its
    other 12 deletes one line each), and nothing left once they are gone names them. So the ref's ``_transactions/`` is
    listed before the cleanup and again after it, part-way or not, and each file gone is recorded as rask's own deletes
    are. A file gone that the cleanup did not remove (another reclaim of the same ref meanwhile) is a removed file too.

    Raises:
        OSError: The store refused, or could not be reached for, the listing before the cleanup.
    """
    directory = f"{ref_root.rstrip('/')}/_transactions"
    before = _paths_under(directory, storage_options)
    try:
        yield
    finally:
        try:
            gone = before - _paths_under(directory, storage_options)
        except OSError:
            log.exception("file_audit_transactions_unrecorded", extra={"directory": directory, "listed_before": len(before)})
        else:
            for path in sorted(gone):
                _record_deleted(path)


def _paths_under(uri: str, storage_options: StorageOptions) -> set[str]:
    """The store path of every object beneath ``uri``.

    Raises:
        OSError: The store refused the listing, or could not be reached.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    try:
        return {stored.path for stored in iter_under(uri, storage_options)}
    except (ClientError, BotoCoreError) as exc:
        raise OSError(f"the store refused listing {uri}: {exc}") from exc


def _record_deleted(path: str) -> None:
    """One line on :data:`FILE_AUDIT_LOGGER` for a store path rask deleted, in the shape of Lance's own delete line."""
    kind = _file_kind(path)
    _file_audit.info(
        f'target="{FILE_AUDIT_LOGGER}" mode="delete" type="{kind}" path="{path}"',
        extra={"file_audit.mode": "delete", "file_audit.type": kind, "file_audit.path": path},
    )
