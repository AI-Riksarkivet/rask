"""The one way the estate reclaims a table's versions: marked before its first delete, so no ref door names a version a
cleanup was removing, and a mark trusted only for what it can truthfully say (ADR 0243).

LANCE'S CLEANUP DELETES A VERSION'S FILES BEFORE ITS MANIFEST, AND COUNTS A REFUSED DELETE. Measured on pylance 13.0.0
(ADR 0243 §3, and by review on RustFS 1.0.3): one ``cleanup_old_versions`` deletes the transaction files, the data files,
each data file's blob sidecars (``data/<stem>/<id>.blob``, ``lance_docs/lance/format/table/blob.md`` § Sidecar
Resolution), and the manifests last, and returns with ``CleanupStats.failed_deletes`` counting what the store refused. A
cleanup that stops part-way (its process killed, its credential expired) leaves versions standing over deleted files,
and which files is not what any enumeration of a manifest's file kinds can tell: a version whose sidecar went and whose
data file stayed reads its rows and fails its blobs.

SO EVERY RECLAIM MARKS WHAT IT WILL DELETE, AND EVERY DOOR READS THE MARKS (:class:`Reclaim`, the one function every
caller of Lance's cleanup goes through: a maintenance pass, the catalog's ``maintenance/run`` and ``version/delete``, an
erasure's verification and its release). Under its hold of the table's history, a reclaim names its victims from Lance's
own dry run (``explain_cleanup_old_versions``, :func:`victims`) and cleans them up in batches of :data:`BATCH`: before
each batch it runs its caller's ``before_each`` (a confirm of its holds, which also stops it for a policy door's intent,
``lakehouse.fences.Hold.confirm_unchallenged``), then writes the batch's mark, then runs ``cleanup_old_versions``
restricted to the batch (its ``versions``, which ``lance_docs/lance/guide/read_and_write.md`` § Cleanup old versions says
limits which versions it can remove), and removes the mark once that call returned with no failed delete. So a reclaim
whose confirm failed wrote no mark, and one whose delete the store refused leaves the mark of the batch it was in. A ref
door refuses a version a trusted mark names, whatever files still stand (:func:`refused`); it lists no file of the
version: every cleanup marks before it deletes and unmarks only after a call with no failed delete, so a version no mark
names stands whole or was never touched by a cleanup (``specs/maintenance/TablePass.tla`` RefsWhole, with the doors
judging the mark alone).

A MARK IS TRUSTED ONLY FOR WHAT IT CAN TRUTHFULLY SAY (:func:`trusted`). A maintain-tier vend may put one, a backup may
restore one, and a reclaim may stop after writing one; so one is believed only where a cleanup could have written it:
at the key and in the shape the catalog defines, naming this table, at most :data:`BATCH` versions, each below its ref's
head and pinned by no tag and no branch cut from it. Anything else is ignored whole by every door, logged and counted
(``lance.reclaim_marks.ignored``), and removed by the next reclaim. A trusted mark ends at the next reclaim that finds it
with its writer holding nothing (:attr:`VictimsMark.holder`, :attr:`lakehouse.fences.Hold.holders`): removed when every
file Lance's dry run restricted to its standing versions names still stands (nothing was half-removed, so there is
nothing to protect; measured on pylance 13.0.0, that dry run names each version's transaction, data and blob sidecar
files and its manifest, and leaves out a version whose manifest is gone), or finished when its reclaim's policy still
reclaims its versions, or left, half-removed, where a policy now keeps them, which the pass counts and an operator sees
(``maintenance.reclaims.held_back{reason="marked"}`` and the oldest mark's age). What a forged mark can make a door
refuse is versions its writer could have deleted anyway, until the next reclaim judges it.

Every function is blocking IO.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from opentelemetry import metrics
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from lakehouse import records, stored_objects
from lakehouse.fences import MARK_NAME, Hold, mark_name
from lakehouse.history_fences import VICTIMS_MARK, fence_directory
from lakehouse.storage_options import StorageOptions, on_s3
from lakehouse.version_retention import fork_versions, recorded_branch


if TYPE_CHECKING:
    import lance


log = logging.getLogger(__name__)
_meter = metrics.get_meter("lance.app")
_ignored = _meter.create_counter(
    "lance.reclaim_marks.ignored",
    unit="{mark}",
    description="Cleanup marks ignored as no cleanup's: naming a version at or above its ref's head, one a tag or a branch pins, or more than a batch.",
)

#: How many versions one cleanup call removes, and so the most one mark names: Lance plans each call by reading the ref's
#: retained manifests, so a call per version would read them once per version, and a reclaim confirms its holds, and
#: meets a policy door's intent, between two calls (ADR 0243 §9, F4).
BATCH: Final = 16

#: How many candidate files a dry run names before it says it truncated: past it the versions are taken as every one the
#: ref holds below its head and pinned by nothing, a superset, which only makes a door refuse more.
_MOST_CANDIDATES: Final = 200_000

#: A manifest's name under ``_versions/``: V2's ``<u64::MAX - version, 20 digits>.manifest`` or V1's ``<version>.manifest``
#: (``lance_docs/lance/format/table/layout.md``).
_V2_MANIFEST: Final = re.compile(r"(\d{20})\.manifest")
_V1_MANIFEST: Final = re.compile(r"(\d{1,19})\.manifest")
_U64_MAX: Final = (1 << 64) - 1


class VictimsMark(BaseModel):
    """One cleanup call's mark: the versions of one ref of one table it will remove."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The uuid of the table whose history the mark is in; a mark naming another is no mark of this table's.
    uuid: str
    #: The branch the versions are of, ``None`` for main, whichever way a caller spelled main.
    ref: str | None
    versions: list[int] = Field(min_length=1)
    #: The token of its writer's hold of the table's history: while that holder stands the writer may still delete, so no
    #: other reclaim judges the mark. ``None`` names no writer.
    holder: str | None = None

    @field_validator("ref", mode="before")
    @classmethod
    def _one_main(cls, ref: object) -> str | None:
        return recorded_branch(ref)


class CleanupDone(BaseModel):
    """What one marked reclaim did."""

    #: The versions its marks named; empty when the dry run named none, and no cleanup ran.
    marked: list[int] = Field(default_factory=list)
    old_versions: int = 0
    bytes_removed: int = 0
    failed_deletes: int = 0
    #: The trusted marks of this ref still standing once it returned, its own among them: each names a version that may
    #: stand over deleted files, which no door may name until a later reclaim finishes it.
    marks_left: int = 0


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


class RefPins(BaseModel):
    """What a ref's cleanup keeps whatever it is asked: its head, and the versions a tag pins; and the versions a branch was
    cut from, whose manifests it keeps for the branch."""

    model_config = ConfigDict(frozen=True)

    head: int
    tagged: frozenset[int]
    forked: frozenset[int]


def pins(dataset: lance.LanceDataset, ref: str | None) -> RefPins:
    """``ref``'s head (main for ``None``), read off any handle of the table, every version of it a tag pins (a tag on another
    branch's version of the same number pins nothing here), and every version of it a branch was cut from.

    Raises:
        OSError: The store failed a read of the refs or the head.
    """
    ref = recorded_branch(ref)
    tagged = {int(tag["version"]) for tag in dataset.tags.list().values() if recorded_branch(tag.get("branch")) == ref}
    return RefPins(head=int(dataset.checkout_version((ref, None)).version), tagged=frozenset(tagged), forked=frozenset(fork_versions(dataset, ref).values()))


def victims(dataset: lance.LanceDataset, *, older_than: timedelta | None, retain_versions: int | None, ref: str | None) -> list[int]:
    """The versions ``dataset``'s ref ``ref`` would lose to ``cleanup_old_versions`` with these arguments (tagged versions
    exempt), as Lance's own dry run names their manifests; every version below the ref's head that nothing pins when the
    dry run truncated its list, a superset.

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
    held = pins(dataset, ref)
    log.warning("reclaim_victims_superset", extra={"uri": str(dataset.uri), "explained": explained.stats.old_versions, "named": len(named)})
    return sorted(version for entry in dataset.versions() if (version := int(entry["version"])) < held.head and version not in held.tagged | held.forked)


def write(control_root: str, storage_options: StorageOptions, *, table_uuid: str, ref: str | None, versions: list[int], holder: str) -> str:
    """Mark ``versions`` of ``ref`` (main for ``None``) of the table ``table_uuid`` as a cleanup call's victims, written by
    the holder ``holder``; the mark's id.

    Raises:
        OSError: The store refused the write.
    """
    mark_id = uuid.uuid4().hex
    body = VictimsMark(uuid=table_uuid, ref=ref, versions=sorted(set(versions)), holder=holder).model_dump(mode="json")
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
            _ignored.add(1)
            continue
        if mark is None:
            continue
        if mark.uuid != table_uuid:
            log.warning("reclaim_mark_of_another_table", extra={"table_uuid": table_uuid, "key": key, "names": mark.uuid})
            _ignored.add(1)
            continue
        found[mark_id] = mark
    return found


def trusted(marks: dict[str, VictimsMark], *, ref: str | None, held: RefPins) -> tuple[dict[str, VictimsMark], list[str]]:
    """The marks of ``ref`` (main for ``None``) a cleanup could have written, and the ids of those of it no cleanup could
    have: naming more than :data:`BATCH` versions, or one at or above the head, or one a tag pins. The others are logged
    and counted, and a door ignores them whole.
    """
    ref = recorded_branch(ref)
    kept: dict[str, VictimsMark] = {}
    ignored: list[str] = []
    for mark_id, mark in marks.items():
        if mark.ref != ref:
            continue
        if len(mark.versions) <= BATCH and all(version < held.head and version not in held.tagged for version in mark.versions):
            kept[mark_id] = mark
            continue
        log.warning(
            "reclaim_mark_ignored", extra={"mark": mark_id, "ref": ref, "versions": mark.versions[:20], "head": held.head, "tagged": sorted(held.tagged)[:20]}
        )
        _ignored.add(1)
        ignored.append(mark_id)
    return kept, ignored


def refused(
    control_root: str, storage_options: StorageOptions, *, table_uuid: str, marks: Iterable[tuple[str, str]], dataset: lance.LanceDataset, ref: str | None
) -> set[int]:
    """The versions of ``ref`` a door holding the table's history as ref work refuses to pin or bring back: those a trusted
    mark among ``marks`` (its take's, :attr:`lakehouse.fences.Hold.marks`) names. Constant in the table's size: a read per
    mark, the ref's head and its pins.

    Raises:
        OSError: The store failed a read of a mark or of the ref.
    """
    standing = read(control_root, storage_options, table_uuid=table_uuid, marks=marks)
    if not standing:
        return set()
    kept, _ignored_ids = trusted(standing, ref=ref, held=pins(dataset, ref))
    return {version for mark in kept.values() for version in mark.versions}


def oldest(control_root: str, storage_options: StorageOptions, *, table_uuid: str) -> timedelta | None:
    """How long, on the store's clock, the oldest victims mark standing directly in the table ``table_uuid``'s history
    fence directory has stood; ``None`` for none. One listing, under a session that reads none of the fence's objects: what
    a pass asks before it vends (whether to finish a mark), and what the oldest mark's age reports.

    Raises:
        OSError: The store failed the listing.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    directory = fence_directory(table_uuid)
    try:
        listing = stored_objects.list_under_at(f"{control_root.rstrip('/')}/{directory}", storage_options)
    except (ClientError, BotoCoreError) as exc:
        raise OSError(f"the store did not answer listing {directory}: {exc}") from exc
    stamps: list[datetime] = []
    for stored in listing.objects:
        relative = stored.path[stored.path.find(f"{directory}/") + len(directory) + 1 :]
        if (name := MARK_NAME.fullmatch(relative)) is not None and name.group(1) == VICTIMS_MARK:
            stamps.append(stored.modified)
    return listing.at - min(stamps) if stamps else None


def _keys_under(dataset: lance.LanceDataset, paths: list[str]) -> tuple[str, list[str]]:
    """A root of the store the table is in, and each of ``paths`` (as Lance's dry run names them: the store path, bucket
    first on S3, the absolute path without its leading ``/`` on a local root) as a key beneath it."""
    uri = str(dataset.uri)
    if on_s3(uri):
        bucket = uri.removeprefix("s3://").split("/", 1)[0]
        return f"s3://{bucket}", [path.removeprefix(f"{bucket}/") for path in paths]
    return "/", [path.lstrip("/") for path in paths]


def _untouched(dataset: lance.LanceDataset, versions: list[int], storage_options: StorageOptions) -> bool:
    """Whether every file Lance's dry run restricted to ``versions`` names (each one's transaction, data, blob sidecar and
    deletion files, its indices and its manifest) still stands: no cleanup took any of them.

    Raises:
        OSError: The store failed a read.
    """
    explained = dataset.explain_cleanup_old_versions(
        older_than=timedelta(0), retain_versions=None, error_if_tagged_old_versions=False, include_files=True, max_files=_MOST_CANDIDATES, versions=versions
    )
    if explained.candidate_files_truncated or explained.stats.old_versions != len(versions):
        return False
    root, keys = _keys_under(dataset, [candidate.path for candidate in explained.candidate_files])
    return len(stored_objects.identities(root, storage_options, keys)) == len(set(keys))


class Reclaim(BaseModel):
    """Where a holder of a table's history reclaims one ref of it: the control root its marks are kept on and the
    credential that writes them, its hold, and what it asks before each batch."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    control_root: str
    storage_options: StorageOptions
    table_uuid: str
    #: The branch it reclaims, ``None`` for main, whichever way a caller spelled main.
    ref: str | None
    #: Its hold of the table's history: the marks and the holders its take saw, and its own token.
    hold: Hold
    #: Run before each batch's mark: a confirm of every hold the caller takes, which raises to stop the reclaim.
    before_each: Callable[[], None] | None = None

    @field_validator("ref", mode="before")
    @classmethod
    def _one_main(cls, ref: object) -> str | None:
        return recorded_branch(ref)

    def run(
        self,
        dataset: lance.LanceDataset,
        *,
        older_than: timedelta | None,
        retain_versions: int | None,
        versions: list[int] | None = None,
        error_if_tagged: bool = False,
        forks: bool = False,
    ) -> CleanupDone:
        """``cleanup_old_versions`` on ``dataset`` (a handle of this ref), marked: each standing mark of the ref judged
        first, then the victims named (``versions``, else :func:`victims` under ``older_than`` and ``retain_versions``)
        and cleaned up a batch at a time, each batch after ``before_each``, marked before its call and unmarked after a
        call with no failed delete; a call with one stops the reclaim, its mark standing.

        ``forks`` names beside them every version of the ref a branch was cut from that no tag pins, which an erasure must
        take a subject's bytes from: measured on pylance 13.0.0, a cleanup naming such a version deletes the files only it
        names (as an unrestricted cleanup does) and keeps its manifest for the branch, so the version stands over deleted
        files for as long as the branch keeps it, marked, and no door names it. Such a mark is not counted in
        :attr:`CleanupDone.marks_left`: no reclaim can finish it, and it is the erasure's answer, not a reclaim starved.

        Raises:
            OSError: The store refused a mark's read or write, or Lance raised: nothing more is deleted, and a batch's mark,
                once written, stands.
            Exception: Whatever ``before_each`` raises; no mark is written for the batch it stopped.
        """
        standing = read(self.control_root, self.storage_options, table_uuid=self.table_uuid, marks=self.hold.marks)
        held = pins(dataset, self.ref)
        mine, ignored = trusted(standing, ref=self.ref, held=held)
        for mark_id in ignored:
            remove(self.control_root, self.storage_options, table_uuid=self.table_uuid, mark_id=mark_id)
        still = {int(entry["version"]) for entry in dataset.versions()}
        left = self._judged(dataset, mine, still)
        named = set(versions) if versions is not None else set(victims(dataset, older_than=older_than, retain_versions=retain_versions, ref=self.ref))
        if forks:
            named |= {version for version in held.forked if version < held.head and version not in held.tagged}
        done = CleanupDone(marked=sorted(named))
        named_now = done.marked
        for start in range(0, len(named_now), BATCH):
            batch = named_now[start : start + BATCH]
            if self.before_each is not None:
                self.before_each()
            mark_id = write(self.control_root, self.storage_options, table_uuid=self.table_uuid, ref=self.ref, versions=batch, holder=self.hold.token)
            stats: Any = dataset.cleanup_old_versions(
                older_than=older_than, retain_versions=retain_versions, error_if_tagged_old_versions=error_if_tagged, versions=batch
            )
            done.old_versions += int(stats.old_versions)
            done.bytes_removed += int(stats.bytes_removed)
            if stats.failed_deletes:
                done.failed_deletes += int(stats.failed_deletes)
                log.warning(
                    "reclaim_mark_left",
                    extra={"uri": str(dataset.uri), "table_uuid": self.table_uuid, "failed_deletes": stats.failed_deletes, "versions": batch},
                )
                done.marks_left = sum(1 for mark in left.values() if not set(mark.versions) <= held.forked) + 1
                return done
            remove(self.control_root, self.storage_options, table_uuid=self.table_uuid, mark_id=mark_id)
        still = {int(entry["version"]) for entry in dataset.versions()}
        for other_id, other in left.items():
            if not set(other.versions) & still:
                remove(self.control_root, self.storage_options, table_uuid=self.table_uuid, mark_id=other_id)
            elif not set(other.versions) & still <= held.forked:
                done.marks_left += 1
        return done

    def _judged(self, dataset: lance.LanceDataset, marks: dict[str, VictimsMark], still: set[int]) -> dict[str, VictimsMark]:
        """The marks of ``marks`` left standing once each whose writer holds nothing any more is judged: one naming no
        version that stands, or one every file of whose standing versions still stands, is removed.

        Raises:
            OSError: The store failed a read.
        """
        left: dict[str, VictimsMark] = {}
        for mark_id, mark in marks.items():
            standing = sorted(set(mark.versions) & still)
            live = mark.holder is not None and (mark.holder in self.hold.holders or mark.holder == self.hold.token)
            if not live and (not standing or _untouched(dataset, standing, self.storage_options)):
                log.info("reclaim_mark_judged_whole", extra={"table_uuid": self.table_uuid, "mark": mark_id, "versions": standing[:20]})
                remove(self.control_root, self.storage_options, table_uuid=self.table_uuid, mark_id=mark_id)
                continue
            left[mark_id] = mark
        return left
