"""A registered shallow clone keeps the source version it reads through by a Lance tag on its source (ADR 0198 §1).

A shallow clone is a dataset of its own whose manifest names its source's data files through a dataset-root base, and
the source records nothing of it (`lance_docs/lance/format/table/transaction.md` § Clone). It "relies on continued
source retention, such as a source tag" (`lance_docs/lance/format/table/blob.md:309-313`), and a tagged version is
exempt from cleanup (`lance_docs/lance/guide/tags_and_branches.md:61-67`). The register door is the one door a clone
enters the estate through (a write vend may not put a final manifest), so it tags the source version the clone's own
first transaction names (:func:`clone_reference`), and every door that ends the clone deletes the tag
(:func:`release`). Measured on pylance 13.0.0 (2026-11-08): a clone of a tagged version read all its rows after its
source was overwritten, compacted and cleaned at age 0, and failed (``Not found``) once the tag was deleted.

The tag is named for the clone's location (:func:`tag_of`), digested as its location's claim is keyed, so every door
that forgets the clone's base record releases what it holds without knowing more (`base_registry.forget_base_record`),
and a re-registration of the same bytes takes the same tag. It is also the source's own record of who reads it: a drop
that destroys a table's bytes, and the purge of a trashed one, refuse while one stands (:func:`clones_reading`). No
writer can plant one: no vend puts under ``_refs/`` (`lakehouse.table_layout`), and the tag doors refuse every
``rask.`` tag.

Every function is blocking IO.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict

from lakehouse import objectfs, stored_objects
from lakehouse.base_refs import decoded_path, key_uri, store_of
from lakehouse.history_fences import HistoryWork, TableHistory


if TYPE_CHECKING:
    import lance

    from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

#: The prefix of every tag the catalog takes on a source for a clone; the rest of the name is the digest of the clone's
#: location. The tag doors refuse every ``rask.`` tag, so only the catalog writes or deletes one.
TAG_PREFIX: Final = "rask.clone."

#: Where Lance keeps a dataset's tags (`lance_docs/lance/format/table/branch_tag.md` § Tag Storage).
_TAGS_DIR: Final = "_refs/tags"
_TAG_SUFFIX: Final = ".json"


class CloneSourceGoneError(ValueError):
    """The source version a clone reads through is no longer retained, so tagging it would protect nothing."""


class ReadFromOutsideError[K](ValueError):
    """Tables a removal would destroy are read by registered clones it does not remove with them."""

    def __init__(self, read: Mapping[K, set[str]]) -> None:
        super().__init__(f"{len(read)} table(s) are read by registered clones outside what is removed")
        #: Each such table, and the clone tags it carries from outside.
        self.read = dict(read)


class CloneReference(BaseModel):
    """What a shallow clone's first transaction says it reads: the source root, the version, and the branch it is on."""

    model_config = ConfigDict(frozen=True)

    #: The source's root as the cloner spelled it (``Clone.ref_path``).
    path: str
    version: int
    #: The source branch the version is on, ``None`` for main.
    branch: str | None = None


class CloneSource(BaseModel):
    """One source a registered clone reads through, resolved to the table the catalog governs there."""

    model_config = ConfigDict(frozen=True)

    #: The source table's location, spelled as the catalog records it, which Lance opens.
    location: str
    #: The source table's uuid, which its history fence is kept under.
    table_uuid: str
    #: The source table's canonical id, for a refusal or a log line.
    table: str
    #: The source table's identifier segments, as its claim records them.
    segments: list[str]
    version: int
    branch: str | None = None


def tag_of(clone_location: str) -> str:
    """The tag the clone at ``clone_location``, in either spelling, holds on each source it reads through: the SHA-256 of
    the path it decodes to, as `lakehouse.location_claims` keys the location's claim."""
    return f"{TAG_PREFIX}{hashlib.sha256(decoded_path(clone_location).encode()).hexdigest()}"


def clone_reference(clone: lance.LanceDataset) -> CloneReference | None:
    """What the dataset's first version says it was cloned from, or ``None`` when that version is not a shallow clone.

    A Clone can only be a dataset's first operation (`lance_docs/lance/format/table/transaction.md` § Clone
    Compatibility), and a shallow clone takes the source's version number (measured on pylance 13.0.0: a clone of
    version 2 starts at version 2), so the first version listed holds it.
    """
    import lance

    first = min(int(entry["version"]) for entry in clone.versions())
    transaction = clone.read_transaction(first)
    operation = transaction.operation if transaction is not None else None
    if not isinstance(operation, lance.LanceOperation.Clone) or not operation.is_shallow:
        return None
    return CloneReference(path=operation.ref_path, version=operation.ref_version, branch=operation.branch_name or None)


def _reference(source: CloneSource) -> int | tuple[str, int]:
    """The pylance tag reference for the source version: a bare int on main, the ``(branch, version)`` tuple elsewhere,
    since branch histories are numbered independently (`lance_docs/lance/guide/tags_and_branches.md` § Tags)."""
    return (source.branch, source.version) if source.branch is not None else source.version


def _open(location: str, storage_options: StorageOptions) -> lance.LanceDataset:
    import lance

    return lance.dataset(location, storage_options=storage_options.in_clear())


def _dataset_key(location: str) -> str:
    """The key ``location``, in either spelling the estate holds it, decodes to: where the store keeps the dataset."""
    return key_uri(store_of(location), decoded_path(location))


def _holds(dataset: lance.LanceDataset, source: CloneSource) -> bool:
    """Whether the source still keeps the version the clone reads, on the branch the clone reads it from."""
    if source.branch is not None and source.branch not in dataset.branches.list():
        return False
    ref = dataset if source.branch is None else dataset.checkout_version((source.branch, None))
    return any(int(entry["version"]) == source.version for entry in ref.versions())


def take(source: CloneSource, *, clone_location: str, control_root: str, storage_options: StorageOptions) -> None:
    """Tag the source version the clone reads through, under the source's history fence.

    The fence keeps a reclaim from deleting the version between the check that the source still keeps it and the tag
    landing ([[LH-308]]). A tag already naming that version is a retry's, and kept. One naming another version was left
    by an earlier clone at the same location whose end could not delete it: the register door holds the location's
    claim, so nothing reads through that tag any more, and it is moved to this clone's version.

    Raises:
        CloneSourceGoneError: The source no longer keeps the version, or the branch it is on.
        lakehouse.history_fences.HistoryFencedError: A reclaim holds the source's history; nothing was written.
    """
    tag = tag_of(clone_location)
    dataset = _open(source.location, storage_options)
    history = TableHistory(control_root=control_root, storage_options=storage_options, table_uuid=source.table_uuid)
    with history.held(HistoryWork.REFS):
        held = dataset.tags.list().get(tag)
        if held is not None and (int(held["version"]), held.get("branch") or None) == (source.version, source.branch):
            return
        if not _holds(dataset, source):
            raise CloneSourceGoneError(f"version {source.version} of {source.table} is not retained")
        if held is None:
            dataset.tags.create(tag, _reference(source))
            log.info("clone_source_tagged", extra={"source": source.table, "clone": clone_location, "version": source.version, "branch": source.branch})
        else:
            dataset.tags.update(tag, _reference(source))
            log.warning("clone_source_retagged", extra={"source": source.table, "clone": clone_location, "from": held["version"], "to": source.version})


def release(source_location: str, *, clone_location: str, storage_options: StorageOptions) -> bool:
    """Delete the tag of the clone at ``clone_location`` on the source at ``source_location``; whether one was there.

    A source whose dataset is gone holds no tag, so nothing is deleted. Held under no fence: a reclaim reads the tags
    once per pass, so a tag deleted while one runs only keeps the version one pass longer.

    Raises:
        OSError: The store did not answer the open, the read or the delete.
        ValueError: The source's dataset stands and could not be opened, or its tag could not be deleted.
    """
    tag = tag_of(clone_location)
    try:
        dataset = _open(source_location, storage_options)
    except (OSError, ValueError):
        if objectfs.is_lance_dataset_root(_dataset_key(source_location), storage_options):
            raise
        log.warning("clone_source_gone", extra={"source": source_location, "clone": clone_location})
        return False
    if tag not in dataset.tags.list():
        return False
    dataset.tags.delete(tag)
    log.info("clone_source_untagged", extra={"source": source_location, "clone": clone_location})
    return True


def release_all(source_locations: Iterable[str], *, clone_location: str, storage_options: StorageOptions) -> list[str]:
    """Delete the clone's tag on every source it reads through; the sources a tag was deleted on.

    A source whose dataset is gone holds nothing to release (:func:`release`). Any other failure raises, so the caller
    keeps the record that names the sources for its retry.

    Raises:
        OSError: The store did not answer a read or a delete.
        ValueError: A source's dataset stands and could not be opened, or its tag could not be deleted.
    """
    return [source for source in source_locations if release(source, clone_location=clone_location, storage_options=storage_options)]


def clones_reading(location: str, storage_options: StorageOptions) -> list[str]:
    """The tag of every registered clone the dataset at ``location`` carries, from one listing of its tags.

    ``location`` in either spelling the estate holds it: listed at the key it decodes to.

    Raises:
        OSError: The store refused the listing.
        botocore.exceptions.ClientError: The store refused the listing.
    """
    tags_dir = f"{_dataset_key(location)}/{_TAGS_DIR}"
    names = (stored.path.rsplit("/", 1)[-1] for stored in stored_objects.list_under(tags_dir, storage_options))
    return sorted(name.removesuffix(_TAG_SUFFIX) for name in names if name.startswith(TAG_PREFIX) and name.endswith(_TAG_SUFFIX))


def destroy_order[K](clone_tags: Mapping[K, str], carried: Mapping[K, set[str]]) -> list[K]:
    """The tables a removal destroys, each after every clone that reads it (:func:`clones_reading`).

    ``clone_tags`` maps each table removed to the tag it would hold as a clone (:func:`tag_of` of its location), and
    ``carried`` maps a table to the clone tags it carries. A clone's own end releases its tag
    (`base_registry.forget_base_record`), so a source destroyed after its clones carries none by then. A clone is
    always made after its source, so the order exists.

    Raises:
        ReadFromOutsideError: A table removed is read by a clone that is not removed with it; nothing is ordered.
    """
    inside = set(clone_tags.values())
    if outside := {key: tags - inside for key, tags in carried.items() if tags - inside}:
        raise ReadFromOutsideError(outside)
    order: list[K] = []
    gone: set[str] = set()
    pending = list(clone_tags)
    while pending:
        ready = [key for key in pending if carried.get(key, set()) <= gone]
        if not ready:
            raise ValueError("the clones read each other in a cycle, which no clone can make")
        order.extend(ready)
        gone.update(clone_tags[key] for key in ready)
        pending = [key for key in pending if key not in ready]
    return order
