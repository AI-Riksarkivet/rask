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
own dry run (``explain_cleanup_old_versions``, :func:`victims`) and cleans them up in batches of :data:`BATCH`: each
batch first opens the handle it deletes through (:attr:`Reclaim.session`, a credential minted for that batch where the
caller mints one), then runs its caller's ``before_each`` (a confirm of its holds, which also stops it for a policy
door's intent, ``lakehouse.fences.Hold.confirm_unchallenged``), then writes the batch's mark, then runs
``cleanup_old_versions`` restricted to the batch (its ``versions``, which ``lance_docs/lance/guide/read_and_write.md`` §
Cleanup old versions says limits which versions it can remove), and removes the mark once that call returned with no
failed delete. So a reclaim whose confirm failed wrote no mark, and one whose delete the store refused leaves the mark
of the batch it was in. A ref door refuses a version a trusted mark names, whatever files still stand (:func:`refused`);
it lists no file of the version: every cleanup marks before it deletes and unmarks only after a call with no failed
delete, so a version no mark names stands whole or was never touched by a cleanup (``specs/maintenance/TablePass.tla``
RefsWhole, with the doors judging the mark alone).

A MARK IS TRUSTED ONLY FOR WHAT IT CAN TRUTHFULLY SAY (:func:`trusted`). A maintain-tier vend may put one, a backup may
restore one, and a reclaim may stop after writing one; so one is believed only where a cleanup could have written it:
at the key and in the shape the catalog defines, naming this table and an incarnation of a ref that still stands (a
branch by the uuid Lance gives each incarnation, :func:`lakehouse.version_retention.branch_identity`), at most
:data:`BATCH` versions, each below its ref's head and pinned by no tag. Anything else is ignored whole by every door,
logged and counted (``lance.reclaim_marks.ignored``), and removed by the next reclaim of the table, whichever ref it
reclaims: a deleted branch's mark among them, which a branch made again under its name never inherits. A trusted mark
ends at the next reclaim that finds it with its writer holding nothing (:attr:`VictimsMark.holder`,
:attr:`lakehouse.fences.Hold.holders`): removed when Lance's dry run restricted to its standing versions still names
each one's transaction file, which a cleanup deletes before any other file of its versions (nothing was half-removed, so
there is nothing to protect: :func:`_untouched`), or finished when its reclaim's policy still reclaims its versions, or
left, half-removed, where a policy now keeps them, which the pass counts and an operator sees (``maintenance.reclaims.held_back{reason="marked"}`` and the
oldest mark's age). What a forged mark can make a door refuse is versions its writer could have deleted anyway, until
the next reclaim judges it.

AN ERASURE'S FORK POINTS STAND MARKED (:class:`ForkMark`, ADR 0243 R1). Measured on pylance 13.0.0 (ADR 0243 §13): a
cleanup naming a version a branch was cut from keeps its manifest while the branch's own history holds a copy of it, and
then deletes only its transaction file, which the manifest carries inline (``read_transaction`` answers from it, and
every read, tag, restore and clone of the version works); but once the branch's own history no longer names the fork's
files (an erasure rewrote the branch and reclaimed it, deepest first), the same cleanup deletes the files only the fork
names and still keeps the manifest, and every scan, tag checkout, restore, shallow or deep clone and branch from that
version fails ``Not found``. An erasure's reclaim names its ref's fork points to take a subject's bytes from them
(:meth:`Reclaim.run` ``forks``), so it marks each with a fork mark before the batch that names it, apart from the batch's
mark and never removed by the run: a door refuses the version while the mark stands. A fork mark is no stuck reclaim:
the oldest-mark gauge and a pass's probe count victims marks alone (:func:`oldest`). It ends with the version, or, once
no branch is cut from it, is judged as a dead cleanup's mark: removed when every file of the version stands, else
handed on as a victims mark the next reclaim finishes (:func:`settle_forks`, run by every reclaim and by the branch
delete door).

Every function is blocking IO.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from opentelemetry import metrics
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from lakehouse import records, stored_objects
from lakehouse.fences import MARK_NAME, Hold, mark_name
from lakehouse.history_fences import FORK_MARK, VICTIMS_MARK, fence_directory
from lakehouse.storage_options import StorageOptions
from lakehouse.version_retention import branch_identity, recorded_branch


if TYPE_CHECKING:
    import lance


log = logging.getLogger(__name__)
_meter = metrics.get_meter("lance.app")
_ignored = _meter.create_counter(
    "lance.reclaim_marks.ignored",
    unit="{mark}",
    description=(
        "Cleanup marks ignored as no cleanup's: naming a ref incarnation that no longer stands, a version at or above its ref's head, one a tag "
        "pins, or more than a batch."
    ),
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


class _OfARef(BaseModel):
    """What every mark names: the table, and the incarnation of the ref whose versions it names."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The uuid of the table whose history the mark is in; a mark naming another is no mark of this table's.
    uuid: str
    #: The branch the versions are of, ``None`` for main, whichever way a caller spelled main.
    ref: str | None
    #: The uuid of the branch's incarnation (:func:`lakehouse.version_retention.branch_identity`), ``None`` for main: a
    #: mark of a branch deleted since, or of an earlier branch of the name, names no ref that stands.
    ref_id: str | None = None

    @field_validator("ref", mode="before")
    @classmethod
    def _one_main(cls, ref: object) -> str | None:
        return recorded_branch(ref)


class VictimsMark(_OfARef):
    """One cleanup call's mark: the versions of one ref of one table it will remove."""

    versions: list[int] = Field(min_length=1)
    #: The token of its writer's hold of the table's history: while that holder stands the writer may still delete, so no
    #: other reclaim judges the mark. ``None`` names no writer.
    holder: str | None = None


class ForkMark(_OfARef):
    """An erasure's mark of a version of one ref a branch was cut from, whose files its cleanup took."""

    version: int


class CleanupDone(BaseModel):
    """What one marked reclaim did."""

    #: The versions its marks named; empty when the dry run named none, and no cleanup ran.
    marked: list[int] = Field(default_factory=list)
    old_versions: int = 0
    bytes_removed: int = 0
    failed_deletes: int = 0
    #: The trusted victims marks of this ref still standing once it returned, its own among them: each names a version
    #: that may stand over deleted files, which no door may name until a later reclaim finishes it.
    marks_left: int = 0


def mark_key(table_uuid: str, mark_id: str, *, kind: str = VICTIMS_MARK) -> str:
    """The key, on the control root, of the mark ``(kind, mark_id)`` of the table ``table_uuid``."""
    return f"{fence_directory(table_uuid)}/{mark_name(kind, mark_id)}"


def _version_of(path: str) -> int | None:
    name = path.rsplit("/", 1)[-1]
    if (v2 := _V2_MANIFEST.fullmatch(name)) is not None:
        return _U64_MAX - int(v2.group(1))
    if (v1 := _V1_MANIFEST.fullmatch(name)) is not None:
        return int(v1.group(1))
    return None


class RefPins(BaseModel):
    """What a ref's cleanup keeps whatever it is asked: its head, and the versions a tag pins; the versions a branch was
    cut from, whose manifests it keeps for the branch; and every ref of the table, by the incarnation that stands."""

    model_config = ConfigDict(frozen=True)

    head: int
    tagged: frozenset[int]
    forked: frozenset[int]
    #: Every ref standing, main as ``None``, each with its incarnation's uuid (``None`` for main).
    refs: Mapping[str | None, str | None]


def _refs(listed: Mapping[str, Any]) -> dict[str | None, str | None]:
    return {None: None, **{str(name): branch_identity(entry) for name, entry in listed.items()}}


def _forks(listed: Mapping[str, Any], ref: str | None) -> frozenset[int]:
    return frozenset(
        int(version)
        for entry in listed.values()
        if recorded_branch((entry or {}).get("parent_branch")) == ref and isinstance(version := (entry or {}).get("parent_version"), int)
    )


def pins(dataset: lance.LanceDataset, ref: str | None) -> RefPins:
    """``ref``'s head (main for ``None``), read off any handle of the table, every version of it a tag pins (a tag on another
    branch's version of the same number pins nothing here), every version of it a branch was cut from, and the table's
    refs as one listing of its branches reads them.

    Raises:
        OSError: The store failed a read of the refs or the head.
    """
    ref = recorded_branch(ref)
    tagged = {int(tag["version"]) for tag in dataset.tags.list().values() if recorded_branch(tag.get("branch")) == ref}
    listed = dataset.branches.list()
    return RefPins(head=int(dataset.checkout_version((ref, None)).version), tagged=frozenset(tagged), forked=_forks(listed, ref), refs=_refs(listed))


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


def _create(control_root: str, storage_options: StorageOptions, key: str, mark: BaseModel) -> None:
    """Put ``mark`` at ``key`` unless a mark stands there already.

    Raises:
        OSError: The store refused the write.
    """
    try:
        records.create_json(control_root, storage_options, key, mark.model_dump(mode="json"))
    except records.RecordExistsError:
        return


def write(
    control_root: str, storage_options: StorageOptions, *, table_uuid: str, ref: str | None, ref_id: str | None, versions: list[int], holder: str | None
) -> str:
    """Mark ``versions`` of the incarnation ``ref_id`` of ``ref`` (main for ``None``) of the table ``table_uuid`` as a
    cleanup call's victims, written by the holder ``holder``; the mark's id.

    Raises:
        OSError: The store refused the write.
    """
    mark_id = uuid.uuid4().hex
    mark = VictimsMark(uuid=table_uuid, ref=ref, ref_id=ref_id, versions=sorted(set(versions)), holder=holder)
    _create(control_root, storage_options, mark_key(table_uuid, mark_id), mark)
    return mark_id


def remove(control_root: str, storage_options: StorageOptions, *, table_uuid: str, mark_id: str, kind: str = VICTIMS_MARK) -> None:
    """Remove the mark ``(kind, mark_id)``. Never raises: a mark left stands until a later reclaim removes it, and refuses
    only refs."""
    try:
        stored_objects.remove_object(control_root, storage_options, mark_key(table_uuid, mark_id, kind=kind))
    except Exception as exc:
        log.warning("reclaim_mark_not_removed", extra={"table_uuid": table_uuid, "mark": mark_id, "reason": f"{type(exc).__name__}: {exc}"[:300]})


def _read[M: _OfARef](
    control_root: str, storage_options: StorageOptions, *, table_uuid: str, marks: Iterable[tuple[str, str]], kind: str, model: type[M]
) -> dict[str, M]:
    """Each mark of ``kind`` among ``marks`` (``(kind, id)`` as a take of the table's history fence lists them), read by id.
    One that is gone, does not parse as ``model``, or names another table is no mark of this table's, and is logged and
    left out.

    Raises:
        OSError: The store failed a read.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    found: dict[str, M] = {}
    for listed_kind, mark_id in sorted(marks):
        if listed_kind != kind:
            continue
        key = mark_key(table_uuid, mark_id, kind=kind)
        try:
            stored = records.read_json_at(control_root, storage_options, key).stored
            mark = None if stored is None else model.model_validate(stored.record)
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


def read(control_root: str, storage_options: StorageOptions, *, table_uuid: str, marks: Iterable[tuple[str, str]]) -> dict[str, VictimsMark]:
    """Each victims mark among ``marks``, read by id (:func:`_read`).

    Raises:
        OSError: The store failed a read.
    """
    return _read(control_root, storage_options, table_uuid=table_uuid, marks=marks, kind=VICTIMS_MARK, model=VictimsMark)


def _of_a_standing_ref(mark: _OfARef, held: RefPins) -> bool:
    return mark.ref in held.refs and held.refs[mark.ref] == mark.ref_id


def trusted(marks: dict[str, VictimsMark], *, ref: str | None, held: RefPins) -> tuple[dict[str, VictimsMark], list[str]]:
    """The marks of ``ref`` (main for ``None``) a cleanup could have written, and the ids of those of the table no cleanup
    could have written as they stand: naming an incarnation of a ref that no longer stands (whichever ref it names), more
    than :data:`BATCH` versions, or one at or above the head, or one a tag pins. The others are logged and counted, and a
    door ignores them whole.
    """
    ref = recorded_branch(ref)
    kept: dict[str, VictimsMark] = {}
    ignored: list[str] = []
    for mark_id, mark in marks.items():
        if not _of_a_standing_ref(mark, held):
            log.warning("reclaim_mark_ignored", extra={"mark": mark_id, "ref": mark.ref, "reason": "names a ref incarnation that no longer stands"})
            _ignored.add(1)
            ignored.append(mark_id)
            continue
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


def _standing_forks(forks: dict[str, ForkMark], *, ref: str | None, held: RefPins) -> set[int]:
    """The versions of ``ref`` a fork mark of its standing incarnation names, below its head and pinned by no tag."""
    return {
        mark.version
        for mark in forks.values()
        if mark.ref == ref and _of_a_standing_ref(mark, held) and mark.version < held.head and mark.version not in held.tagged
    }


def refused(
    control_root: str, storage_options: StorageOptions, *, table_uuid: str, marks: Iterable[tuple[str, str]], dataset: lance.LanceDataset, ref: str | None
) -> set[int]:
    """The versions of ``ref`` a door holding the table's history as ref work refuses to pin or bring back: those a trusted
    mark among ``marks`` (its take's, :attr:`lakehouse.fences.Hold.marks`) names, a victims mark or a fork mark. Constant
    in the table's size: a read per mark, the ref's head, its tags and the table's branches.

    Raises:
        OSError: The store failed a read of a mark or of the ref.
    """
    marks = list(marks)
    standing = read(control_root, storage_options, table_uuid=table_uuid, marks=marks)
    forks = _read(control_root, storage_options, table_uuid=table_uuid, marks=marks, kind=FORK_MARK, model=ForkMark)
    if not standing and not forks:
        return set()
    held = pins(dataset, ref)
    kept, _ignored_ids = trusted(standing, ref=ref, held=held)
    return {version for mark in kept.values() for version in mark.versions} | _standing_forks(forks, ref=recorded_branch(ref), held=held)


def _listed(control_root: str, storage_options: StorageOptions, *, table_uuid: str) -> tuple[datetime, list[tuple[str, str, datetime]]]:
    """One listing of the table ``table_uuid``'s history fence directory: the store's clock at it, and each mark standing
    directly in it as ``(kind, id, stamped)``.

    Raises:
        OSError: The store failed the listing.
    """
    from botocore.exceptions import BotoCoreError, ClientError

    directory = fence_directory(table_uuid)
    try:
        listing = stored_objects.list_under_at(f"{control_root.rstrip('/')}/{directory}", storage_options)
    except (ClientError, BotoCoreError) as exc:
        raise OSError(f"the store did not answer listing {directory}: {exc}") from exc
    found: list[tuple[str, str, datetime]] = []
    for stored in listing.objects:
        relative = stored.path[stored.path.find(f"{directory}/") + len(directory) + 1 :]
        if (name := MARK_NAME.fullmatch(relative)) is not None and name.group(1) in (VICTIMS_MARK, FORK_MARK):
            found.append((name.group(1), name.group(2), stored.modified))
    return listing.at, found


def oldest(control_root: str, storage_options: StorageOptions, *, table_uuid: str) -> timedelta | None:
    """How long, on the store's clock, the oldest victims mark standing directly in the table ``table_uuid``'s history
    fence directory has stood; ``None`` for none. One listing, under a session that reads none of the fence's objects: what
    a pass asks before it vends (whether to finish a mark), and what the oldest mark's age reports. A fork mark is no
    reclaim left part-way, and is not counted.

    Raises:
        OSError: The store failed the listing.
    """
    at, found = _listed(control_root, storage_options, table_uuid=table_uuid)
    stamps = [stamped for kind, _id, stamped in found if kind == VICTIMS_MARK]
    return at - min(stamps) if stamps else None


def _untouched(dataset: lance.LanceDataset, versions: list[int]) -> bool:
    """Whether no cleanup took any file of ``versions``: Lance's dry run restricted to them still names each one's manifest
    and each one's transaction file.

    THE TRANSACTION FILES ARE THE WITNESS, not the files' existence. Measured on pylance 13.0.0 (ADR 0243 R4): the dry run
    names only files that still stand (a data file or a transaction file removed by hand drops out of its candidates), so
    asking whether the files it names stand answers yes for every version whose manifest stands, a half-removed one
    included; and one cleanup call deletes every transaction file of its versions before any data file, blob sidecar or
    manifest (ADR 0243 §3). So a version whose transaction file still stands lost nothing to a cleanup, and one whose
    transaction file is gone may have lost anything.

    Raises:
        OSError: The store failed a read.
    """
    explained = dataset.explain_cleanup_old_versions(
        older_than=timedelta(0), retain_versions=None, error_if_tagged_old_versions=False, include_files=True, max_files=_MOST_CANDIDATES, versions=versions
    )
    if explained.candidate_files_truncated or explained.stats.old_versions != len(versions):
        return False
    return sum(1 for candidate in explained.candidate_files if candidate.kind == "transaction") == len(versions)


def settle_forks(control_root: str, storage_options: StorageOptions, *, table_uuid: str, dataset: lance.LanceDataset, marks: Iterable[tuple[str, str]]) -> None:
    """End each fork mark among ``marks`` that no branch needs any more: one of a ref incarnation that no longer stands, or
    naming a version gone, is removed; one naming a version no branch is cut from any more is judged as a dead cleanup's
    mark is, removed when every file of the version still stands, else handed on as a victims mark of the same id, which
    the next reclaim of its ref finishes (and which a pass, whose credential cannot remove a fork mark, writes again
    harmlessly: it is put only where none stands). ``dataset`` is any handle of the table.

    Raises:
        OSError: The store failed a read, or refused the hand-over's write.
    """
    forks = _read(control_root, storage_options, table_uuid=table_uuid, marks=marks, kind=FORK_MARK, model=ForkMark)
    if not forks:
        return
    listed = dataset.branches.list()
    refs = _refs(listed)
    for mark_id, mark in forks.items():
        if mark.ref not in refs or refs[mark.ref] != mark.ref_id:
            remove(control_root, storage_options, table_uuid=table_uuid, mark_id=mark_id, kind=FORK_MARK)
            continue
        handle = dataset.checkout_version((mark.ref, None))
        if mark.version not in {int(entry["version"]) for entry in handle.versions()}:
            remove(control_root, storage_options, table_uuid=table_uuid, mark_id=mark_id, kind=FORK_MARK)
            continue
        if mark.version in _forks(listed, mark.ref):
            continue
        if not _untouched(handle, [mark.version]):
            handed = VictimsMark(uuid=table_uuid, ref=mark.ref, ref_id=mark.ref_id, versions=[mark.version])
            _create(control_root, storage_options, mark_key(table_uuid, mark_id), handed)
            log.info("reclaim_fork_mark_handed_on", extra={"table_uuid": table_uuid, "mark": mark_id, "ref": mark.ref, "version": mark.version})
        remove(control_root, storage_options, table_uuid=table_uuid, mark_id=mark_id, kind=FORK_MARK)


def ended(control_root: str, storage_options: StorageOptions, *, table_uuid: str, dataset: lance.LanceDataset, removed: Mapping[str, str | None]) -> None:
    """End the marks of the branches ``removed`` (each name with the incarnation deleted), as the branch delete door
    removes them: their versions went with them. Then every fork mark no branch needs any more (:func:`settle_forks`).
    Never raises: a mark left names an incarnation that no longer stands, which no door trusts and the next reclaim of the
    table removes.
    """
    try:
        _at, found = _listed(control_root, storage_options, table_uuid=table_uuid)
        marks = [(kind, mark_id) for kind, mark_id, _stamped in found]
        for kind, model in ((VICTIMS_MARK, VictimsMark), (FORK_MARK, ForkMark)):
            for mark_id, mark in _read(control_root, storage_options, table_uuid=table_uuid, marks=marks, kind=kind, model=model).items():
                if mark.ref is not None and mark.ref in removed and removed[mark.ref] == mark.ref_id:
                    remove(control_root, storage_options, table_uuid=table_uuid, mark_id=mark_id, kind=kind)
        settle_forks(control_root, storage_options, table_uuid=table_uuid, dataset=dataset, marks=marks)
    except Exception as exc:
        log.warning("reclaim_marks_not_ended", extra={"table_uuid": table_uuid, "branches": sorted(removed), "reason": f"{type(exc).__name__}: {exc}"[:300]})


class Reclaim(BaseModel):
    """Where a holder of a table's history reclaims one ref of it: the control root its marks are kept on and the
    credential that writes them, its hold, what it asks before each batch, and what each batch deletes through."""

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
    #: Called FIRST in each batch, before ``before_each``: a handle of this ref opened under a credential minted for that
    #: batch, which dies no later than the holds ``before_each`` then renews (STS issues none shorter than the hold:
    #: RustFS 1.0.3, measured). ``None`` deletes through the handle :meth:`run` is given.
    session: Callable[[], Any] | None = None

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
        """``cleanup_old_versions`` on ``dataset`` (a handle of this ref), marked: every mark of a ref incarnation that no
        longer stands removed and every fork mark no branch needs settled (:func:`settle_forks`), each standing mark of
        the ref judged, then the victims named (``versions``, else :func:`victims` under ``older_than`` and
        ``retain_versions``) and cleaned up a batch at a time: the batch's handle opened (:attr:`session`), its holds
        confirmed (``before_each``), its mark written, and the call; the mark is removed after a call with no failed
        delete, and a call with one stops the reclaim, its mark standing.

        ``forks`` names beside them every version of the ref a branch was cut from that no tag pins, which an erasure must
        take a subject's bytes from, each marked with a fork mark before the batch that names it (unless one stands), and
        left standing (the module's AN ERASURE'S FORK POINTS).

        Raises:
            OSError: The store refused a mark's read or write, or Lance raised: nothing more is deleted, and a batch's mark,
                once written, stands.
            Exception: Whatever ``session`` or ``before_each`` raises; no mark is written for the batch it stopped.
        """
        marks = list(self.hold.marks)
        standing = read(self.control_root, self.storage_options, table_uuid=self.table_uuid, marks=marks)
        held = pins(dataset, self.ref)
        mine, ignored = trusted(standing, ref=self.ref, held=held)
        for mark_id in ignored:
            remove(self.control_root, self.storage_options, table_uuid=self.table_uuid, mark_id=mark_id)
        settle_forks(self.control_root, self.storage_options, table_uuid=self.table_uuid, dataset=dataset, marks=marks)
        still = {int(entry["version"]) for entry in dataset.versions()}
        left = self._judged(dataset, mine, still)
        named = set(versions) if versions is not None else set(victims(dataset, older_than=older_than, retain_versions=retain_versions, ref=self.ref))
        forked = {version for version in held.forked if version < held.head and version not in held.tagged} if forks else set()
        marked_forks = _standing_forks(
            _read(self.control_root, self.storage_options, table_uuid=self.table_uuid, marks=marks, kind=FORK_MARK, model=ForkMark), ref=self.ref, held=held
        )
        done = CleanupDone(marked=sorted(named | forked))
        ref_id = held.refs.get(self.ref)
        for start in range(0, len(done.marked), BATCH):
            batch = done.marked[start : start + BATCH]
            handle = self.session() if self.session is not None else dataset
            if self.before_each is not None:
                self.before_each()
            for version in sorted(set(batch) & forked - marked_forks):
                fork = ForkMark(uuid=self.table_uuid, ref=self.ref, ref_id=ref_id, version=version)
                _create(self.control_root, self.storage_options, mark_key(self.table_uuid, uuid.uuid4().hex, kind=FORK_MARK), fork)
            mark_id = write(
                self.control_root, self.storage_options, table_uuid=self.table_uuid, ref=self.ref, ref_id=ref_id, versions=batch, holder=self.hold.token
            )
            stats: Any = handle.cleanup_old_versions(
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
                done.marks_left = len(left) + 1
                return done
            remove(self.control_root, self.storage_options, table_uuid=self.table_uuid, mark_id=mark_id)
        still = {int(entry["version"]) for entry in dataset.versions()}
        for other_id, other in left.items():
            if set(other.versions) & still:
                done.marks_left += 1
            else:
                remove(self.control_root, self.storage_options, table_uuid=self.table_uuid, mark_id=other_id)
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
            if not live and (not standing or _untouched(dataset, standing)):
                log.info("reclaim_mark_judged_whole", extra={"table_uuid": self.table_uuid, "mark": mark_id, "versions": standing[:20]})
                remove(self.control_root, self.storage_options, table_uuid=self.table_uuid, mark_id=mark_id)
                continue
            left[mark_id] = mark
        return left
