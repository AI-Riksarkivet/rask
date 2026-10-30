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
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, Final

import pyarrow.fs as pafs
from pydantic import BaseModel, ConfigDict

from service_kit.lakehouse.objectfs import credential_of, fs_and_base
from storage import s3_client


#: The most keys one S3 DeleteObjects request takes.
_DELETE_BATCH: Final = 1000

#: How many objects :func:`identities` asks the store about at once.
_IDENTITY_READS: Final = 16

#: How much of a local object :func:`identities` hashes per read.
_HASH_CHUNK: Final = 1 << 20


class StoredObject(BaseModel):
    """One object under a listed prefix."""

    model_config = ConfigDict(frozen=True)

    #: Its store path: ``<bucket>/<key>`` on S3, the absolute path on a local root.
    path: str
    size: int
    modified: datetime


def _on_object_client(uri: str, storage_options: dict[str, str]) -> bool:
    return uri.startswith("s3://") and bool(storage_options.get("endpoint"))


def _object_client(storage_options: dict[str, str]) -> Any:  # noqa: ANN401 — a boto3 S3 client
    access_key, secret_key, session_token = credential_of(storage_options)
    return s3_client(
        storage_options.get("endpoint"), access_key=access_key, secret_key=secret_key, session_token=session_token, region=storage_options.get("region")
    )


def list_under(uri: str, storage_options: dict[str, str]) -> list[StoredObject]:
    """Every object beneath ``uri``, at any depth; ``[]`` when nothing is there."""
    if _on_object_client(uri, storage_options):
        bucket, _, key = uri.removeprefix("s3://").partition("/")
        pages = _object_client(storage_options).get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{key.rstrip('/')}/")
        return [
            StoredObject(path=f"{bucket}/{entry['Key']}", size=int(entry.get("Size", 0)), modified=entry["LastModified"])
            for page in pages
            for entry in page.get("Contents", [])
        ]
    fs, base = fs_and_base(uri, storage_options)
    infos = fs.get_file_info(pafs.FileSelector(base.rstrip("/"), recursive=True, allow_not_found=True))
    return [
        StoredObject(path=info.path, size=int(info.size or 0), modified=info.mtime if info.mtime.tzinfo else info.mtime.replace(tzinfo=UTC))
        for info in infos
        if info.type == pafs.FileType.File and info.mtime is not None
    ]


class ObjectIdentity(BaseModel):
    """One object as the store holds it now: its ETag and its size."""

    model_config = ConfigDict(frozen=True)

    #: The store's ETag on S3, unquoted; on a local root the SHA-256 of the bytes, which means the same thing, "these
    #: bytes", as a local record's ETag does in :mod:`service_kit.lakehouse.records`.
    etag: str
    size: int


def identities(uri: str, storage_options: dict[str, str], keys: Sequence[str]) -> dict[str, ObjectIdentity]:
    """Each object at ``<uri>/<key>`` the store holds, by key, as it holds it now; an absent one is left out.

    ``uri`` is the store's own spelling of a root (``s3://<bucket>/<key>`` or a local path), never decoded here. An S3
    object is read with one HEAD, sixteen at a time; a local one is hashed.

    Raises:
        OSError: The store refused, or could not be reached for, a read.
    """
    if not keys:
        return {}
    read = _s3_identity(uri, storage_options) if uri.startswith("s3://") else _local_identity(uri, storage_options)
    with ThreadPoolExecutor(max_workers=min(_IDENTITY_READS, len(keys))) as pool:
        found = list(pool.map(read, keys))
    return {key: identity for key, identity in zip(keys, found, strict=True) if identity is not None}


def _s3_identity(uri: str, storage_options: dict[str, str]) -> Callable[[str], ObjectIdentity | None]:
    from botocore.exceptions import BotoCoreError, ClientError

    bucket, _, prefix = uri.removeprefix("s3://").rstrip("/").partition("/")
    client = _object_client(storage_options)

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


def _local_identity(uri: str, storage_options: dict[str, str]) -> Callable[[str], ObjectIdentity | None]:
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


def delete_object(uri: str, storage_options: dict[str, str], key: str) -> bool:
    """Delete the object at ``<uri>/<key>``; ``False`` when there was none.

    Any ``s3://`` root takes the object client, endpoint or none: a vend naming no endpoint is AWS proper, the store whose
    policy refuses the marker, and a missing endpoint resolves as ``records``' conditional writes resolve it.

    Raises:
        OSError: The store refused, or could not be reached for, the read or the delete.
    """
    if not uri.startswith("s3://"):
        fs, base = fs_and_base(uri, storage_options)
        try:
            fs.delete_file(f"{base}/{key}")
        except FileNotFoundError:
            return False
        return True
    from botocore.exceptions import BotoCoreError, ClientError

    bucket, _, prefix = uri.removeprefix("s3://").rstrip("/").partition("/")
    full_key = f"{prefix}/{key}" if prefix else key
    client = _object_client(storage_options)
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


def delete_objects(uri: str, storage_options: dict[str, str], paths: list[str]) -> None:
    """Delete the store ``paths``, every one beneath ``uri``'s bucket or root.

    Raises:
        OSError: The store refused a delete.
    """
    if not paths:
        return
    if not _on_object_client(uri, storage_options):
        fs, _ = fs_and_base(uri, storage_options)
        for path in paths:
            fs.delete_file(path)
        return
    client = _object_client(storage_options)
    bucket = paths[0].partition("/")[0]
    keys = [path.partition("/")[2] for path in paths]
    for start in range(0, len(keys), _DELETE_BATCH):
        answered = client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": key} for key in keys[start : start + _DELETE_BATCH]], "Quiet": True})
        if refused := answered.get("Errors"):
            raise OSError(f"the store refused {len(refused)} delete(s) in {bucket}: {refused[:3]}")
