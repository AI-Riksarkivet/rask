"""Ending what the trash keeps, judged on the catalog's own reads: what maintenance reclaims the trash through ([[XC-084]]).

A recoverable drop keeps its object's bytes, its records and its grants for an undrop until the record's deadline, past
which the drop is DropTable's, its data deleted asynchronously (ADR 0146). Maintenance runs that ending and holds no
write of its own on the store and no client that writes grants, so every write it needs is the catalog's:

- the release, once the deadline has settled (:data:`lakehouse.trash.RELEASE_SETTLE`): the catalog revokes the
  object's grants, tells each person who lost one, and stamps the record released;
- the purge of a table's bytes, under a session the catalog vends for that record alone (:func:`scope_of`), which
  lists and deletes at the location and in the table's own data directories and reaches nothing on the control root;
- the ending, once nothing the record names holds a byte: the catalog revokes what grants remain, ends the records the
  object holds under its uuid, its base record and its claims, and the trash record last (:func:`end_records`, the
  same ending the warehouse cascade gives its trash).

Every judgement reads only what no session can write: the kind and uuid the door names, the record the catalog filed
under them, and the claim on the record's location, which must record that uuid, so a record naming another table's
place is refused rather than vended for. Whether a live object holds the place or the name again is the door's to ask
(``catalog.api.v1.endpoints.trash_reclaim``), through the request's own resolution. A refusal is noted on the record
(:func:`lakehouse.trash.note_refusal`), as maintenance's own refusals are, so a record refused tick after tick sorts
behind the ones that move (``maintenance.services.purge._drain_order``).

Every function is BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from lance_namespace import NamespaceNotFoundError, TableNotFoundError

from catalog.core.config import Settings
from catalog.core.vending import ReclaimScope
from catalog.services import table_claims
from lakehouse import location_claims, trash
from lakehouse.base_refs import decoded_segments, store_of
from lakehouse.base_registry import BaseRegistry
from lakehouse.control_prefixes import CONTROL_PREFIXES
from lakehouse.data_directories import directories_to_end, directory_uri
from lakehouse.location_claims import LocationClaim
from lakehouse.stored_objects import list_under
from lakehouse.table_paths import objects_at
from lakehouse.trash import TrashKind


def read_record(settings: Settings, kind: TrashKind, uuid: str) -> dict[str, Any]:
    """The trash record of the ``kind`` object ``uuid`` names.

    Raises:
        TableNotFoundError: No trashed table holds that uuid.
        NamespaceNotFoundError: No trashed namespace holds that uuid.
    """
    record = trash.get(settings.registry_root, settings.storage_options(), uuid, kind=kind)
    if record is not None:
        return record
    if kind == "namespace":
        raise NamespaceNotFoundError(f"no trashed namespace holds the uuid {uuid}")
    raise TableNotFoundError(f"no trashed table holds the uuid {uuid}")


def location_claim(settings: Settings, record: dict[str, Any]) -> LocationClaim | None:
    """The claim on the record's location, held or released; ``None`` for a record naming no location, or no claim.

    Raises:
        ValueError: The stored claim is not a claim.
        botocore.exceptions.ClientError: The store refused the read.
    """
    location = trash.location_of(record)
    if not location:
        return None
    found = location_claims.read(table_claims.claim_store(settings), location)
    return found.claim if found is not None else None


def vend_refusal(record: dict[str, Any], claim: LocationClaim | None, *, kind: TrashKind, uuid: str, now: datetime) -> str | None:
    """Why no session deleting the record's bytes is vended, or ``None``: a table's, past its deadline, at its own place."""
    location = trash.location_of(record)
    if kind != "table" or not location:
        return "the record names no bytes, so no session deletes anything of it; end it once it is due"
    if (misplaced := _place_refusal(location, claim, uuid=uuid)) is not None:
        return misplaced
    if not trash.past_deadline(record, now=now):
        return "the record is short of its deadline, so its bytes are still an undrop's to restore"
    return None


def end_refusal(settings: Settings, record: dict[str, Any], claim: LocationClaim | None, *, kind: TrashKind, uuid: str, now: datetime) -> str | None:
    """Why the record is not ended yet, or ``None``: at its own place holding no byte, or naming none and past its deadline.

    A table record whose bytes are gone is ended at any time, as the purge leaves one after its deletes and a warehouse
    whose bucket went before its trash leaves one: no undrop restores what is not there.

    Raises:
        UnreadableBaseRecordError: The table's base record exists and cannot be read.
        OSError: The store refused, or could not be reached for, a listing.
    """
    location = trash.location_of(record)
    if location and kind == "namespace":
        return f"a namespace's record names no bytes, and this one names the location {location!r}"
    if not location:
        return None if trash.past_deadline(record, now=now) else "the record is short of its deadline and names no bytes, so it is still an undrop's"
    if (misplaced := _place_refusal(location, claim, uuid=uuid)) is not None:
        return misplaced
    if held := _objects_held(settings, record):
        return f"its place still holds {held} object(s); the purge deletes them first"
    return None


def release_refusal(record: dict[str, Any], *, now: datetime) -> str | None:
    """Why the record's grants are not released yet, or ``None`` once its deadline has settled."""
    if trash.releasable(record, now=now):
        return None
    return f"the record is not {trash.RELEASE_SETTLE} past its deadline, so an undrop admitted at it may still be finishing"


def scope_of(settings: Settings, record: dict[str, Any]) -> ReclaimScope:
    """What the purge of ``record`` deletes: its table's bytes at its location, and in the data directories its claim
    names ([[LH-286]], :func:`lakehouse.data_directories.directories_to_end`).

    Raises:
        UnreadableBaseRecordError: The table's base record exists and cannot be read.
        botocore.exceptions.ClientError: The store refused a read of the record or a claim.
    """
    location = trash.location_of(record)
    directories = directories_to_end(_registry(settings), location, table=trash.name_of(record))
    return ReclaimScope(location=location, data_directories=tuple(directory_uri(directory) for directory in directories))


def stamp_released(settings: Settings, *, kind: TrashKind, uuid: str, now: datetime) -> bool:
    """Stamp the record released once its grants are revoked; ``False`` when it is gone, purged or undropped since."""
    return trash.mark_released(settings.registry_root, settings.storage_options(), uuid, kind=kind, now=now)


def note_refusal(settings: Settings, *, kind: TrashKind, uuid: str, reason: str) -> int | None:
    """Note a refusal on the record and return its new attempt count; ``None`` when the note could not be made."""
    return trash.note_refusal(settings.registry_root, settings.storage_options(), uuid, kind=kind, reason=reason)


def _registry(settings: Settings) -> BaseRegistry:
    return BaseRegistry(control_root=settings.registry_root, storage_options=settings.storage_options())


def _place_refusal(location: str, claim: LocationClaim | None, *, uuid: str) -> str | None:
    """Why ``location`` is not the trashed table's own place, or ``None``.

    It names one place below a store root and crosses no control prefix, as the catalog's destructive drop requires of
    a location it deletes, and its claim records the record's uuid: the claim is what a create, a register or a rename
    writes for the table holding a place, and a recoverable drop keeps it for the trashed uuid, so no record can steer
    a reclaim to a place another table holds or one no table holds.
    """
    segments = decoded_segments(location)
    if segments is None or (store_of(location) != "file" and len(segments) < 2):
        return f"the record's location {location!r} names no single place below a store root"
    if crossed := sorted(CONTROL_PREFIXES.intersection(segments)):
        return f"the record's location {location!r} crosses the control prefix(es) {crossed}"
    if claim is None:
        return f"no claim on {location!r} records the trashed table, so nothing shows the place is its own"
    if claim.uuid != uuid:
        return f"the claim on {location!r} records another table, so the place is not this record's"
    return None


def _objects_held(settings: Settings, record: dict[str, Any]) -> int:
    """How many objects the record's location, in both spellings, and the data directories its claim names still hold."""
    so = settings.storage_options()
    location = trash.location_of(record)
    directories = directories_to_end(_registry(settings), location, table=trash.name_of(record))
    return len(objects_at(location, so)) + sum(len(list_under(directory_uri(directory), so)) for directory in directories)
