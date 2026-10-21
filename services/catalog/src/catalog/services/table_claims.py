"""The catalog's half of the location claims ([[LH-204]]): who may take a location, when a holder is gone, and the uuid a new table's claim records.

:mod:`service_kit.lakehouse.location_claims` is the store-arbitrated record; this module is what only the
catalog can answer about it — whether a claim's holder still resolves to the location — and the one
shape every door calls it in. A table's uuid is recorded here when a door brings the table into being (ADR 0173
§2): :func:`claim_new_table` for create, declare and register, :func:`take` for a table that already exists and
keeps its uuid.

A door resolves its table once (ADR 0173 §7, [[LH-521]]): :func:`resolve` reads the table's location and that
location's claim when the request reaches the door, and a drop, deregister or rename acts on that reading and
nothing else. A drop or deregister fences the claim it read (:func:`fence`) before its backend call and lifts the fence
once it has acted (:func:`lift_fence`); a rename hands over the claim it read (:func:`hand_over`).
"""

from __future__ import annotations

import logging
from uuid import uuid4

from lance_namespace import (
    ConcurrentModificationError,
    DescribeTableRequest,
    InvalidTableStateError,
    LanceNamespace,
    NamespaceNotFoundError,
    TableNotFoundError,
)
from pydantic import BaseModel

from catalog.core.config import Settings
from catalog.services import native
from service_kit.lakehouse import location_claims, trash
from service_kit.lakehouse.base_refs import decoded_path
from service_kit.lakehouse.location_claims import ClaimRead, ClaimStore, LocationClaim


log = logging.getLogger(__name__)


class ResolvedTable(BaseModel):
    """The table a request's name named when the request reached its door: the one the door acts on.

    The table's uuid is its claim's (ADR 0173 §6). No location means the name named no table.
    """

    segments: list[str]
    location: str | None = None
    only_declared: bool = False
    #: The location's claim as the resolution read it, with its ETag; ``None`` when the location carries no record.
    claim: ClaimRead | None = None


class DoorFence(BaseModel):
    """The fence a door set on its table's claim: the location it acts on and the request token the fence carries."""

    location: str
    token: str


def claim_store(settings: Settings) -> ClaimStore:
    """The claims live on the control root beside the trash and base records, written with its credential."""
    return ClaimStore(control_root=settings.registry_root, storage_options=settings.storage_options())


def resolve(ns: LanceNamespace, store: ClaimStore, segments: list[str]) -> ResolvedTable:
    """Resolve the table ``segments`` names to its location and that location's claim, once.

    A declared table resolves too, flagged. A name that names no table, in a namespace that exists or one that does
    not (the ``dir`` backend answers ``NamespaceNotFoundError`` for that, measured on pylance 13.0.0), resolves to no
    location rather than raising, so a door answers for that itself after the checks it makes first, an idempotent
    replay of a request that already ended the table among them.
    """
    if not segments:
        return ResolvedTable(segments=[])
    try:
        described = native.call(ns, "describe_table", DescribeTableRequest(id=segments, check_declared=True))
    except (TableNotFoundError, NamespaceNotFoundError):
        return ResolvedTable(segments=segments)
    location = described.location or None
    return ResolvedTable(
        segments=segments,
        location=location,
        only_declared=bool(described.is_only_declared),
        claim=location_claims.read(store, location) if location else None,
    )


def fence(ns: LanceNamespace, store: ClaimStore, table: ResolvedTable, *, holder: str, verb: str) -> DoorFence:
    """Fence the claim ``table`` resolved to, before a door that removes ``holder`` from ``__manifest`` acts on it.

    A claim naming another table that still resolves to the location refuses too, deregister and recoverable drop
    included: it is what the resolution reads when a rename landed between its describe and its claim read, and acting
    on the name then would reach whatever table holds it now. Telling an alias (a second id registered at a held
    location, which no door has formed since [[LH-204]]) from that race would mean resolving the name again.

    Raises:
        TableNotFoundError: The name named no table.
        ConcurrentModificationError: The claim changed since the resolution read it, or another request is acting on
            the table: the name may now name another table.
        InvalidTableStateError: Another table holds the location and still resolves to it.
    """
    if table.location is None:
        raise TableNotFoundError(f"table not found: {holder}")
    token = uuid4().hex
    try:
        location_claims.fence(
            store, table.location, table.claim, table=holder, segments=table.segments, token=token, is_gone=lambda claim: holder_is_gone(ns, store, claim)
        )
    except location_claims.ClaimChangedError:
        raise ConcurrentModificationError(f"cannot {verb} {holder}: it changed while this request was being served; describe it and retry") from None
    except location_claims.LocationHeldError as held:
        log.warning("door_refused_held_location", extra={"table": holder, "location": held.claim.location, "holder": held.claim.table, "verb": verb})
        raise InvalidTableStateError(f"cannot {verb} {holder}: its location is held by another table, which still resolves to it") from None
    return DoorFence(location=table.location, token=token)


def lift_fence(store: ClaimStore, fenced: DoorFence) -> None:
    """Lift a door's fence once it has acted, or failed to. Logged, never raised: the fence lapses on its own after its lease."""
    try:
        location_claims.lift_fence(store, fenced.location, fenced.token)
    except Exception as exc:  # noqa: BLE001 — the door's own answer stands; a fence left behind lapses after LEASE
        log.error("location_claim_fence_not_lifted", extra={"location": fenced.location, "error": str(exc)[:300]})


def hand_over(ns: LanceNamespace, store: ClaimStore, source: ResolvedTable, *, table: str, segments: list[str], previous: str, token: str) -> LocationClaim:
    """Hand the claim ``source`` resolved to from ``previous`` to ``table``, judging a stale holder against this namespace.

    Raises:
        TableNotFoundError: The name named no table.
        LocationHeldError: Another table holds the location, or another request is acting on its holder.
        ClaimChangedError: The claim changed since the resolution read it.
    """
    if source.location is None:
        raise TableNotFoundError(f"table not found: {previous}")
    return location_claims.hand_over(
        store,
        source.location,
        source.claim,
        table=table,
        segments=segments,
        previous=previous,
        token=token,
        is_gone=lambda claim: holder_is_gone(ns, store, claim),
    )


def holder_is_gone(ns: LanceNamespace, store: ClaimStore, claim: LocationClaim) -> bool:
    """Whether ``claim``'s holder no longer resolves to the claimed location.

    A holder in the trash at that location is NOT gone: a recoverable drop keeps its location for the
    undrop, and the purge releases it. Any read that fails answers "not gone", so an outage never frees a
    location.
    """
    try:
        record = trash.get(store.control_root, store.storage_options, claim.table)
        if record is not None and decoded_path(str(record.get("location") or "")) == claim.location:
            return False
        try:
            described = native.call(ns, "describe_table", DescribeTableRequest(id=claim.segments))
        except TableNotFoundError:
            return True
        return decoded_path(str(described.location or "")) != claim.location
    except Exception as exc:  # noqa: BLE001 — an unreadable holder is a live one
        log.warning("location_claim_holder_unreadable", extra={"table": claim.table, "location": claim.location, "error": str(exc)[:300]})
        return False


def data_directory_holder_is_gone(ns: LanceNamespace, store: ClaimStore, claim: LocationClaim) -> bool:
    """Whether a data directory's holder no longer exists ([[LH-252]]).

    Unlike a table location, a data directory is never the holder's described location, so the holder
    is judged by existence alone: a table the namespace still describes, or one in the trash awaiting
    undrop, keeps its directory. Any read that fails answers "not gone", so an outage never frees one.
    """
    try:
        if trash.get(store.control_root, store.storage_options, claim.table) is not None:
            return False
        try:
            native.call(ns, "describe_table", DescribeTableRequest(id=claim.segments))
        except TableNotFoundError:
            return True
        return False
    except Exception as exc:  # noqa: BLE001 — an unreadable holder is a live one
        log.warning("data_directory_holder_unreadable", extra={"table": claim.table, "location": claim.location, "error": str(exc)[:300]})
        return False


def take(
    ns: LanceNamespace, store: ClaimStore, location: str, table: str, segments: list[str], *, previous: str | None = None, token: str | None = None
) -> LocationClaim:
    """:func:`service_kit.lakehouse.location_claims.take` for a table that already exists, judging a stale holder against this namespace.

    The table keeps the uuid its location's claim carries: a rename hands it over, and a recoverable drop and the
    undrops re-take what they hold.

    Raises:
        LocationHeldError: Another table holds the location.
    """
    return location_claims.take(store, location, table, segments, previous=previous, token=token, is_gone=lambda claim: holder_is_gone(ns, store, claim))


def claim_new_table(ns: LanceNamespace, store: ClaimStore, location: str, *, table: str, segments: list[str], table_uuid: str | None = None) -> LocationClaim:
    """Make a table this request brings into being the holder of ``location``, under its uuid (ADR 0173 §2).

    ``table_uuid`` is the uuid a declaration minted, because the location it declares is named after it
    ([[LH-520]]); a registration's location is its registrar's, so its uuid is minted here. A claim the location
    still holds under ``table``'s name is an ended table's, since the door brings a table of that name into being:
    it is replaced, as a released claim and a gone holder's are. A table attached where an earlier one was
    destroyed, deregistered or is gone is given a new uuid, and nothing granted to the earlier table reaches it.

    Raises:
        LocationHeldError: Another table holds the location.
    """
    minted = table_uuid or str(uuid4())
    return location_claims.take(store, location, table, segments, uuid=minted, is_gone=lambda claim: holder_is_gone(ns, store, claim))


def keep_for_trash(ns: LanceNamespace, store: ClaimStore, location: str, table: str, segments: list[str]) -> None:
    """Hold ``location`` for a table a recoverable drop is about to detach, so no other id registers over its bytes.

    A location another table already holds is left with that holder and logged: the drop is still
    correct, the purge refuses to delete bytes a live id resolves to, and the undrop refuses to alias it.
    """
    try:
        take(ns, store, location, table, segments)
    except location_claims.LocationHeldError as held:
        log.warning("trash_location_held_by_another_table", extra={"table": table, "holder": held.claim.table, "location": held.claim.location})


def require_no_other_holder(ns: LanceNamespace, store: ClaimStore, location: str, table: str) -> None:
    """Refuse destroying the bytes at ``location`` while its claim names ANOTHER table that still resolves to them.

    A destructive drop deletes the location, not the id: when another id holds it — an alias the doors
    cannot form any more, or one already on the estate — the drop would destroy that table's bytes while it
    goes on resolving. No claim, or a claim held by ``table`` or by a holder that is gone, lets the drop
    proceed; the manifest alone cannot say which of two ids owns the bytes, the claim can.

    Raises:
        InvalidTableStateError: Another live table holds the location.
    """
    current = location_claims.holder(store, location)
    if current is None or current.table == table or holder_is_gone(ns, store, current):
        return
    log.warning("drop_refused_held_location", extra={"table": table, "location": current.location, "holder": current.table})
    raise InvalidTableStateError(f"cannot destroy table {table}: its location is held by another table, which still resolves to it")
