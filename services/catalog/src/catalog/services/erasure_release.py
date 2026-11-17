"""The release of what pins an erasure's residual, a grace period after the people it costs were told ([[LH-178]]).

ADR 0138 (owner decision D4): when an erasure finds a tag or a branch keeping a version that holds the subject
(``ErasureReport.pinned_by``), the requester and the table's owners are told, and when the grace period ends
every pinning tag is deleted and every pinning branch, with its descendants, is deleted and recreated at main's
head. There is no door that cancels or extends it: an erasure that can be blocked is not an erasure.

**THE RELEASE IS DURABLE STATE WITH A DUE TIME, not a timer.** One record per release on the control root
(``_erasure_releases/<id>.json``), written before anyone is told, read by the tick that releases what is due.
A restart loses nothing, and a tick that comes late releases late, which costs only time.

**WHERE EACH BRANCH COMES BACK IS RECORDED BEFORE ANY BRANCH GOES.** A branch has no update operation, so its
only release is delete-and-recreate (``lance_docs/ns_catalog/namespace/operations/index.md``), and the two
cannot be one commit. The tick that claims a release writes the plan, each branch with the ``(parent,
version)`` it is recreated at, before it deletes anything, and moves the record to ``recreating`` once the
deletes are done. A tick that dies anywhere is finished by a later one from the record, at the recorded
reference, whatever main has done since.

**THE SUBTREE IS READ AGAIN WHEN THE RELEASE RUNS, and on every resumed tick until its deletes are done.** Lance refuses to delete a branch another branch is cut
from or a tag names (measured on pylance 12.0.0, ``prototypes/lance-erasure-and-commit-door/a1_child_branches.py`` on branch ``prototype/lance-erasure-and-commit-door``),
so a branch cut from a pinning branch during the grace period goes with it, as do the tags on any branch of
the subtree. Leaf-first deletes, top-down recreates: each pinning root from main's head, each descendant from
its recreated parent (``a1b_child_branch_release.py``). The descendants' own commits are lost; their owners had
the grace period to copy them out (owner, 2026-10-10).

**AND EVERY REF STANDING ON A RESIDUAL GOES TOO, read again on every tick, whoever made it** (ADR 0233). The record
keeps the versions the verification found holding the subject (``residual``). A tag another caller put on one of them
during the grace period, or a branch cut from one, would keep the subject for good once the refs the verification named
are gone, so each is taken with them: the tag deleted, the branch deleted and recreated at main's head with its subtree.
A tick resumed after its deletes takes only what stands on a residual since, which a branch it recreated never does.

**WHAT IS TAKEN IS WHAT STILL PINS, BY ONE PREDICATE** (:func:`_taken`, G8 of ``specs/catalog/Erasure.tla``). A ref is
taken while it stands on a residual, or where the verification found a pin (``pinned_at``), or in a taken subtree, at the
tick's own read of the table, whatever its name: the tick's plan, its deletes and every ref door (:func:`takes`) ask the
same question, so a door never tells its caller less than the tick takes. A tag the release named, moved by its owner
onto a clean version in the grace, or a branch deleted and cut again from clean history after a tick that died recorded
it, pins nothing and stays; the residual it no longer holds is reclaimed all the same. Taking a recorded name wherever it
stands would take that ref from a caller no door told (ErasureNameTaking, ADR 0233 §9).

**BUT NEVER A REGISTERED CLONE'S TAG** (ADR 0198 §1, ADR 0233). A clone registered during the grace period reads its
version through this table's files, held by the catalog's own tag (:data:`lakehouse.clone_sources.TAG_PREFIX`), which only
the clone's end releases. While one stands on a residual, or on a branch the release would delete, the tick changes
nothing: it records the clones on the release (``held_by_clones``), names each on the obligation as a verification names
one (:func:`catalog.services.erasure.held_by_a_clone`), and tells the release's people again, each notice naming the
clones, under an id fixed by the release, the person and the clones (:func:`held_notice`). The release runs on the first
tick after the last such clone ends, however long past its due time: the subject stays readable through the clone's
version until then, and a clone is never broken in silence (owner, 2026-11-17: "Wait and report", ADR 0233 §9).

**THEN THE RELEASE RECLAIMS WHAT THE REFS KEPT, under the same hold** (ADR 0233). Once nothing pins a residual, main's
cleanup at the obligation's retention (``retain_days``, whose window the verification already found the residuals
outside) deletes it, as the verification's own reclaim does. The table's history is held as a release from the plan to
the reclaim (:attr:`lakehouse.history_fences.HistoryWork.RELEASE`), which no ref write shares: a tag written after the
release's last read of the refs and before the reclaim would otherwise keep a residual, and one written after the
reclaim finds no residual left to name.

**NOBODY IS TOLD TWICE AND NOBODY IS LEFT UNTOLD** (ADR 0233). The verification files the release's notices before it
writes the record (:mod:`catalog.services.announcement_intents`, on :func:`notice_target`), each notice under an id fixed
by the release and the person (:func:`notice`), so whoever sends it again sends one event: the verification itself, the
next verification that meets the record, or the catalog's finish, which maintenance's hourly audit asks for. Each lands
only while the record it was built from stands, its ``due_at`` included, so no copy of an id names another deadline. A tick
leaves a release alone while a filing of its notices stands, so it never runs before everyone it costs is told.

**AND WHOEVER PUTS A REF THE RELEASE WILL TAKE IS TOLD AT THE REF'S WRITE** (owner, 2026-11-17: "Tell the creator", ADR
0233 §9). A tag or a branch put on a residual, or a branch cut from one the release takes, during the grace period loses
its work to the release. The verification writes the release's mark in the table's history fence (:func:`mark_key`) and then
its record, while it holds the table's history as a scheduling (:attr:`HistoryWork.SCHEDULE`), which no ref write
shares (:func:`schedule`). Every ref door holds the table's history, and its take lists the fence's directory: a door
whose listing shows a mark reads that release's record (:class:`RefNotices`), judges whether the release takes its ref
as the release's next tick will read the table (:func:`takes`), and when it does files the creator's notice on the
release's own target before the ref, under an id fixed by the release, the person and the ref (:func:`creator_notice`),
landed once the ref and the record with its ``due_at`` stand. The door sends it after the ref, the finish sends what a
door that ended filed, and the tick leaves the release alone while it is filed, as it does every notice. A table with no
release scheduled carries no mark, and its ref writes read nothing more. A registered clone's tag is never taken
(below), so its register files nothing, and its owner hears of the release only as its people do.

**ONE CLAIM PER RELEASE ACROSS REPLICAS.** A tick that outlasts its window can overlap the next on another replica, so
a tick claims a record by an ETag-guarded write (:func:`lakehouse.records.mutate_json`) carrying a lease; a replica that
loses the write, or finds the lease live, leaves the record alone.

**THE PREDICATE IS NEVER STORED.** It names the subject, and a record on the control root would be one more
place the identifier is written ([[LH-281]]). The release needs only the refs, which ``pinned_by`` names.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field

from catalog.services import announcement_intents, erasure_obligations
from catalog.services.announcement_intents import Announcement, ControlMessage, RecordHeld, RefOnARelease
from catalog.services.dataplane import PlacedRef, recorded_branch
from catalog.services.erasure import ErasureReport, Pin, SurfaceResult, held_by_a_clone, reclaim_refs
from lakehouse import clone_sources, control_prefixes, history_fences, stored_objects
from lakehouse.control_events import CatalogControlEvent
from lakehouse.fences import mark_name
from lakehouse.governed import fga
from lakehouse.history_fences import HistoryFencedError, HistoryWork, TableHistory
from lakehouse.record_store import delete_record, list_records, put_record
from lakehouse.records import RecordChangedError, RecordMissingError, create_json, mutate_json, read_json
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

RELEASE_PREFIX = control_prefixes.ERASURE_RELEASES

#: How long a claimed release stays a tick's own before another may resume it. Longer than a release takes
#: (a handful of ref deletes and creates), so a slow tick is not overtaken; a tick that fails hands its lease
#: back at once, so this bounds only a replica that died holding one.
LEASE = timedelta(minutes=15)

#: The kind of a release's mark in its table's history fence (:func:`lakehouse.fences.mark_name`), the one the fence hands.
MARK_KIND: Final = history_fences.RELEASE_MARK

#: How many takes of the table's history a scheduling makes, a short wait between each, before it answers the release
#: unrecorded: a ref write holds it for the seconds its write takes.
_SCHEDULE_TAKES: Final = 5
_SCHEDULE_WAIT: Final = 0.2


type RefAction = Literal["table_tag_deleted", "table_branch_deleted", "table_branch_created"]


class Recreate(BaseModel):
    """Where one branch of a released subtree comes back."""

    model_config = ConfigDict(extra="forbid")

    branch: str
    #: The branch it is recreated from, or None for main.
    parent: str | None
    version: int


class ReleaseRecord(BaseModel):
    """One scheduled release, as it lives on the control root."""

    model_config = ConfigDict(extra="forbid")

    release_id: str
    #: The table's name when the erasure was asked of it, for the log.
    table: str
    #: The table's uuid (ADR 0173 §8): the events name the table by it, and by its name when the release runs.
    table_uuid: str
    #: The dataset root the erasure ran on: the release acts on these bytes whatever the id resolves to later.
    location: str
    requested_by: str | None
    notified: list[str]
    #: ``pinned_by``'s refs as the erasure named them: tags, then branches deepest first.
    refs: list[str]
    due_at: datetime
    phase: Literal["pending", "deleting", "recreating"] = "pending"
    lease_until: datetime | None = None
    #: The versions the verification found holding the subject, as ``ErasureReport.residual_versions`` names them
    #: (``<ref>@<version>``, ``main`` for main): every ref standing on one goes with the release.
    residual: list[str] = Field(default_factory=list)
    #: Where each of ``refs`` stood when the verification named it (``Pin.holds``, ``<ref>@<version>``): until the deletes
    #: are done a ref still standing on one of these is taken with the residual's, a recorded name standing elsewhere not.
    pinned_at: list[str] = Field(default_factory=list)
    #: The obligation's retention, in days: the reclaim after the deletes keeps what is younger.
    retain_days: int = 0
    #: Recorded by the claiming tick before any delete: the tags it deletes, and the branches top-down.
    tags: list[str] = Field(default_factory=list)
    plan: list[Recreate] = Field(default_factory=list)
    #: The registered clones' tags the last tick found on a residual, which hold the release until each clone ends.
    held_by_clones: list[str] = Field(default_factory=list)
    last_error: str = ""

    def change(self, action: RefAction, extra: dict[str, Any]) -> RefChange:
        """The event-to-be of one ref this release changed on its table."""
        return RefChange(action=action, table=self.table, table_uuid=self.table_uuid, location=self.location, extra=extra)


class RefChange(BaseModel):
    """One ref the release changed, for the untargeted ref-plane event the matching door would emit."""

    action: RefAction
    #: The release's table, as it was named when the release was scheduled; the tick names it as it is named now.
    table: str
    table_uuid: str
    location: str
    extra: dict[str, Any]


class _Dataset(Protocol):
    """The pylance surface a release uses."""

    @property
    def uri(self) -> str: ...
    @property
    def _ds(self) -> Any: ...
    @property
    def version(self) -> int: ...
    @property
    def branches(self) -> Any: ...
    @property
    def tags(self) -> Any: ...
    def create_branch(self, branch: str, reference: Any = None, storage_options: Any = None, **kwargs: Any) -> Any: ...
    def checkout_latest(self) -> None: ...
    def checkout_version(self, version: Any, /) -> Any: ...
    def cleanup_old_versions(self, older_than: timedelta | None = None, *, error_if_tagged_old_versions: bool = True) -> Any: ...


def record_key(release_id: str) -> str:
    return f"{RELEASE_PREFIX}/{release_id}.json"


def new_release(
    pins: Sequence[Pin],
    *,
    release_id: str,
    table: str,
    table_uuid: str,
    location: str,
    requested_by: str | None,
    notified: list[str],
    due_at: datetime,
    residual: Sequence[str],
    retain_days: int,
) -> ReleaseRecord:
    """The release of everything ``pins`` names, and of every ref standing on a ``residual`` version, under ``release_id``:
    the erasure obligation's own id, so a second verification that settles the same obligation meets the first one's
    record rather than scheduling it again."""
    return ReleaseRecord(
        release_id=release_id,
        table=table,
        table_uuid=table_uuid,
        location=location,
        requested_by=requested_by,
        notified=notified,
        refs=[pin.ref for pin in pins],
        pinned_at=sorted({pin.holds for pin in pins}),
        due_at=due_at,
        residual=list(residual),
        retain_days=retain_days,
    )


def notice_target(release_id: str) -> str:
    """The object the release's notices are filed on (:mod:`catalog.services.announcement_intents`)."""
    return f"erasure_release:{release_id}"


def notice(record: ReleaseRecord, subject: str, *, object_id: str) -> Announcement:
    """The notice telling ``subject`` of ``record``, under an id fixed by the release and the person, so every request that
    sends it sends one event, and landed only while ``record`` itself stands: a record under the release's id naming another
    deadline (one a verification that lost the record's create meant to write) lands none of it (ADR 0233)."""
    stood = RecordHeld(key=record_key(record.release_id), fields={"due_at": record.model_dump(mode="json")["due_at"]})
    return Announcement(evidence=stood, message=ControlMessage(event=_scheduled(record, subject, object_id=object_id)))


def held_notice(record: ReleaseRecord, subject: str, clones: Sequence[str]) -> CatalogControlEvent:
    """The notice telling ``subject`` that ``record`` is held by the registered clones ``clones`` read through: under an id
    fixed by the release, the person and the clones, so every tick that finds them holding it sends one event."""
    event = _scheduled(record, subject, object_id=fga.table_object(record.table_uuid))
    return event.model_copy(
        update={
            "event_id": uuid5(NAMESPACE_URL, f"erasure_release_held/{record.release_id}/{subject}/{','.join(sorted(clones))}").hex,
            "extra": {**event.extra, "held_by_clones": sorted(clones)},
        }
    )


def _scheduled(record: ReleaseRecord, subject: str, *, object_id: str) -> CatalogControlEvent:
    return CatalogControlEvent(
        event_id=uuid5(NAMESPACE_URL, f"erasure_release_scheduled/{record.release_id}/{subject}").hex,
        action="erasure_release_scheduled",
        object_type="table",
        object_id=object_id,
        object_name=record.table,
        actor=record.requested_by,
        extra={"subject": subject, "release_id": record.release_id, "refs": record.refs, "due_at": record.due_at.isoformat()},
    )


def mark_key(table_uuid: str, release_id: str) -> str:
    """The key of the mark the release ``release_id`` keeps in its table's history fence while it is scheduled."""
    return f"{history_fences.fence_directory(table_uuid)}/{mark_name(MARK_KIND, release_id)}"


def schedule(control_root: str, storage_options: StorageOptions, record: ReleaseRecord) -> None:
    """Write ``record``'s mark in its table's history fence, then ``record`` where the release tick finds it, both while
    the table's history is held as a scheduling, which no ref write shares: a ref write either ended before the record
    stood, or its take listed the mark (G7 of ``specs/catalog/Erasure.tla``). The hold is confirmed before the record, so a
    scheduling whose hold lapsed records nothing.

    Raises:
        RecordExistsError: A release under the same id is already scheduled; its mark stands, the same as this one's.
        HistoryFencedError: Ref writes held the table's history at every take, or the hold lapsed; nothing is recorded.
    """
    history = TableHistory(control_root=control_root, storage_options=storage_options, table_uuid=record.table_uuid)
    for attempt in range(_SCHEDULE_TAKES):
        try:
            with history.hold_for(HistoryWork.SCHEDULE, renewed=False) as hold:
                put_record(control_root, storage_options, mark_key(record.table_uuid, record.release_id), {"release_id": record.release_id})
                hold.confirm()
                create_json(control_root, storage_options, record_key(record.release_id), record.model_dump(mode="json"))
                return
        except HistoryFencedError:
            if attempt + 1 == _SCHEDULE_TAKES:
                raise
            time.sleep(_SCHEDULE_WAIT * (attempt + 1))


def scheduled(control_root: str, storage_options: StorageOptions, release_id: str) -> ReleaseRecord | None:
    """The release scheduled under ``release_id``, or None when none is (never scheduled, or already run)."""
    found = read_json(control_root, storage_options, record_key(release_id))
    return None if found is None else ReleaseRecord.model_validate(found[0])


def takes(record: ReleaseRecord, dataset: _Dataset, ref: PlacedRef) -> bool:
    """Whether ``record``'s release takes ``ref`` once it stands on ``dataset``'s table, as its next tick reads the table:
    :func:`_taken` over the table with ``ref`` put where the door puts it, the one predicate the tick's plan and deletes run
    (G8 of ``specs/catalog/Erasure.tla``). Never a registered clone's tag, which only the clone's end releases."""
    if ref.kind == "tag" and ref.name.startswith(clone_sources.TAG_PREFIX):
        return False
    taken = _taken(dataset, record, placing=ref)
    return ref.name in (taken.tags if ref.kind == "tag" else taken.branches)


def creator_notice(record: ReleaseRecord, subject: str, ref: PlacedRef, *, location: str, object_id: str, not_before: datetime) -> Announcement:
    """The notice telling ``subject``, who puts ``ref`` on the table, that ``record``'s release will take it: under an id fixed
    by the release, the person and the ref, so a retry, the door and the finish send one event, and landed once the ref
    stands where it was put, written no earlier than ``not_before`` (the door's clock as it files), and ``record`` itself
    stands, its ``due_at`` included (ADR 0233 §9)."""
    event = _scheduled(record, subject, object_id=object_id)
    told = event.model_copy(
        update={
            "event_id": uuid5(NAMESPACE_URL, f"erasure_release_scheduled/{record.release_id}/{subject}/{ref.kind}:{ref.name}").hex,
            "extra": {**event.extra, "ref": f"{ref.kind}:{ref.name}", "on": f"{ref.branch or 'main'}@{ref.version}"},
        }
    )
    landed = RefOnARelease(
        key=record_key(record.release_id),
        due_at=record.model_dump(mode="json")["due_at"],
        table_uuid=record.table_uuid,
        location=location,
        ref=ref.kind,
        name=ref.name,
        branch=ref.branch,
        version=ref.version,
        not_before=not_before,
    )
    return Announcement(evidence=landed, message=ControlMessage(event=told))


class RefNotices:
    """What one ref door files for the person who puts its ref, from inside its hold of the table's history: for each
    release the hold's take found marked and whose record stands, the creator's notice, when that release takes the ref.

    Each filing is made before the ref is written, and kept in :attr:`filed` for the door to send once the ref is written,
    or to judge when the write failed.
    """

    def __init__(self, control_root: str, storage_options: StorageOptions, *, table_uuid: str, subject: str, object_id: str) -> None:
        self._control_root, self._storage_options = control_root, storage_options
        self._table_uuid, self._subject, self._object_id = table_uuid, subject, object_id
        #: Each filing's key and what it holds.
        self.filed: list[tuple[str, list[Announcement]]] = []

    def __call__(self, marks: frozenset[tuple[str, str]], dataset: _Dataset, ref: PlacedRef) -> None:
        """File the notice of each marked release that takes ``ref``.

        One read of each marked release's record, and nothing for a table none is marked on. A mark whose record is gone
        is removed: its release has run, and no scheduling holds the table's history beside this door's hold, so no
        record is about to be written under it.

        Raises:
            OSError: The store refused a read or a filing; nothing is filed for the release it was reading, and the ref is
                not written.
        """
        for kind, release_id in sorted(marks):
            if kind != MARK_KIND:
                continue
            record = scheduled(self._control_root, self._storage_options, release_id)
            if record is None:
                stored_objects.remove_object(self._control_root, self._storage_options, mark_key(self._table_uuid, release_id))
                continue
            if record.table_uuid != self._table_uuid or not takes(record, dataset, ref):
                continue
            filed_at = datetime.now(UTC)
            told = [creator_notice(record, self._subject, ref, location=str(dataset.uri), object_id=self._object_id, not_before=filed_at)]
            key = announcement_intents.file(
                self._control_root, self._storage_options, door="erasure_release_creator", target=notice_target(release_id), announcements=told
            )
            self.filed.append((key, told))
            log.info("erasure_release_creator_filed", extra={"release": release_id, "table": record.table, "ref": f"{ref.kind}:{ref.name}"})


def completion_bound(due_at: datetime, cleanup_window_days: int) -> str:
    """When the erased bytes can leave storage, in words: the release is the first of four conditions (ADR 0138)."""
    return (
        f"the subject's bytes leave storage only after all of: the pins named in refs, and every ref standing on a residual version since, "
        f"are released at {due_at.isoformat()} and the versions they kept are reclaimed then, outside the obligation's retention; "
        "compaction rewrites the fragments that hold the subject, since a delete leaves its rows in their data files behind a deletion "
        "file; every index over an erased column is rebuilt from the rewritten data; and a version holding the subject that the release "
        f"did not reclaim is superseded and older than the maintenance cleanup window ({cleanup_window_days} days, or the table's "
        "retention_days policy). Until the release runs, restoring a retained pre-erasure version brings the rows back"
    )


class Released(BaseModel):
    """What one tick did: the refs it changed, and the notices of each release a registered clone holds."""

    changes: list[RefChange] = Field(default_factory=list)
    notices: list[CatalogControlEvent] = Field(default_factory=list)


def release_due(
    control_root: str, storage_options: StorageOptions, *, now: datetime, open_location: Callable[[str], _Dataset], table_options: StorageOptions
) -> Released:
    """Release every scheduled release that is due and not held by another tick; the refs it changed, and the notices of
    the releases registered clones hold.

    One release failing records its error, hands its lease back and leaves the others to run. One a reclaim of its table
    holds off ([[LH-308]]) is left unclaimed for a later tick, as is one whose notices are still filed (ADR 0233): its
    people are told first; and one a registered clone holds changes nothing, and is told again. ``table_options`` reach
    the tables' store, where the reclaim records what it removes.
    """
    done = Released()
    for raw in list_records(control_root, storage_options, RELEASE_PREFIX, event="erasure_release"):
        try:
            record = ReleaseRecord.model_validate(raw)
        except ValueError as exc:
            log.error("erasure_release_unreadable", extra={"release": str(raw.get("release_id", ""))[:64], "error": str(exc)})
            continue
        if not _claimable(record, now):
            continue
        try:
            if announcement_intents.standing(control_root, storage_options, target=notice_target(record.release_id))[0]:
                log.info("erasure_release_waits_for_its_notices", extra={"release": record.release_id, "table": record.table})
                continue
            if clones := _release(control_root, storage_options, record, now=now, open_location=open_location, table_options=table_options, done=done):
                done.notices += [held_notice(record, subject, clones) for subject in record.notified]
        except _Lost:
            log.info("erasure_release_claimed_elsewhere", extra={"release": record.release_id})
        except HistoryFencedError as exc:
            log.info("erasure_release_waits_for_a_reclaim", extra={"release": record.release_id, "table": record.table, "reason": str(exc)})
        except Exception as exc:  # noqa: BLE001 — one release's failure must not hold up the others
            log.warning("erasure_release_failed", extra={"release": record.release_id, "table": record.table, "error": str(exc)})
            _hand_back(control_root, storage_options, record.release_id, now=now, error=str(exc))
    return done


class _Lost(Exception):
    """Another tick holds the release, or finished it."""


def _claimable(record: ReleaseRecord, now: datetime) -> bool:
    return record.due_at <= now and (record.lease_until is None or record.lease_until <= now)


def _release(
    control_root: str,
    storage_options: StorageOptions,
    record: ReleaseRecord,
    *,
    now: datetime,
    open_location: Callable[[str], _Dataset],
    table_options: StorageOptions,
    done: Released,
) -> list[str]:
    """Run ``record``'s release, adding the refs it changed to ``done``; the registered clones' tags that hold it, which
    leave everything as it was."""
    dataset = open_location(record.location)
    # [[LH-308]] Held before the release is claimed, so a tick a reclaim holds off leaves it as it was: a branch is
    # recreated at the version the release recorded, which a reclaim reading the refs before it would delete. Held as a
    # release (ADR 0233), which no other work on the table's history shares: no ref is written between this tick's read
    # of the refs and its reclaim, and no create through the door lands a name nesting with one this recreates ([[LH-413]]).
    history = TableHistory(control_root=control_root, storage_options=storage_options, table_uuid=record.table_uuid)
    with history.held(HistoryWork.RELEASE):
        tags, plan, clones = _plan(dataset, record)
        if clones:
            # G4: a registered clone reads a version this release would let go of; only its own end releases its tag.
            _held_by_clones(control_root, storage_options, record, clones)
            return clones
        claimed = _mutate(control_root, storage_options, record.release_id, lambda current: _claim(current, now=now, tags=tags, plan=plan))
        # G8: only what the plan read just now takes is deleted, of what the claim recorded where to recreate, so a ref its
        # owner moved off the residual, or cut again from clean history, since a tick that died recorded it stays.
        taken_now = {step.branch for step in plan}
        done.changes += _delete(dataset, claimed, tags=tags, plan=[step for step in claimed.plan if step.branch in taken_now])
        if claimed.phase == "deleting":
            claimed = _mutate(control_root, storage_options, record.release_id, lambda current: {**current, "phase": "recreating"})
        done.changes += _recreate(dataset, claimed)
        _reclaim(dataset, claimed, storage_options=table_options)
        if record.held_by_clones:
            _report_held(control_root, storage_options, record.release_id, [])
        # G9: the end inside the hold, which a scheduling shares with nothing: a verification recording the release
        # again cannot land between the record's delete and the mark's, which would remove its new mark.
        _finish(control_root, storage_options, record)
    log.info(
        "erasure_release_done",
        extra={"release": record.release_id, "table": record.table, "tags": claimed.tags, "branches": [step.branch for step in claimed.plan]},
    )
    return []


def _held_by_clones(control_root: str, storage_options: StorageOptions, record: ReleaseRecord, clones: list[str]) -> None:
    """Record on the release, and on its obligation, the registered clones that hold it; the release keeps its phase and
    no lease, so the next tick reads the table again."""
    detail = f"held by the registered clones' tags {clones}: each clone reads its version through this table's files, so the release waits for their ends"
    log.warning("erasure_release_held_by_clones", extra={"release": record.release_id, "table": record.table, "clones": clones})
    _mutate(control_root, storage_options, record.release_id, lambda current: {**current, "held_by_clones": clones, "last_error": detail})
    _report_held(control_root, storage_options, record.release_id, [held_by_a_clone(tag) for tag in clones])


def _report_held(control_root: str, storage_options: StorageOptions, release_id: str, surfaces: list[SurfaceResult]) -> None:
    """Name what holds the release on its obligation, the release's own id; an obligation no longer recorded names
    nothing, and the release goes on as recorded."""
    try:
        erasure_obligations.release_held(control_root, storage_options, release_id, surfaces=surfaces)
    except RecordMissingError:
        log.warning("erasure_release_obligation_unrecorded", extra={"release": release_id})


class _Taken(BaseModel):
    """What a release takes of a table as it stands (:func:`_taken`)."""

    #: The tags it deletes, never a registered clone's.
    tags: set[str]
    #: The registered clones' tags standing where it would take a tag, which hold it.
    clones: set[str]
    #: The branches it deletes and recreates.
    branches: set[str]
    #: Each listed branch's parent, ``None`` for main.
    forks: dict[str, str | None]


def _taken(dataset: _Dataset, record: ReleaseRecord, *, placing: PlacedRef | None = None) -> _Taken:
    """THE ONE PREDICATE of what ``record``'s release takes of the table as it stands now, with ``placing`` put where its
    door puts it: the tick's plan and its deletes (:func:`_plan`) and every ref door (:func:`takes`) ask this, so the door
    and the tick never disagree (G8 of ``specs/catalog/Erasure.tla``).

    A ref is taken while it still pins what the erasure left, whatever its name: a tag on one of the release's places, a
    branch cut from one, with every branch cut from a branch it takes, and a tag on any of those. The places are the
    residual versions and, until the release's deletes are done, the versions its pins stood on when the verification named
    them (``pinned_at``); once they are done a recreated branch stands at main's head, which pins nothing. A name the
    release recorded, its pins' or its claim's, is taken only where it still stands so: a tag its owner moved onto a clean
    version, or a branch deleted and cut again from clean history, pins nothing and is left alone.
    """
    listed_tags = dict(dataset.tags.list() or {})
    listed_branches = dict(dataset.branches.list() or {})
    if placing is not None and placing.kind == "tag":
        listed_tags[placing.name] = {"branch": placing.branch, "version": placing.version}
    elif placing is not None:
        listed_branches[placing.name] = {"parent_branch": placing.branch, "parent_version": placing.version}
    places = {_reference(name) for name in record.residual}
    if record.phase != "recreating":
        places |= {_reference(name) for name in record.pinned_at}
    forks = {str(name): recorded_branch(meta.get("parent_branch")) for name, meta in listed_branches.items()}
    branches = {str(name) for name, meta in listed_branches.items() if (recorded_branch(meta.get("parent_branch")), meta.get("parent_version")) in places}
    frontier = list(branches)
    while frontier:
        parent = frontier.pop()
        children = {name for name, fork in forks.items() if fork == parent} - branches
        branches |= children
        frontier.extend(children)
    on = {
        str(name)
        for name, meta in listed_tags.items()
        if recorded_branch(meta.get("branch")) in branches or (recorded_branch(meta.get("branch")), meta.get("version")) in places
    }
    clones = {tag for tag in on if tag.startswith(clone_sources.TAG_PREFIX)}
    return _Taken(tags=on - clones, clones=clones, branches=branches, forks=forks)


def _plan(dataset: _Dataset, record: ReleaseRecord) -> tuple[list[str], list[Recreate], list[str]]:
    """The tags to delete and the branches to recreate top-down, as :func:`_taken` reads the table now, and the registered
    clones' tags among what it would take, which no release deletes: one standing there holds the release."""
    taken = _taken(dataset, record)
    head = int(dataset.version)
    top_down = sorted(taken.branches, key=lambda name: (_depth(name, taken.forks, taken.branches), name))
    plan = [Recreate(branch=name, parent=taken.forks[name] if taken.forks[name] in taken.branches else None, version=head) for name in top_down]
    return sorted(taken.tags), plan, sorted(taken.clones)


def _depth(name: str, forks: dict[str, str | None], subtree: set[str]) -> int:
    depth, parent = 0, forks.get(name)
    while parent is not None and parent in subtree and depth <= len(subtree):
        depth, parent = depth + 1, forks.get(parent)
    return depth


def _claim(current: dict[str, Any], *, now: datetime, tags: list[str], plan: list[Recreate]) -> dict[str, Any]:
    record = ReleaseRecord.model_validate(current)
    if not _claimable(record, now):
        raise _Lost
    record.lease_until = now + LEASE
    if record.phase == "pending":
        record.phase, record.tags, record.plan = "deleting", tags, plan
        return record.model_dump(mode="json")
    # Resumed: a branch cut from the subtree or from a residual, or a tag put on either, since the plan was recorded
    # would make Lance refuse a delete on every tick, or keep the subject, so it joins the plan before it is deleted. A
    # recorded step keeps its reference, and a newcomer comes back at the recorded head, as a descendant follows its parent.
    # A tag needs no recreate, so one found once the deletes are done is deleted without being recorded.
    recorded = {step.branch for step in record.plan}
    version = record.plan[0].version if record.plan else None
    newcomers = [step if version is None else step.model_copy(update={"version": version}) for step in plan if step.branch not in recorded]
    record.plan = [*record.plan, *newcomers]
    if record.phase == "deleting":
        record.tags = sorted({*record.tags, *tags})
    return record.model_dump(mode="json")


def _delete(dataset: _Dataset, record: ReleaseRecord, *, tags: Sequence[str], plan: Sequence[Recreate]) -> list[RefChange]:
    """Every tag of ``tags``, then every branch of ``plan`` leaf-first; one already gone is skipped, so a resumed tick
    converges."""
    changes: list[RefChange] = []
    listed = dataset.tags.list() or {}
    for tag in tags:
        if tag in listed:
            dataset.tags.delete(tag)
            changes.append(record.change("table_tag_deleted", {"tag": tag, "release_id": record.release_id}))
    branches = dataset.branches.list() or {}
    for step in reversed(plan):
        if step.branch in branches:
            dataset.branches.delete(step.branch)
            changes.append(record.change("table_branch_deleted", {"branch": step.branch, "release_id": record.release_id}))
    return changes


def _recreate(dataset: _Dataset, record: ReleaseRecord) -> list[RefChange]:
    """Every branch top-down at its recorded reference; one already back is skipped."""
    changes: list[RefChange] = []
    present = set(dataset.branches.list() or {})
    for step in record.plan:
        if step.branch in present:
            continue
        dataset.create_branch(step.branch, reference=(step.parent, step.version))
        present.add(step.branch)
        extra = {"branch": step.branch, "parent_branch": step.parent, "parent_version": step.version, "release_id": record.release_id}
        changes.append(record.change("table_branch_created", extra))
    return changes


def _reclaim(dataset: _Dataset, record: ReleaseRecord, *, storage_options: StorageOptions) -> None:
    """Main's cleanup at the obligation's retention, now that no ref the release could reach pins a residual: what the
    verification's reclaim left for the pins goes, as that reclaim would have taken it.

    Raises:
        RuntimeError: The cleanup did not go through (its ``history:main`` surface failed); the tick hands its lease back.
    """
    report = ErasureReport(table=record.table, predicate="")
    reclaim_refs(report, dataset, [None], clean={None}, retention=timedelta(days=record.retain_days), storage_options=storage_options)
    if failed := [surface.detail for surface in report.surfaces if surface.outcome == "failed"]:
        raise RuntimeError(f"the reclaim after the release did not go through: {'; '.join(failed)}")
    log.info("erasure_release_reclaimed", extra={"release": record.release_id, "table": record.table, "versions": report.versions_reclaimed})


def _reference(name: str) -> tuple[str | None, int | None]:
    """The ``(branch, version)`` a residual's ``<ref>@<version>`` names, ``None`` for main."""
    ref, _, version = name.rpartition("@")
    return (None if ref == "main" else ref, int(version) if version.isdigit() else None)


def _mutate(control_root: str, storage_options: StorageOptions, release_id: str, mutate: Callable[[dict[str, Any]], dict[str, Any]]) -> ReleaseRecord:
    try:
        return ReleaseRecord.model_validate(mutate_json(control_root, storage_options, record_key(release_id), mutate))
    except (RecordChangedError, RecordMissingError) as exc:
        raise _Lost from exc


def _hand_back(control_root: str, storage_options: StorageOptions, release_id: str, *, now: datetime, error: str) -> None:
    """Record why a tick failed and end its lease, so the next tick resumes rather than waiting the lease out."""

    def _release_lease(current: dict[str, Any]) -> dict[str, Any]:
        return {**current, "lease_until": now.isoformat(), "last_error": error[:512]}

    try:
        mutate_json(control_root, storage_options, record_key(release_id), _release_lease)
    except Exception as exc:  # noqa: BLE001 — the lease then simply runs out
        log.warning("erasure_release_hand_back_failed", extra={"release": release_id, "error": str(exc)})


def _finish(control_root: str, storage_options: StorageOptions, record: ReleaseRecord) -> None:
    """End the release, inside its hold: its record, then its mark, so no state has a record whose mark is gone (G9 of
    ``specs/catalog/Erasure.tla``: the mark first, and a tick that died between, left a record no ref door finds). A mark
    left by a tick that died between is removed by the next ref door that finds its record gone."""
    delete_record(control_root, storage_options, record_key(record.release_id))
    stored_objects.remove_object(control_root, storage_options, mark_key(record.table_uuid, record.release_id))
