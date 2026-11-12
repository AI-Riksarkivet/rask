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
from datetime import timedelta
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from lakehouse import control_prefixes, location_claims, records, stored_objects
from lakehouse.control_events import CatalogControlEvent
from lakehouse.lineage_events import DatasetUuids
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

PREFIX = control_prefixes.ANNOUNCEMENT_INTENTS

#: How long a filing belongs to the door that filed it. A request runs at most the request deadline, so past this the
#: door has stopped and what it filed is the store's to decide.
LEASE = location_claims.LEASE

_FILING_RE = re.compile(r"/([0-9a-f]{32})/([0-9a-f]{32})\.json$")


class TupleGone(BaseModel):
    """A revoke landed: OpenFGA holds no such tuple."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["tuple_gone"] = "tuple_gone"
    user: str
    relation: str
    object: str


class TupleHeld(BaseModel):
    """A grant landed: OpenFGA holds the tuple."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["tuple_held"] = "tuple_held"
    user: str
    relation: str
    object: str


class NameResolves(BaseModel):
    """A table's name resolves to the table ``uuid`` names (``held``), or no longer does: a declare, a register or a rename
    landed when the name names it, a drop or a deregister when the name names no table or another one."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["name_resolves"] = "name_resolves"
    segments: list[str]
    uuid: str
    held: bool


class NamespaceResolves(BaseModel):
    """A namespace's name resolves to the namespace ``uuid`` names (``held``), or no longer does: a namespace's drop
    landed when its name names no namespace or another one."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["namespace_resolves"] = "namespace_resolves"
    segments: list[str]
    uuid: str
    held: bool


class RecordGone(BaseModel):
    """A control record is gone: a project's delete landed when its record is."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["record_gone"] = "record_gone"
    key: str


class RecordHeld(BaseModel):
    """A control record stands: a principal's offboarding landed when its deletion is recorded."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["record_held"] = "record_held"
    key: str


class RoleEnded(BaseModel):
    """A role's end landed: ``project`` no longer holds the role standing."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["role_ended"] = "role_ended"
    project: str
    role: str


type Evidence = Annotated[TupleGone | TupleHeld | NameResolves | NamespaceResolves | RecordGone | RecordHeld | RoleEnded, Field(discriminator="kind")]


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
    records.create_json(control_root, storage_options, key, Intent(door=door, target=target, announcements=list(announcements)).model_dump(mode="json"))
    return key


def settle(control_root: str, storage_options: StorageOptions, key: str) -> None:
    """Remove a filing whose announcements went out, or were judged unlanded, in one request; one already gone is no error.

    Raises:
        OSError: The store refused the delete or did not answer.
    """
    stored_objects.remove_object(control_root, storage_options, key)


def standing(control_root: str, storage_options: StorageOptions, *, target: str | None = None, limit: int | None = None) -> tuple[list[Standing], int]:
    """The filings still standing, or only those on ``target``, oldest first, and how many the listing held: at most
    ``limit`` of them read, so a caller bounded by it reads no more than it judges. One that cannot be read as a filing is
    logged and left out: it names nothing a finish could send.

    Raises:
        OSError: The store refused the listing or a read, or did not answer.
    """
    under = _directory(target) if target is not None else PREFIX
    listing = stored_objects.list_under_at(f"{control_root.rstrip('/')}/{under}", storage_options)
    named = [(listed, found) for listed in sorted(listing.objects, key=lambda obj: obj.modified) if (found := _FILING_RE.search(listed.path)) is not None]
    found: list[Standing] = []
    for listed, match in named[:limit]:
        key = f"{PREFIX}/{match.group(1)}/{match.group(2)}.json"
        try:
            read = records.read_json(control_root, storage_options, key)
            intent = None if read is None else Intent.model_validate(read[0])
        except ValueError as exc:  # bytes that are no JSON, or JSON that is no filing (pydantic's ValidationError is one)
            log.error("announcement_intent_unreadable", extra={"key": key, "error": str(exc)[:300]})
            continue
        if intent is not None:
            found.append(Standing(key=key, intent=intent, age=listing.at - listed.modified))
    return found, len(named)
