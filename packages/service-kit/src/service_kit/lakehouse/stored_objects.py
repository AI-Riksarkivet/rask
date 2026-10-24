"""The objects under one prefix, listed and deleted by key, the way the store keeps them.

On S3 the object client lists and deletes by key (``DeleteObjects``, a thousand keys a request), because pyarrow's
``delete_file`` and ``delete_dir`` write a directory marker into the emptied parent, which maintenance's own policy may
deny it ([[LH-535]]). A local root is listed and deleted through pyarrow's filesystem.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import pyarrow.fs as pafs
from pydantic import BaseModel, ConfigDict

from service_kit.lakehouse.objectfs import credential_of, fs_and_base
from storage import s3_client


#: The most keys one S3 DeleteObjects request takes.
_DELETE_BATCH: Final = 1000


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
        storage_options["endpoint"], access_key=access_key, secret_key=secret_key, session_token=session_token, region=storage_options.get("region")
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
