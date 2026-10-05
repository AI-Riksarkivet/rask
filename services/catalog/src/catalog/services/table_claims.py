"""The catalog's half of the location claims ([[LH-204]]): who may take a location, and when a holder is gone.

:mod:`service_kit.lakehouse.location_claims` is the store-arbitrated record; this module is what only the
catalog can answer about it — whether a claim's holder still resolves to the location — and the one
shape every door calls it in.
"""

from __future__ import annotations

import logging

from lance_namespace import DescribeTableRequest, InvalidTableStateError, LanceNamespace, TableNotFoundError

from catalog.core.config import Settings
from catalog.services import native
from service_kit.lakehouse import location_claims, trash
from service_kit.lakehouse.base_refs import decoded_path
from service_kit.lakehouse.location_claims import ClaimStore, LocationClaim


log = logging.getLogger(__name__)


def claim_store(settings: Settings) -> ClaimStore:
    """The claims live on the control root beside the trash and base records, written with its credential."""
    return ClaimStore(control_root=settings.registry_root, storage_options=settings.storage_options())


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


def take(
    ns: LanceNamespace, store: ClaimStore, location: str, table: str, segments: list[str], *, previous: str | None = None, token: str | None = None
) -> None:
    """:func:`service_kit.lakehouse.location_claims.take`, judging a stale holder against this namespace.

    Raises:
        LocationHeldError: Another table holds the location.
    """
    location_claims.take(store, location, table, segments, previous=previous, token=token, is_gone=lambda claim: holder_is_gone(ns, store, claim))


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
    raise InvalidTableStateError(
        f"cannot destroy table {table}: its location is held by table {current.table!r}, which still resolves to it; deregister {table} instead"
    )
