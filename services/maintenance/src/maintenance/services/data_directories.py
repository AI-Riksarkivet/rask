"""A table's own directories on its data bases: what no version references is reclaimed, what a deleted row keeps is reported ([[LH-286]]).

LANCE'S OWN CLEANUP IS ROOT-SCOPED. Measured on pylance 13.0.0: ``cleanup_old_versions(older_than=0,
delete_unverified=True)`` after an overwrite of a table whose fragments sit on a non-root data base removed every
superseded version and none of the base's files. A data base is still the table's own storage: each table registers its
own directory beneath an approved base ([[LH-252]]), its base record names that directory, and the directory's claim
keeps every other table out of it. So the pass reclaims that directory as Lance reclaims ``data/``: a file no retained
version references, once it is older than Lance's unverified-file age (``lance_docs/lance_sdk.md:931``: files "newer
than 7 days old are not deleted by default"), since a younger one may belong to a commit still in flight.

WHAT A VERSION REFERENCES IS READ OFF EVERY RETAINED MANIFEST, branches included. A branch inherits the data base under
the same id and appends into the same directory, and ``versions()`` on a branch handle lists the branch's own versions,
its fork version included (both measured on pylance 13.0.0), so main's history alone is not the referenced set. A tag
pins a version of main or of a branch, which cleanup retains, so it is in one of those lists.

ONLY A DIRECTORY THE RECORD NAMES AS THE TABLE'S OWN, AND WHOSE CLAIM THE TABLE HOLDS, is listed: a ``data`` entry, a
plain base. A dataset-root base is another dataset's (a clone's source, a branch's parent) and is never listed here, and
the claim is what makes a directory one table's ([[LH-252]]): a record naming a directory whose claim the table does not
hold, such as an approved base itself, which every table naming it once shared, is never listed either.

A DELETED ROW IS REPORTED, NOT RECLAIMED (owner, 2026-10-24). Its fragment stays referenced behind a deletion vector and
only a compaction rewrites it; pylance 13.0.0's ``CompactionOptions`` carries no ``target_bases``, so that rewrite would
move the table's rows off the base into its root.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta
from typing import Final

import lance
from pydantic import BaseModel, ConfigDict

from maintenance.services.stored_objects import StoredObject, delete_objects, list_under
from service_kit.lakehouse import location_claims
from service_kit.lakehouse.base_refs import decoded_path, store_of
from service_kit.lakehouse.base_registry import BaseRecord, BaseRole, RecordedBase


log = logging.getLogger(__name__)

#: Lance's unverified-file age: a file younger than this may belong to a commit that has not landed yet.
UNVERIFIED_AGE: Final = timedelta(days=7)

#: A file a version references, as the directory it lives in and its name there.
type ReferencedFile = tuple[str, str]


class Reclaimed(BaseModel):
    """What one reclaim of a table's data directories deleted."""

    model_config = ConfigDict(frozen=True)

    files: int = 0
    bytes: int = 0


def recorded_directories(record: BaseRecord | None) -> list[RecordedBase]:
    """The table's own data directories, as its base record names them: ``data`` entries on a plain base."""
    if record is None:
        return []
    return [entry for entry in record.entries if entry.role is BaseRole.DATA and not entry.is_dataset_root]


def claimed_by(claims: location_claims.ClaimStore, directory: RecordedBase, table: str) -> bool:
    """Whether ``table`` holds ``directory``'s claim, the evidence that its bytes are that table's alone ([[LH-252]]).

    Raises:
        ValueError: The stored claim is not a claim.
        botocore.exceptions.ClientError: The store refused the read.
    """
    held = location_claims.holder(claims, directory.path)
    return held is not None and held.table == table


def directory_uri(directory: RecordedBase) -> str:
    """Where ``directory`` is listed: its path in the store the record names."""
    return f"/{directory.path}" if directory.store == "file" else f"{directory.store}://{directory.path}"


def _listed_prefix(directory: RecordedBase) -> str:
    """The prefix every object :func:`list_under` finds in ``directory`` carries: its store path and a separator."""
    return f"{directory_uri(directory).split('://', 1)[-1].rstrip('/')}/"


def _declared_in(directory: RecordedBase, base_path: str) -> bool:
    return store_of(base_path) == directory.store and decoded_path(base_path) == decoded_path(directory.path)


def _owned_bases(ds: lance.LanceDataset, directories: Sequence[RecordedBase]) -> dict[int, str]:
    """The id of each base ``ds``'s manifest declares at one of ``directories``, mapped to that directory's path."""
    owned: dict[int, str] = {}
    for base_id, base in ds.base_paths().items():
        if (directory := next((d for d in directories if _declared_in(d, str(base.path))), None)) is not None:
            owned[int(base_id)] = directory.path
    return owned


def _referenced_by(ds: lance.LanceDataset, directories: Sequence[RecordedBase]) -> set[ReferencedFile]:
    """Every file ``ds``'s fragments hold in ``directories``."""
    owned = _owned_bases(ds, directories)
    referenced: set[ReferencedFile] = set()
    for fragment in ds.get_fragments() if owned else []:
        for data_file in fragment.data_files():
            if data_file.base_id in owned:
                referenced.add((owned[data_file.base_id], data_file.path))
        deletion = fragment.metadata.deletion_file
        if deletion is not None and deletion.base_id in owned:
            referenced.add((owned[deletion.base_id], deletion.path(fragment.fragment_id)))
    return referenced


def _heads(ds: lance.LanceDataset) -> Iterator[lance.LanceDataset]:
    """Main's current version and every branch's head."""
    yield ds
    for name in ds.branches.list():
        yield ds.checkout_version((name, None))


def _every_retained_version(ds: lance.LanceDataset) -> Iterator[lance.LanceDataset]:
    """Every version of main and of every branch the store still keeps a manifest for."""
    for version in ds.versions():
        yield ds.checkout_version(int(version["version"]))
    for name in ds.branches.list():
        branch = ds.checkout_version((name, None))
        for version in branch.versions():
            yield ds.checkout_version((name, int(version["version"])))


def _held(directory: str, name: str, referenced: set[ReferencedFile]) -> bool:
    """Whether the object ``name`` in ``directory`` belongs to a referenced file: the file itself, or a sidecar under its stem."""
    if (directory, name) in referenced:
        return True
    stem, separator, _rest = name.partition("/")
    return bool(separator) and (directory, f"{stem}.lance") in referenced


def _unheld(candidates: dict[str, list[StoredObject]], prefixes: dict[str, str], referenced: set[ReferencedFile]) -> dict[str, list[StoredObject]]:
    """``candidates`` less every object a referenced file holds, by directory path; a directory left empty is dropped."""
    unheld = {path: [obj for obj in objects if not _held(path, obj.path.removeprefix(prefixes[path]), referenced)] for path, objects in candidates.items()}
    return {path: objects for path, objects in unheld.items() if objects}


def unreferenced(
    ds: lance.LanceDataset, directories: Sequence[RecordedBase], storage_options: dict[str, str], *, now: datetime
) -> dict[str, list[StoredObject]]:
    """The objects in ``directories`` no retained version references and older than :data:`UNVERIFIED_AGE`, by directory path.

    The listing comes first and the manifests only for what it leaves: a directory holding nothing past the age costs one
    listing, and one whose old files the heads still reference costs the heads' manifests and no more.
    """
    cutoff = now - UNVERIFIED_AGE
    listed = {directory.path: list_under(directory_uri(directory), storage_options) for directory in directories}
    candidates = {path: [obj for obj in objects if obj.modified <= cutoff] for path, objects in listed.items()}
    prefixes = {directory.path: _listed_prefix(directory) for directory in directories}
    if not any(candidates.values()):
        return {}
    referenced = {file for head in _heads(ds) for file in _referenced_by(head, directories)}
    if not (candidates := _unheld(candidates, prefixes, referenced)):
        return {}
    referenced |= {file for version in _every_retained_version(ds) for file in _referenced_by(version, directories)}
    return _unheld(candidates, prefixes, referenced)


def reclaim(ds: lance.LanceDataset, directories: Sequence[RecordedBase], storage_options: dict[str, str], *, now: datetime) -> Reclaimed:
    """Delete what :func:`unreferenced` finds in the table's data directories, signed by ``storage_options``.

    Raises:
        OSError: The store refused a delete; what was deleted before it stays deleted.
    """
    found = unreferenced(ds, directories, storage_options, now=now)
    by_path = {directory.path: directory for directory in directories}
    files = 0
    size = 0
    for path, objects in found.items():
        delete_objects(directory_uri(by_path[path]), storage_options, [obj.path for obj in objects])
        files += len(objects)
        size += sum(obj.size for obj in objects)
        log.info("maintenance_base_files_reclaimed", extra={"directory": path, "files": len(objects), "bytes": sum(obj.size for obj in objects)})
    return Reclaimed(files=files, bytes=size)


def fragments_holding_deleted_rows(ds: lance.LanceDataset, directories: Sequence[RecordedBase]) -> int:
    """How many of ``ds``'s fragments keep a deleted row's bytes in one of ``directories``: a deletion vector over a base file."""
    owned = _owned_bases(ds, directories)
    if not owned:
        return 0
    return sum(
        1
        for fragment in ds.get_fragments()
        if fragment.metadata.deletion_file is not None and any(data_file.base_id in owned for data_file in fragment.data_files())
    )
