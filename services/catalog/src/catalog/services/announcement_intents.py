"""Announcements filed before the change they announce ([[LH-576]], ADR 0205).

A door that changes state and tells people about it (a grant added or revoked, a role or a project ended, a table
declared, registered, renamed, dropped or deregistered) announces only after its change, so a change that did not
happen is never announced. Each emitter stages what it publishes before the publish, so a bus that loses an event loses
nothing; what lay between the change and the emitter's stage, an error or a process death, lost the announcement for
good. So the door files its announcements first, one record under ``_announcement_intents/<target>/`` on the control
root, the object its change is made on naming the directory, makes its change, emits them as it always has, and
removes the record last.

A record its door left (it failed, or its process died) is finished by the catalog when maintenance's hourly audit
asks: once :data:`LEASE` has passed since the filing, the door that filed it has stopped, and each announcement goes out
only where the store shows its change landed (its :data:`Evidence`), as the event the door built, so one the door sent
already arrives again under its own id (a control event's ``event_id``, a marker's run derived from the time the door
fixed) and its reader recognises it. The record goes last, so a finish that fails part-way is finished by the next.

The store shows a change landed, never whose request landed it: a request that failed before its change landed leaves
a record a retry's change satisfies, and one that failed after it leaves a record a retry announces again. So a door
reads what is filed on its object before it files, and announces each change an earlier request filed under the id that
request filed it with (:func:`identity`, :func:`reusing`): the retry, the earlier request and the finish then send one
event, and its reader keeps the copy that reaches it first.

All IO here is blocking; callers threadpool it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from lakehouse.events.control_events import CatalogControlEvent
from lakehouse.events.lineage_events import DatasetUuids
from lakehouse.records import control_prefixes, location_claims
from lakehouse.storage import conditional_records, stored_objects
from lakehouse.storage.options import StorageOptions


log = logging.getLogger(__name__)

PREFIX = control_prefixes.ANNOUNCEMENT_INTENTS

#: How long a filing belongs to the door that filed it. A request runs at most the request deadline, so past this the
#: door has stopped and what it filed is the store's to decide.
LEASE = location_claims.LEASE

_FILING_RE = re.compile(r"/([0-9a-f]{32})/([0-9a-f]{32})\.json$")


class _Evidence(BaseModel):
    """What a filing says the store shows once its change landed. Read exactly or not at all: a field the code does not
    know (one an earlier shape carried, ``held`` on a name's evidence) may reverse what the evidence means, so a filing
    whose evidence the code cannot read exactly is never judged: the finish sends nothing of it, and removes it once it
    has stood past its lease, counted in ``catalog.announcement_intents.unreadable``."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class TupleGone(_Evidence):
    """A revoke landed: OpenFGA holds no such tuple."""

    kind: Literal["tuple_gone"] = "tuple_gone"
    user: str
    relation: str
    object: str


class TupleHeld(_Evidence):
    """A grant landed: OpenFGA holds the tuple."""

    kind: Literal["tuple_held"] = "tuple_held"
    user: str
    relation: str
    object: str


class NameResolves(_Evidence):
    """A table's name resolves to the table ``uuid`` names: a declare, a register, a rename or an undrop landed when the
    name names it."""

    kind: Literal["name_resolves"] = "name_resolves"
    segments: list[str]
    uuid: str


class Unregistered(_Evidence):
    """The table ``uuid`` names is registered under no name: it resolves neither at ``segments``, the name its door
    retired it from, nor at the name the claim on its ``location`` holds it under now, and the trash keeps it exactly
    where its door retires a table into the trash (``trashed``). A destroying drop, a deregister and a cascade's removal of
    a table landed so.

    Evidence about the table, never about a name alone: a rename hands the location's claim to the table's new name, so
    a table renamed since a door that never reached it is registered still, and nothing is announced of it. A table the
    trash keeps by a record written since the filing was retired by a recoverable drop, so a door that retires a table
    out of governance is not judged to have done it; a record written before the filing is a stale one a live table
    carried (an undrop that ended before clearing it, a cascade that failed before its detach), and a residue record a
    destroying drop leaves on bytes it could not delete keeps no table.

    TWO LIMITS. The store cannot tell which actor retired a table: once this door died, another actor's deregister or
    destroying drop satisfies the evidence too, and this door's event is sent as well, under its own ids (a dead
    deregister followed by another's destroying drop yields a ``table_deregistered`` for a table that was dropped). And a
    recoverable cascade files each table's trash record before its detach, so one that fails between the two leaves a
    record on a live table; the single drop's stamp (``TrashedUnder``) closes that for the drop door, and the cascade has
    no stamp. The stamp compared is the record's last write, so a stale record rewritten since the filing reads as a
    drop since (no step rewrites one while a filing on its object stands). A filing whose evidence the code cannot read
    exactly is removed unread by the finish once past its lease, with an ERROR line and a counter as its trace.
    """

    kind: Literal["unregistered"] = "unregistered"
    segments: list[str]
    uuid: str
    location: str
    trashed: bool


class NamespaceResolves(_Evidence):
    """A namespace's name resolves to the namespace ``uuid`` names (``held``), or no longer does: a namespace's drop
    landed when its name names no namespace or another one."""

    kind: Literal["namespace_resolves"] = "namespace_resolves"
    segments: list[str]
    uuid: str
    held: bool


class RecordGone(_Evidence):
    """A control record is gone: a project's delete landed when its record is."""

    kind: Literal["record_gone"] = "record_gone"
    key: str


class RecordHeld(_Evidence):
    """A control record stands: a principal's offboarding landed when its deletion is recorded."""

    kind: Literal["record_held"] = "record_held"
    key: str


class RoleEnded(_Evidence):
    """A role's end landed: ``project`` no longer holds the role standing."""

    kind: Literal["role_ended"] = "role_ended"
    project: str
    role: str


class TrashedUnder(_Evidence):
    """A recoverable drop of the table ``uuid`` names landed: the trash record that drop filed under the id it minted
    (``attempt``, ``lakehouse.records.trash.TrashRecord.drop``) is stamped detached, or, left unstamped, the location's
    claim shows the drop detached the table (``catalog.api.announcing.Announcer.landed``). Evidence about the table and
    this drop, never about a name: a name stops naming a table its drop never reached once a rename moves it."""

    kind: Literal["trashed_under"] = "trashed_under"
    trash_kind: Literal["table"] = "table"
    uuid: str
    attempt: str


type Evidence = Annotated[
    TupleGone | TupleHeld | NameResolves | Unregistered | NamespaceResolves | RecordGone | RecordHeld | RoleEnded | TrashedUnder,
    Field(discriminator="kind"),
]


class ControlMessage(BaseModel):
    """A control event, built by its door before the change, so every send of it carries its ``event_id``."""

    model_config = ConfigDict(frozen=True)

    lane: Literal["control"] = "control"
    event: CatalogControlEvent


class LineageMarker(BaseModel):
    """A table's lifecycle marker on the lineage lane, as its door emits it (``catalog.core.lineage_emit.emit_write_event``)."""

    model_config = ConfigDict(frozen=True)

    lane: Literal["lineage"] = "lineage"
    segments: list[str]
    author: str | None
    operation: str
    uuids: DatasetUuids
    source_uri: str | None = None
    renamed_from: list[str] | None = None
    #: The person a service made the change for, as the door's request claimed it (``x-lance-originator``), so a finish
    #: names them as the door would have.
    originator: str | None = None
    #: When the door announced the change, fixed at its filing: the marker's run id is derived from it, so the door's
    #: emit and a finish's are one run (``catalog.core.commit_ids.lifecycle_run_id``).
    event_time: str


type Message = Annotated[ControlMessage | LineageMarker, Field(discriminator="lane")]


class Announcement(BaseModel):
    """One message, sent once the store shows its change landed."""

    model_config = ConfigDict(frozen=True)

    evidence: Evidence
    message: Message


def identity(announcement: Announcement) -> str:
    """What makes two filed announcements one event: the same change (its evidence), told on the same lane as the same
    action, and on the lineage lane by the same author, since lineage binds a run to the first author it records for it.
    A control event's reader keeps the first copy of an id, whoever it names as the actor."""
    message = announcement.message
    told = (message.event.action,) if isinstance(message, ControlMessage) else (message.operation, message.author)
    return json.dumps([announcement.evidence.model_dump(mode="json"), message.lane, *told], sort_keys=True)


def reusing(current: Announcement, earlier: Announcement) -> Announcement:
    """``current`` as one event with ``earlier``, which an earlier request filed for the same change (:func:`identity`): a
    control event as ``current`` tells it under ``earlier``'s id, a marker as ``earlier`` was filed, its run derived from
    the time that request fixed."""
    if isinstance(current.message, ControlMessage) and isinstance(earlier.message, ControlMessage):
        event = current.message.event.model_copy(update={"event_id": earlier.message.event.event_id})
        return current.model_copy(update={"message": ControlMessage(event=event)})
    return earlier


class Intent(BaseModel):
    """What one door filed before its change."""

    model_config = ConfigDict(frozen=True)

    door: str
    #: The object the door's change is made on, which names the filing's directory.
    target: str
    announcements: list[Announcement]


class Standing(BaseModel):
    """A filing still standing, and how old the store's clock read it."""

    model_config = ConfigDict(frozen=True)

    #: Where it lives on the control root, which :func:`settle` removes.
    key: str
    intent: Intent
    #: The store's clock at the listing, less the stamp the store put on the filing.
    age: timedelta
    #: The stamp the store put on the filing, on the store's clock: what a record written since is ordered against.
    filed_at: datetime

    @property
    def young(self) -> bool:
        """Whether the door that filed it may still be at work."""
        return self.age < LEASE


def _directory(target: str) -> str:
    return f"{PREFIX}/{hashlib.sha256(target.encode()).hexdigest()[:32]}"


def file(control_root: str, storage_options: StorageOptions, *, door: str, target: str, announcements: Sequence[Announcement]) -> str:
    """File ``announcements`` before ``door``'s change on ``target``; the filing's key, which :func:`settle` removes.

    Raises:
        OSError: The store refused the write or did not answer; the door has changed nothing yet.
    """
    key = f"{_directory(target)}/{uuid4().hex}.json"
    conditional_records.create_json(
        control_root, storage_options, key, Intent(door=door, target=target, announcements=list(announcements)).model_dump(mode="json")
    )
    return key


def settle(control_root: str, storage_options: StorageOptions, key: str) -> None:
    """Remove a filing whose announcements went out, or were judged unlanded, in one request; one already gone is no error.

    Raises:
        OSError: The store refused the delete or did not answer.
    """
    stored_objects.remove_object(control_root, storage_options, key)


class Listed(BaseModel):
    """What one read of the standing filings found."""

    model_config = ConfigDict(frozen=True)

    filings: list[Standing]
    #: The key, the store's stamp and the age of each filing looked at that is no filing this code reads exactly, which
    #: this code can never judge (:meth:`catalog.api.announcing.Announcer.finish` removes it once past its lease).
    unread: list[tuple[str, datetime, timedelta]] = []
    #: How many filings the listing held past the cursor it was read after.
    listed: int
    #: Where the next read resumes: past the last filing this one looked at, whatever became of it; ``None`` once it looked
    #: at the last one (:func:`standing`'s ``after``).
    next: str | None = None


def standing(control_root: str, storage_options: StorageOptions, *, target: str | None = None, limit: int | None = None, after: str | None = None) -> Listed:
    """The filings still standing, or only those on ``target``, oldest first (by the store's stamp, then key), from past
    the cursor ``after`` a read before this one returned: at most ``limit`` of them read, so a caller bounded by it reads
    no more than it judges, and a caller resuming from the cursor reaches the filings behind any it could not settle.
    One that cannot be read as a filing is left out of ``filings`` and named in ``unread``: it names nothing a finish
    could send.

    Raises:
        OSError: The store refused the listing or a read, or did not answer.
        ValueError: ``after`` is no cursor this function returned.
    """
    resume = _resumed(after)
    under = _directory(target) if target is not None else PREFIX
    listing = stored_objects.list_under_at(f"{control_root.rstrip('/')}/{under}", storage_options)
    named = sorted(
        (listed.modified, f"{PREFIX}/{found.group(1)}/{found.group(2)}.json")
        for listed in listing.objects
        if (found := _FILING_RE.search(listed.path)) is not None
    )
    if resume is not None:
        named = [(modified, key) for modified, key in named if (modified, key) > resume]
    looked = named[:limit]
    found: list[Standing] = []
    unread: list[tuple[str, datetime, timedelta]] = []
    for modified, key in looked:
        try:
            read = conditional_records.read_json(control_root, storage_options, key)
            intent = None if read is None else Intent.model_validate(read[0])
        except ValueError:  # bytes that are no JSON, or JSON that is no filing (pydantic's ValidationError is one)
            unread.append((key, modified, listing.at - modified))
            continue
        if intent is not None:
            found.append(Standing(key=key, intent=intent, age=listing.at - modified, filed_at=modified))
    following = f"{looked[-1][0].isoformat()} {looked[-1][1]}" if looked and len(looked) < len(named) else None
    return Listed(filings=found, listed=len(named), next=following, unread=unread)


def _resumed(after: str | None) -> tuple[datetime, str] | None:
    """The (stamp, key) the cursor ``after`` names.

    Raises:
        ValueError: It names none.
    """
    if after is None:
        return None
    stamp, _, key = after.partition(" ")
    if not _FILING_RE.search(f"/{key}") or not key.startswith(f"{PREFIX}/"):
        raise ValueError(f"{after!r} is no cursor of the standing filings")
    at = datetime.fromisoformat(stamp)
    # The store's stamps carry their zone, and one without cannot be ordered against them.
    if at.tzinfo is None:
        raise ValueError(f"{after!r} is no cursor of the standing filings")
    return at, key
