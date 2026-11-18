"""What a cleanup will remove, marked before its first delete, so no ref door names a version it was removing (ADR 0243).

LANCE'S CLEANUP DELETES A VERSION'S FILES BEFORE ITS MANIFEST, AND COUNTS A REFUSED DELETE. Measured on pylance 13.0.0
(ADR 0243 §3, and by review on RustFS 1.0.3): one ``cleanup_old_versions`` deletes the transaction files, the data files,
each data file's blob sidecars (``data/<stem>/<id>.blob``, ``lance_docs/lance/format/table/blob.md`` § Sidecar
Resolution), and the manifests last, and returns with ``CleanupStats.failed_deletes`` counting what the store refused. A
cleanup that stops part-way (its process killed, its credential expired) leaves versions standing over deleted files,
and which files is not what any enumeration of a manifest's file kinds can tell: a version whose sidecar went and whose
data file stayed reads its rows and fails its blobs.

SO THE GUARD RESTS ON WHAT THE CLEANUP DECIDED, NOT ON THE FILES THAT STAND. Under its RECLAIM hold of the table's history,
before any delete, a reclaim names the versions its cleanup will remove from Lance's own dry run
(``explain_cleanup_old_versions``, :func:`victims`), writes them as a mark in the table's history fence directory
(:func:`write`), and runs ``cleanup_old_versions`` restricted to them (its ``versions``, which
``lance_docs/lance/guide/read_and_write.md`` § Cleanup old versions says limits which versions it can remove), so it
removes no version the mark omits. The mark is removed only once its cleanup returned with no failed delete
(:func:`cleanup`). A ref door, holding the history as ref work, refuses a version a standing mark names, whatever files
still stand (``catalog.services.dataplane.require_whole``); and a later reclaim that finds a mark finishes its versions
while its policy still reclaims them, since they were past their retention and no door may pin them, then removes it.

THE MARK IS TRUSTED ONLY WHERE AND AS THE CATALOG DEFINES IT. Its key is ``<history fence directory>/reclaim-victims.<32
hex>.json`` (:data:`lakehouse.history_fences.VICTIMS_MARK`, directly in the directory: a name below a subdirectory is no
mark, :class:`lakehouse.fences.HolderFence`), its body a :class:`VictimsMark` naming the table's uuid; anything else there
is ignored and logged. A maintain-tier vend may put and delete one (``catalog.core.vending.TableRecords``), and what it
can plant only makes a ref door refuse versions of its own table, which its RECLAIM hold already lets it do.

Every function is blocking IO.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable, Iterable
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lakehouse import records, stored_objects
from lakehouse.fences import MARK_NAME, mark_name
from lakehouse.history_fences import VICTIMS_MARK, fence_directory
from lakehouse.storage_options import StorageOptions


if TYPE_CHECKING:
    import lance


log = logging.getLogger(__name__)

#: How many candidate files a dry run names before it says it truncated: past it the versions are taken as every one the
#: ref holds below its head and untagged, a superset, which only makes a door refuse more.
_MOST_CANDIDATES: Final = 200_000

#: A manifest's name under ``_versions/``: V2's ``<u64::MAX - version, 20 digits>.manifest`` or V1's ``<version>.manifest``
#: (``lance_docs/lance/format/table/layout.md``).
_V2_MANIFEST: Final = re.compile(r"(\d{20})\.manifest")
_V1_MANIFEST: Final = re.compile(r"(\d{1,19})\.manifest")
_U64_MAX: Final = (1 << 64) - 1


class VictimsMark(BaseModel):
    """One cleanup's mark: the versions of one ref of one table it will remove."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The uuid of the table whose history the mark is in; a mark naming another is no mark of this table's.
    uuid: str
    #: The branch the versions are of, ``None`` for main.
    ref: str | None
    versions: list[int] = Field(min_length=1)


class CleanupDone(BaseModel):
    """What one marked cleanup did."""

    #: The versions its mark named; empty when the dry run named none, and no cleanup ran.
    marked: list[int] = Field(default_factory=list)
    old_versions: int = 0
    bytes_removed: int = 0
    failed_deletes: int = 0
    #: Whether its mark still stands: some delete failed, so a version it named may stand over deleted files.
    mark_left: bool = False
    #: The marks of this ref still standing once it returned, its own among them: each names a version that may stand over
    #: deleted files, which no door may name until a later cleanup removes it.
    marks_left: int = 0


class MarkedCleanup(BaseModel):
    """Where a caller holding a table's history as a reclaim marks its cleanups (:func:`cleanup`), and what it asks
    before each."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    control_root: str
    storage_options: StorageOptions
    table_uuid: str
    #: The branch the cleanups are of, ``None`` for main.
    ref: str | None
    #: The marks the caller's take saw (:func:`read`).
    standing: dict[str, VictimsMark] = Field(default_factory=dict)
    before_each: Callable[[], None] | None = None
    batch: int | None = None

    def run(
        self,
        dataset: lance.LanceDataset,
        *,
        older_than: timedelta | None,
        retain_versions: int | None,
        versions: list[int] | None = None,
        error_if_tagged: bool = False,
    ) -> CleanupDone:
        """:func:`cleanup` here.

        Raises:
            OSError: As :func:`cleanup`.
        """
        return cleanup(
            dataset,
            control_root=self.control_root,
            storage_options=self.storage_options,
            table_uuid=self.table_uuid,
            ref=self.ref,
            older_than=older_than,
            retain_versions=retain_versions,
            standing=self.standing,
            versions=versions,
            before_each=self.before_each,
            batch=self.batch,
            error_if_tagged=error_if_tagged,
        )


def mark_key(table_uuid: str, mark_id: str) -> str:
    """The key, on the control root, of the mark ``mark_id`` of the table ``table_uuid``."""
    return f"{fence_directory(table_uuid)}/{mark_name(VICTIMS_MARK, mark_id)}"


def _version_of(path: str) -> int | None:
    name = path.rsplit("/", 1)[-1]
    if (v2 := _V2_MANIFEST.fullmatch(name)) is not None:
        return _U64_MAX - int(v2.group(1))
    if (v1 := _V1_MANIFEST.fullmatch(name)) is not None:
        return int(v1.group(1))
    return None


def victims(dataset: lance.LanceDataset, *, older_than: timedelta | None, retain_versions: int | None) -> list[int]:
    """The versions ``dataset``'s ref would lose to ``cleanup_old_versions`` with these arguments (tagged versions exempt),
    as Lance's own dry run names their manifests; every version below the handle's head that no tag names when the dry
    run truncated its list, a superset.

    Raises:
        OSError: The store failed a read of the dry run.
    """
    explained = dataset.explain_cleanup_old_versions(
        older_than=older_than, retain_versions=retain_versions, error_if_tagged_old_versions=False, include_files=True, max_files=_MOST_CANDIDATES
    )
    named = sorted(
        {version for candidate in explained.candidate_files if candidate.kind == "manifest" and (version := _version_of(candidate.path)) is not None}
    )
    if not explained.candidate_files_truncated and len(named) == explained.stats.old_versions:
        return named
    tagged = {int(tag["version"]) for tag in dataset.tags.list().values()}
    log.warning("reclaim_victims_superset", extra={"uri": str(dataset.uri), "explained": explained.stats.old_versions, "named": len(named)})
    return sorted(int(entry["version"]) for entry in dataset.versions() if int(entry["version"]) < int(dataset.version) and int(entry["version"]) not in tagged)


def write(control_root: str, storage_options: StorageOptions, *, table_uuid: str, ref: str | None, versions: list[int]) -> str:
    """Mark ``versions`` of ``ref`` (main for ``None``) of the table ``table_uuid`` as a cleanup's victims; the mark's id.

    Raises:
        OSError: The store refused the write.
    """
    mark_id = uuid.uuid4().hex
    body = VictimsMark(uuid=table_uuid, ref=ref, versions=sorted(set(versions))).model_dump(mode="json")
    records.create_json(control_root, storage_options, mark_key(table_uuid, mark_id), body)
    return mark_id


def remove(control_root: str, storage_options: StorageOptions, *, table_uuid: str, mark_id: str) -> None:
    """Remove the mark ``mark_id``. Never raises: a mark left stands until a later reclaim removes it, and refuses only refs."""
    try:
        stored_objects.remove_object(control_root, storage_options, mark_key(table_uuid, mark_id))
    except Exception as exc:
        log.warning("reclaim_mark_not_removed", extra={"table_uuid": table_uuid, "mark": mark_id, "reason": f"{type(exc).__name__}: {exc}"[:300]})


def read(control_root: str, storage_options: StorageOptions, *, table_uuid: str, marks: Iterable[tuple[str, str]]) -> dict[str, VictimsMark]:
    """Each victims mark among ``marks`` (``(kind, id)`` as a take of the table's history fence lists them,
    :attr:`lakehouse.fences.Hold.marks`), read by id. One that is gone, does not parse as a :class:`VictimsMark`, or names
    another table is no mark of this table's, and is logged and left out.

    Raises:
        OSError: The store failed a read.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    found: dict[str, VictimsMark] = {}
    for kind, mark_id in sorted(marks):
        if kind != VICTIMS_MARK:
            continue
        key = mark_key(table_uuid, mark_id)
        try:
            stored = records.read_json_at(control_root, storage_options, key).stored
            mark = None if stored is None else VictimsMark.model_validate(stored.record)
        except (ClientError, BotoCoreError) as exc:
            raise OSError(f"the store did not answer reading the mark {key}: {exc}") from exc
        except (ValueError, ValidationError) as exc:
            log.warning("reclaim_mark_unparsed", extra={"table_uuid": table_uuid, "key": key, "reason": str(exc)[:300]})
            continue
        if mark is None:
            continue
        if mark.uuid != table_uuid:
            log.warning("reclaim_mark_of_another_table", extra={"table_uuid": table_uuid, "key": key, "names": mark.uuid})
            continue
        found[mark_id] = mark
    return found


def named(control_root: str, storage_options: StorageOptions, *, table_uuid: str) -> bool:
    """Whether one listing of the table ``table_uuid``'s history fence directory names a victims mark directly in it: what a
    reader holding no hold and no read of the fence's objects asks; a reclaim's take answers it for free, and reads the
    marks (:func:`read`).

    Raises:
        OSError: The store failed the listing.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    directory = fence_directory(table_uuid)
    try:
        listing = stored_objects.list_under(f"{control_root.rstrip('/')}/{directory}", storage_options)
    except (ClientError, BotoCoreError) as exc:
        raise OSError(f"the store did not answer listing {directory}: {exc}") from exc
    for stored in listing:
        relative = stored.path[stored.path.find(f"{directory}/") + len(directory) + 1 :]
        if (name := MARK_NAME.fullmatch(relative)) is not None and name.group(1) == VICTIMS_MARK:
            return True
    return False


def marked_versions(marks: dict[str, VictimsMark], ref: str | None) -> set[int]:
    """The versions of ``ref`` (main for ``None``) ``marks`` name."""
    return {version for mark in marks.values() if mark.ref == ref for version in mark.versions}


def cleanup(
    dataset: lance.LanceDataset,
    *,
    control_root: str,
    storage_options: StorageOptions,
    table_uuid: str,
    ref: str | None,
    older_than: timedelta | None,
    retain_versions: int | None,
    standing: dict[str, VictimsMark],
    versions: list[int] | None = None,
    before_each: Callable[[], None] | None = None,
    batch: int | None = None,
    error_if_tagged: bool = False,
) -> CleanupDone:
    """``cleanup_old_versions`` on ``dataset``'s ref, marked: its victims named (``versions``, else :func:`victims`), the mark
    written under ``control_root`` with ``storage_options`` before any delete, the cleanup restricted to them, and the
    mark removed, with each of ``standing`` (the marks the caller's take saw) of this ref naming no version that still
    stands, only once every cleanup returned with no failed delete.

    The caller holds the table's history as a reclaim for the whole call. ``batch`` splits the victims into cleanups of
    that many versions, and ``before_each`` runs before each (a caller signing with a credential that never dies confirms
    its holds there, ADR 0243 §8).

    Raises:
        OSError: The store refused the mark's write, or Lance raised: nothing more is deleted, and the mark, once written,
            stands.
    """
    mine = {mark_id: mark for mark_id, mark in standing.items() if mark.ref == ref}
    named = sorted(set(versions)) if versions is not None else victims(dataset, older_than=older_than, retain_versions=retain_versions)
    if not named:
        return CleanupDone(marks_left=len(mine))
    mark_id = write(control_root, storage_options, table_uuid=table_uuid, ref=ref, versions=named)
    done = CleanupDone(marked=named, mark_left=True)
    size = batch or len(named)
    for start in range(0, len(named), size):
        if before_each is not None:
            before_each()
        stats = dataset.cleanup_old_versions(
            older_than=older_than, retain_versions=retain_versions, error_if_tagged_old_versions=error_if_tagged, versions=named[start : start + size]
        )
        done.old_versions += stats.old_versions
        done.bytes_removed += stats.bytes_removed
        done.failed_deletes += stats.failed_deletes
    if done.failed_deletes:
        log.warning(
            "reclaim_mark_left", extra={"uri": str(dataset.uri), "table_uuid": table_uuid, "failed_deletes": done.failed_deletes, "versions": named[:20]}
        )
        done.marks_left = len(mine) + 1
        return done
    remove(control_root, storage_options, table_uuid=table_uuid, mark_id=mark_id)
    done.mark_left = False
    still = {int(entry["version"]) for entry in dataset.versions()}
    for other_id, other in mine.items():
        if set(other.versions) & still:
            done.marks_left += 1
        else:
            remove(control_root, storage_options, table_uuid=table_uuid, mark_id=other_id)
    return done
