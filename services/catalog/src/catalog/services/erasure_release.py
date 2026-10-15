"""The release of what pins an erasure's residual, a grace period after the people it costs were told ([[LH-178]]).

ADR 0138 (owner decision D4): when an erasure finds a tag or a branch keeping a version that holds the subject
(``ErasureReport.pinned_by``), the requester and the table's owners are told, and when the grace period ends
every pinning tag is deleted and every pinning branch, with its descendants, is deleted and recreated at main's
head. There is no door that cancels or extends it: an erasure that can be blocked is not an erasure.

**THE RELEASE IS DURABLE STATE WITH A DUE TIME, not a timer.** One record per release on the control root
(``_erasure_releases/<id>.json``), written before anyone is told, read by a cron tick that releases what is due.
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

**ONE CLAIM PER RELEASE ACROSS REPLICAS.** A cron binding fires on every replica, so a tick claims a record by
an ETag-guarded write (:func:`service_kit.lakehouse.records.mutate_json`) carrying a lease; a replica that
loses the write, or finds the lease live, leaves the record alone.

**THE PREDICATE IS NEVER STORED.** It names the subject, and a record on the control root would be one more
place the identifier is written ([[LH-281]]). The release needs only the refs, which ``pinned_by`` names.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from catalog.services.dataplane import recorded_branch
from catalog.services.erasure import Pin
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.lakehouse.record_store import delete_record, list_records
from service_kit.lakehouse.records import RecordChangedError, RecordMissingError, create_json, mutate_json, read_json


log = logging.getLogger(__name__)

RELEASE_PREFIX = "_erasure_releases"

#: How long a claimed release stays a tick's own before another may resume it. Longer than a release takes
#: (a handful of ref deletes and creates), so a slow tick is not overtaken; a tick that fails hands its lease
#: back at once, so this bounds only a replica that died holding one.
LEASE = timedelta(minutes=15)


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
    #: The table id the erasure was asked of, for the log and the events.
    table: str
    #: The dataset root the erasure ran on: the release acts on these bytes whatever the id resolves to later.
    location: str
    #: The canonical FGA object (``table:<id>``) the events name.
    object_id: str
    requested_by: str | None
    notified: list[str]
    #: ``pinned_by``'s refs as the erasure named them: tags, then branches deepest first.
    refs: list[str]
    due_at: datetime
    phase: Literal["pending", "deleting", "recreating"] = "pending"
    lease_until: datetime | None = None
    #: Recorded by the claiming tick before any delete: the tags it deletes, and the branches top-down.
    tags: list[str] = Field(default_factory=list)
    plan: list[Recreate] = Field(default_factory=list)
    last_error: str = ""


class RefChange(BaseModel):
    """One ref the release changed, for the untargeted ref-plane event the matching door would emit."""

    action: Literal["table_tag_deleted", "table_branch_deleted", "table_branch_created"]
    object_id: str
    extra: dict[str, Any]


class _Dataset(Protocol):
    """The pylance surface a release uses."""

    @property
    def version(self) -> int: ...
    @property
    def branches(self) -> Any: ...
    @property
    def tags(self) -> Any: ...
    def create_branch(self, branch: str, reference: Any = None, storage_options: Any = None, **kwargs: Any) -> Any: ...


def record_key(release_id: str) -> str:
    return f"{RELEASE_PREFIX}/{release_id}.json"


def new_release(
    pins: Sequence[Pin], *, release_id: str, table: str, location: str, object_id: str, requested_by: str | None, notified: list[str], due_at: datetime
) -> ReleaseRecord:
    """The release of everything ``pins`` names, under ``release_id``: the erasure obligation's own id, so a second
    verification that settles the same obligation meets the first one's record rather than scheduling it again."""
    return ReleaseRecord(
        release_id=release_id,
        table=table,
        location=location,
        object_id=object_id,
        requested_by=requested_by,
        notified=notified,
        refs=[pin.ref for pin in pins],
        due_at=due_at,
    )


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
        f"the subject's bytes leave storage only after all of: the pins named in refs are released at {due_at.isoformat()}; "
        "compaction rewrites the fragments that hold the subject, since a delete leaves its rows in their data files behind a deletion "
        "file; every index over an erased column is rebuilt from the rewritten data; and the version holding the subject is superseded "
        f"and older than the maintenance cleanup window ({cleanup_window_days} days, or the table's retention_days policy). Until that "
        "cleanup runs, restoring a retained pre-erasure version brings the rows back"
    )


def release_due(control_root: str, storage_options: StorageOptions, *, now: datetime, open_location: Callable[[str], _Dataset]) -> list[RefChange]:
    """Release every scheduled release that is due and not held by another tick; the refs it changed.

    One release failing records its error, hands its lease back and leaves the others to run.
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
            changes += _release(control_root, storage_options, record, now=now, open_location=open_location)
        except _Lost:
            log.info("erasure_release_claimed_elsewhere", extra={"release": record.release_id})
        except Exception as exc:  # noqa: BLE001 — one release's failure must not hold up the others
            log.warning("erasure_release_failed", extra={"release": record.release_id, "table": record.table, "error": str(exc)})
            _hand_back(control_root, storage_options, record.release_id, now=now, error=str(exc))
    return changes


class _Lost(Exception):
    """Another tick holds the release, or finished it."""


def _claimable(record: ReleaseRecord, now: datetime) -> bool:
    return record.due_at <= now and (record.lease_until is None or record.lease_until <= now)


def _release(
    control_root: str, storage_options: StorageOptions, record: ReleaseRecord, *, now: datetime, open_location: Callable[[str], _Dataset]
) -> list[RefChange]:
    dataset = open_location(record.location)
    tags, plan = _plan(dataset, record) if record.phase != "recreating" else (record.tags, record.plan)
    claimed = _mutate(control_root, storage_options, record.release_id, lambda current: _claim(current, now=now, tags=tags, plan=plan))
    changes: list[RefChange] = []
    if claimed.phase == "deleting":
        changes += _delete(dataset, claimed)
        claimed = _mutate(control_root, storage_options, record.release_id, lambda current: {**current, "phase": "recreating"})
    changes += _recreate(dataset, claimed)
    _finish(control_root, storage_options, record.release_id)
    log.info(
        "erasure_release_done",
        extra={"release": record.release_id, "table": record.table, "tags": claimed.tags, "branches": [step.branch for step in claimed.plan]},
    )
    return changes


def _plan(dataset: _Dataset, record: ReleaseRecord) -> tuple[list[str], list[Recreate]]:
    """The tags to delete and the branches to recreate top-down, read from the table as it stands now."""
    forks = {str(name): recorded_branch(meta.get("parent_branch")) for name, meta in (dataset.branches.list() or {}).items()}
    named = {ref.split(":", 1)[1] for ref in record.refs if ref.startswith("branch:")} | {step.branch for step in record.plan}
    subtree = {name for name in named if name in forks}
    frontier = list(subtree)
    while frontier:
        parent = frontier.pop()
        children = {name for name, fork in forks.items() if fork == parent} - subtree
        subtree |= children
        frontier.extend(children)
    listed = dataset.tags.list() or {}
    tags = sorted(
        {ref.split(":", 1)[1] for ref in record.refs if ref.startswith("tag:")}
        | set(record.tags)
        | {str(name) for name, meta in listed.items() if recorded_branch(meta.get("branch")) in subtree}
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
    elif record.phase == "deleting":
        # Resumed between its deletes: a branch cut from the subtree, or a tag put on it, since the plan was
        # recorded would make Lance refuse a delete on every tick, so it joins the plan. A recorded step keeps its
        # reference, and a newcomer comes back at the recorded head: being a descendant, it follows its parent.
        recorded = {step.branch for step in record.plan}
        version = record.plan[0].version if record.plan else None
        newcomers = [step if version is None else step.model_copy(update={"version": version}) for step in plan if step.branch not in recorded]
        record.tags, record.plan = sorted({*record.tags, *tags}), [*record.plan, *newcomers]
    return record.model_dump(mode="json")


def _delete(dataset: _Dataset, record: ReleaseRecord) -> list[RefChange]:
    """Every tag, then every branch leaf-first; one already gone is skipped, so a resumed tick converges."""
    changes: list[RefChange] = []
    listed = dataset.tags.list() or {}
    for tag in record.tags:
        if tag in listed:
            dataset.tags.delete(tag)
            changes.append(RefChange(action="table_tag_deleted", object_id=record.object_id, extra={"tag": tag, "release_id": record.release_id}))
    branches = dataset.branches.list() or {}
    for step in reversed(record.plan):
        if step.branch in branches:
            dataset.branches.delete(step.branch)
            changes.append(RefChange(action="table_branch_deleted", object_id=record.object_id, extra={"branch": step.branch, "release_id": record.release_id}))
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
        changes.append(RefChange(action="table_branch_created", object_id=record.object_id, extra=extra))
    return changes


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
