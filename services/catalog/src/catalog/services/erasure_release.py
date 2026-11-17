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

**THEN THE RELEASE RECLAIMS WHAT THE REFS KEPT, under the same hold** (ADR 0233). Once nothing pins a residual, main's
cleanup at the obligation's retention (``retain_days``, whose window the verification already found the residuals
outside) deletes it, as the verification's own reclaim does. The table's history is held as a release from the plan to
the reclaim (:attr:`lakehouse.history_fences.HistoryWork.RELEASE`), which no ref write shares: a tag written after the
release's last read of the refs and before the reclaim would otherwise keep a residual, and one written after the
reclaim finds no residual left to name.

**NOBODY IS TOLD TWICE AND NOBODY IS LEFT UNTOLD** (ADR 0233). The verification files the release's notices before it
writes the record (:mod:`catalog.services.announcement_intents`, on :func:`notice_target`), each notice under an id fixed
by the release and the person (:func:`notice`), so whoever sends it again sends one event: the verification itself, the
next verification that meets the record, or the catalog's finish, which maintenance's hourly audit asks for. A tick
leaves a release alone while a filing of its notices stands, so it never runs before everyone it costs is told.

**ONE CLAIM PER RELEASE ACROSS REPLICAS.** A tick that outlasts its window can overlap the next on another replica, so
a tick claims a record by an ETag-guarded write (:func:`lakehouse.records.mutate_json`) carrying a lease; a replica that
loses the write, or finds the lease live, leaves the record alone.

**THE PREDICATE IS NEVER STORED.** It names the subject, and a record on the control root would be one more
place the identifier is written ([[LH-281]]). The release needs only the refs, which ``pinned_by`` names.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field

from catalog.services import announcement_intents
from catalog.services.announcement_intents import Announcement, ControlMessage, RecordHeld
from catalog.services.dataplane import recorded_branch
from catalog.services.erasure import ErasureReport, Pin, reclaim_refs
from lakehouse import control_prefixes
from lakehouse.control_events import CatalogControlEvent
from lakehouse.history_fences import HistoryFencedError, HistoryWork, TableHistory
from lakehouse.record_store import delete_record, list_records
from lakehouse.records import RecordChangedError, RecordMissingError, create_json, mutate_json, read_json
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

RELEASE_PREFIX = control_prefixes.ERASURE_RELEASES

#: How long a claimed release stays a tick's own before another may resume it. Longer than a release takes
#: (a handful of ref deletes and creates), so a slow tick is not overtaken; a tick that fails hands its lease
#: back at once, so this bounds only a replica that died holding one.
LEASE = timedelta(minutes=15)


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
    #: The obligation's retention, in days: the reclaim after the deletes keeps what is younger.
    retain_days: int = 0
    #: Recorded by the claiming tick before any delete: the tags it deletes, and the branches top-down.
    tags: list[str] = Field(default_factory=list)
    plan: list[Recreate] = Field(default_factory=list)
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
        due_at=due_at,
        residual=list(residual),
        retain_days=retain_days,
    )


def notice_target(release_id: str) -> str:
    """The object the release's notices are filed on (:mod:`catalog.services.announcement_intents`)."""
    return f"erasure_release:{release_id}"


def notice(record: ReleaseRecord, subject: str, *, object_id: str) -> Announcement:
    """The notice telling ``subject`` of ``record``, landed while the record stands: under an id fixed by the release and
    the person, so every request that sends it sends one event."""
    event = CatalogControlEvent(
        event_id=uuid5(NAMESPACE_URL, f"erasure_release_scheduled/{record.release_id}/{subject}").hex,
        action="erasure_release_scheduled",
        object_type="table",
        object_id=object_id,
        object_name=record.table,
        actor=record.requested_by,
        extra={"subject": subject, "release_id": record.release_id, "refs": record.refs, "due_at": record.due_at.isoformat()},
    )
    return Announcement(evidence=RecordHeld(key=record_key(record.release_id)), message=ControlMessage(event=event))


def schedule(control_root: str, storage_options: StorageOptions, record: ReleaseRecord) -> None:
    """Write ``record`` where the release tick finds it; raises when the store does not take it.

    Raises:
        RecordExistsError: A release under the same id is already scheduled.
    """
    create_json(control_root, storage_options, record_key(record.release_id), record.model_dump(mode="json"))


def scheduled(control_root: str, storage_options: StorageOptions, release_id: str) -> ReleaseRecord | None:
    """The release scheduled under ``release_id``, or None when none is (never scheduled, or already run)."""
    found = read_json(control_root, storage_options, record_key(release_id))
    return None if found is None else ReleaseRecord.model_validate(found[0])


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


def release_due(
    control_root: str, storage_options: StorageOptions, *, now: datetime, open_location: Callable[[str], _Dataset], table_options: StorageOptions
) -> list[RefChange]:
    """Release every scheduled release that is due and not held by another tick; the refs it changed.

    One release failing records its error, hands its lease back and leaves the others to run. One a reclaim of its table
    holds off ([[LH-308]]) is left unclaimed for a later tick, as is one whose notices are still filed (ADR 0233): its
    people are told first. ``table_options`` reach the tables' store, where the reclaim records what it removes.
    """
    changes: list[RefChange] = []
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
            changes += _release(control_root, storage_options, record, now=now, open_location=open_location, table_options=table_options)
        except _Lost:
            log.info("erasure_release_claimed_elsewhere", extra={"release": record.release_id})
        except HistoryFencedError as exc:
            log.info("erasure_release_waits_for_a_reclaim", extra={"release": record.release_id, "table": record.table, "reason": str(exc)})
        except Exception as exc:  # noqa: BLE001 — one release's failure must not hold up the others
            log.warning("erasure_release_failed", extra={"release": record.release_id, "table": record.table, "error": str(exc)})
            _hand_back(control_root, storage_options, record.release_id, now=now, error=str(exc))
    return changes


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
) -> list[RefChange]:
    dataset = open_location(record.location)
    # [[LH-308]] Held before the release is claimed, so a tick a reclaim holds off leaves it as it was: a branch is
    # recreated at the version the release recorded, which a reclaim reading the refs before it would delete. Held as a
    # release (ADR 0233), which no other work on the table's history shares: no ref is written between this tick's read
    # of the refs and its reclaim, and no create through the door lands a name nesting with one this recreates ([[LH-413]]).
    history = TableHistory(control_root=control_root, storage_options=storage_options, table_uuid=record.table_uuid)
    with history.held(HistoryWork.RELEASE):
        tags, plan = _plan(dataset, record)
        claimed = _mutate(control_root, storage_options, record.release_id, lambda current: _claim(current, now=now, tags=tags, plan=plan))
        changes: list[RefChange] = []
        if claimed.phase == "deleting":
            changes += _delete(dataset, claimed, tags=claimed.tags, plan=claimed.plan)
            claimed = _mutate(control_root, storage_options, record.release_id, lambda current: {**current, "phase": "recreating"})
        else:
            # Resumed once its deletes were done: only what stands on a residual since goes, which the plan read just now
            # names, and which the claim recorded where to recreate before this delete.
            tainted = {step.branch for step in plan}
            changes += _delete(dataset, claimed, tags=tags, plan=[step for step in claimed.plan if step.branch in tainted])
        changes += _recreate(dataset, claimed)
        _reclaim(dataset, claimed, storage_options=table_options)
    _finish(control_root, storage_options, record.release_id)
    log.info(
        "erasure_release_done",
        extra={"release": record.release_id, "table": record.table, "tags": claimed.tags, "branches": [step.branch for step in claimed.plan]},
    )
    return changes


def _plan(dataset: _Dataset, record: ReleaseRecord) -> tuple[list[str], list[Recreate]]:
    """The tags to delete and the branches to recreate top-down, read from the table as it stands now: before the deletes,
    what the record names and every ref standing on a residual; once they are done, only what stands on a residual."""
    listed_branches = dataset.branches.list() or {}
    forks = {str(name): recorded_branch(meta.get("parent_branch")) for name, meta in listed_branches.items()}
    residual = {_reference(name) for name in record.residual}
    cut_from = {str(name): (recorded_branch(meta.get("parent_branch")), meta.get("parent_version")) for name, meta in listed_branches.items()}
    on_residual = {name for name, reference in cut_from.items() if reference in residual}
    named = (
        set()
        if record.phase == "recreating"
        else {ref.split(":", 1)[1] for ref in record.refs if ref.startswith("branch:")} | {step.branch for step in record.plan}
    )
    subtree = {name for name in named | on_residual if name in forks}
    frontier = list(subtree)
    while frontier:
        parent = frontier.pop()
        children = {name for name, fork in forks.items() if fork == parent} - subtree
        subtree |= children
        frontier.extend(children)
    listed = dataset.tags.list() or {}
    recorded = set() if record.phase == "recreating" else {ref.split(":", 1)[1] for ref in record.refs if ref.startswith("tag:")} | set(record.tags)
    tags = sorted(
        recorded
        | {str(name) for name, meta in listed.items() if recorded_branch(meta.get("branch")) in subtree}
        | {str(name) for name, meta in listed.items() if (recorded_branch(meta.get("branch")), meta.get("version")) in residual}
    )
    head = int(dataset.version)
    top_down = sorted(subtree, key=lambda name: (_depth(name, forks, subtree), name))
    return [tag for tag in tags if tag in listed], [
        Recreate(branch=name, parent=forks[name] if forks[name] in subtree else None, version=head) for name in top_down
    ]


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


def _finish(control_root: str, storage_options: StorageOptions, release_id: str) -> None:
    delete_record(control_root, storage_options, record_key(release_id))
