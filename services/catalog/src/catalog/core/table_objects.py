"""A table's objects on the store: the key a location decodes to, the files a commit names, and a delete by key.

Every catalog read and write of a table's own objects resolves a location the way pylance opens it: ``describe_table``
answers a registered location percent-encoded, and the store keeps the key it decodes to once
(``lakehouse.lancekit.base_refs.decoded_path``). The doors that ask whether a table is only declared, verify the data
files a commit names, re-attach a table under its namespace root, or delete one object of a table's layout ask here.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any, Final

import pyarrow.fs as pafs
from lance_namespace import (
    InternalError,
    InvalidInputError,
)

from lakehouse.lancekit import manifest_names
from lakehouse.lancekit.base_refs import decoded_path, decoded_segments
from lakehouse.storage import stored_objects
from lakehouse.storage.options import StorageOptions, on_s3, s3_filesystem
from lakehouse.storage.uri import s3_object


__all__ = [
    "RESERVED_MARKER",
    "data_object_sizes",
    "dataset_fs",
    "delete_table_object",
    "discard_detached_manifest",
    "holds_a_version",
    "object_sizes",
    "object_store_path",
    "relative_location",
    "verify_fragment_data_files",
]

log = logging.getLogger(__name__)


#: The marker the `dir` catalog's DeclareTable writes at a table's location, so that a declared table's directory holds
#: an object before any data lands (`lance_docs/ns_catalog/namespace/supported-catalogs/lance-dir.md:143`).
RESERVED_MARKER: Final = ".lance-reserved"


def relative_location(location: str, *, root: str) -> str:
    """The key ``location`` decodes to under ``root``: what RegisterTable takes to re-attach the table there.

    Every re-registration makes this conversion, a rename's and both undrops'. The ``dir`` backend refuses an absolute
    location and keeps a relative one as the literal key it is given, while ``describe_table`` answers an absolute URL
    that percent-encodes a registered table's name (``…/r%C3%A4ksm%C3%B6rg%C3%A5s``). The store holds the table's bytes
    under the key that decodes to once (``…/räksmörgås``), so that key, whole, is what the table re-attaches at
    ([[LH-517]]). ``root`` is the root the namespace's connection is rooted at, its warehouse's
    ([[LH-528]]): a location under another root has no key the backend resolves to it.

    Raises:
        InternalError: ``location`` names no place under ``root``, which no door attaches a table at.
    """
    under, base = decoded_segments(location), decoded_segments(root)
    if under is None or base is None or len(under) <= len(base) or under[: len(base)] != base:
        raise InternalError(f"the location {location!r} names no place under its namespace root {root!r}, so the table cannot be re-attached there")
    return "/".join(under[len(base) :])


def dataset_fs(uri: str, so: StorageOptions) -> tuple[pafs.FileSystem, str]:
    """Resolve ``uri`` to a ``(filesystem, path)`` pair for a dataset relocation.

    A local ``file://`` / bare-path location (the ``dir`` backend default) needs no credentials; an
    ``s3://`` location builds an ``S3FileSystem`` from the SAME storage options the datasets are opened
    with (path-style, http-ok endpoint) — mirroring the media head's filesystem so RustFS works.

    The path is the key pylance opens: ``describe_table`` answers a registered location holding a non-ASCII character
    or a ``%`` percent-encoded (``…/r%C3%A4ksm%C3%B6rg%C3%A5s``), the store keeps the key it decodes to once
    (``base_refs.decoded_path``), and pyarrow's S3 filesystem takes a path literally ([[LH-520]]). A local URI pyarrow
    decodes itself.
    """
    if on_s3(uri):
        return s3_filesystem(so), decoded_path(uri)
    resolved, path = pafs.FileSystem.from_uri(uri)
    return resolved, path


def holds_a_version(location: str, so: StorageOptions) -> bool:
    """Does the table at ``location`` hold a committed version, as the store keeps it? A table that does not is only declared.

    The catalog's own judgment ([[LH-528]]), at the key the location decodes to (:func:`dataset_fs`), for every door that
    asks whether a table is only declared. The `dir` backend probes ``base_path.join(location)``, and object_store's
    ``Path::join`` appends one segment, percent-encoding ``/`` and every non-ASCII byte, so for a table registered at
    ``outside/held`` or ``räksmörgås`` it probes a key that does not exist and answers that the table has no storage
    (``rust/lance-namespace-impls/src/dir/manifest.rs:2272`` at lance v13.0.0; measured on pylance 13.0.0 on a local root
    and on S3). The backend also counts any object under ``_versions/`` as storage, so it answers that a declared table
    whose first commit was staged and never finalized has storage; only a committed manifest's name counts here.
    """
    fs, path = dataset_fs(location, so)
    listed = fs.get_file_info(pafs.FileSelector(f"{path.rstrip('/')}/_versions", allow_not_found=True))
    return any(info.type == pafs.FileType.File and manifest_names.committed_version(info.base_name) is not None for info in listed)


def verify_fragment_data_files(location: str, so: StorageOptions, fragments: list[dict[str, Any]]) -> None:
    """Reject a commit that references data files ABSENT under ``<location>/data/`` (audit HIGH fix).

    ``location`` is the ref's own directory (:func:`_ref_location`): a branch's appended files live under
    ``tree/<branch>/data/``.

    Each fragment's ``files[].path`` is a bare filename under the dataset's ``data/`` dir: a file naming a
    base was already refused by :func:`_refuse_based_data_files`. A missing file means the client's direct
    write targeted the wrong prefix — committing it would publish an unreadable version, so raise 400 and
    leave the table as-is. A declared ``file_size_bytes`` must be the object's size: Lance reads the footer
    at the declared size, so a larger one failed every read of the table (measured, [[LH-211]]). An absent
    size is legitimate, and Lance asks the store.
    """
    fs, base = dataset_fs(location, so)
    prefix = base.rstrip("/") + "/data/"
    # Collect every root-relative data file first, then resolve them in ONE batched lookup: pyarrow's
    # list overload runs the object-store lookups concurrently, so an N-fragment commit costs one round
    # trip instead of N serial HEADs on the commit hot path.
    declared = [
        (str(rel), data_file.get("file_size_bytes"))
        for frag in fragments
        for data_file in (frag.get("files") if isinstance(frag, dict) else None) or []
        if isinstance(data_file, dict) and (rel := data_file.get("path"))
    ]
    infos = fs.get_file_info([prefix + rel for rel, _ in declared]) if declared else []
    missing = [rel for (rel, _), info in zip(declared, infos, strict=True) if info.type == pafs.FileType.NotFound]
    if missing:
        raise InvalidInputError(
            f"commit references {len(missing)} data file(s) not present under the table location (did the direct write target the wrong prefix?): {missing[:5]}"
        )
    resized = [(rel, size, info.size) for (rel, size), info in zip(declared, infos, strict=True) if size is not None and size != info.size]
    if resized:
        raise InvalidInputError(
            f"commit refused: {len(resized)} data file(s) declare a file_size_bytes the object does not have, as (path, declared, actual): {resized[:5]}"
        )


def object_sizes(uris: Sequence[str], *, so: StorageOptions) -> list[int | None]:
    """Each object's size, ``None`` for one that is absent: one batched lookup per filesystem, as :func:`verify_fragment_data_files` does."""
    if uris and all(on_s3(uri) for uri in uris):
        infos = s3_filesystem(so).get_file_info([uri.removeprefix("s3://") for uri in uris])
    else:
        infos = [fs.get_file_info(path) for fs, path in (dataset_fs(uri, so) for uri in uris)]
    return _file_sizes(infos)


def _file_sizes(infos: Sequence[pafs.FileInfo]) -> list[int | None]:
    return [None if info.type != pafs.FileType.File else int(info.size) for info in infos]


def data_object_sizes(location: str, relative: Sequence[str], *, so: StorageOptions) -> list[int | None]:
    """Each object's size under ``<location>/data/``, ``None`` for one that is absent, in one batched lookup.

    ``location`` is the ref's own directory, resolved as :func:`verify_fragment_data_files` resolves it.
    """
    fs, base = dataset_fs(location, so)
    prefix = base.rstrip("/") + "/data/"
    return _file_sizes(fs.get_file_info([prefix + path for path in relative]))


def object_store_path(location: str) -> str:
    """``location`` as Lance's object store names it: no scheme, no bucket, no leading slash, decoded once as pylance opens it."""
    decoded = decoded_path(location)
    return decoded.partition("/")[2] if on_s3(location) else decoded


def discard_detached_manifest(location: str, so: StorageOptions, version: int) -> None:
    """Delete ``_versions/d<version>.manifest``, writing nothing else; the ``d`` prefix keeps the name apart from every
    manifest on the version line."""
    delete_table_object(location, so, f"_versions/{manifest_names.detached_name(version)}")


def delete_table_object(location: str, so: StorageOptions, relative: str) -> None:
    """Delete ``<location>/<relative>``, writing nothing else.

    It deletes by key through :func:`lakehouse.storage.stored_objects.delete_objects`, which records the delete ([[LH-232]]):
    pyarrow's ``S3FileSystem.delete_file`` PUTs the parent's directory marker after the delete, which left a
    ``_versions/`` object beside the table's manifests (measured on moto). A delete that fails leaves an inert object,
    never a wrong table, so it is logged rather than raised.
    """
    try:
        if on_s3(location):
            bucket, key = s3_object(location, relative)
            path = f"{bucket}/{key}"
        else:
            _, base = dataset_fs(location, so)
            path = f"{base.rstrip('/')}/{relative}"
        stored_objects.delete_objects(location, so, [path])
    except Exception:
        log.warning("catalog_table_object_left", extra={"location": location, "object": relative}, exc_info=True)
