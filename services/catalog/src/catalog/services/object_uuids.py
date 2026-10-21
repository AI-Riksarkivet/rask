"""The uuids an event names a table or namespace by beside its name (ADR 0173 §10, [[LH-522]]).

A table's uuid is in its location's claim, a namespace's in its own ``rask.uuid`` property (ADR 0173 §6). An event is
announced after the door's act, so the table is read through the location that act touched whenever the door knows
it: a rename or a re-create landing between the act and the announcement moves the name and leaves the location, so
the event still names the table it was about. Best-effort, as the announcement is: a uuid that cannot be read is
``None``, and the event carries the name alone.
"""

from __future__ import annotations

import logging

from lance_namespace import DescribeNamespaceRequest, DescribeTableRequest, InternalError, LanceNamespace

from catalog.services import native
from service_kit.control_events import ControlUuids
from service_kit.lakehouse import location_claims
from service_kit.lakehouse.location_claims import ClaimStore
from service_kit.lakehouse.naming import uuid_of
from service_kit.openlineage import DatasetUuids


log = logging.getLogger(__name__)


def namespace_uuid(ns: LanceNamespace, segments: list[str]) -> str | None:
    """The uuid of the namespace ``segments`` names, or ``None`` for the root, a namespace that is gone, or a failed read."""
    if not segments:
        return None
    try:
        described = native.call(ns, "describe_namespace", DescribeNamespaceRequest(id=segments))
    except Exception as exc:  # noqa: BLE001 — an event is announced without what it cannot read
        log.info("event_namespace_uuid_unread", extra={"namespace": segments, "error": str(exc)[:300]})
        return None
    return uuid_of(described.properties)


def require_namespace_uuid(ns: LanceNamespace, segments: list[str]) -> str:
    """The uuid of the namespace ``segments`` names, for a door that reads or writes a record kept for it (ADR 0173 §8).

    Raises:
        NamespaceNotFoundError: No such namespace.
        InternalError: The namespace carries no uuid, which no door leaves.
    """
    described = native.call(ns, "describe_namespace", DescribeNamespaceRequest(id=segments))
    uuid = uuid_of(described.properties)
    if uuid is None:
        log.error("namespace_uuid_unrecorded", extra={"namespace": segments})
        raise InternalError(f"namespace {segments} carries no uuid, so no record kept for it can be read")
    return uuid


def table_uuid(ns: LanceNamespace, store: ClaimStore, segments: list[str], *, location: str | None = None) -> str | None:
    """The uuid of the table at ``location``, else of the table ``segments`` names now; ``None`` when neither reads.

    A released claim still names the table that last held the location, which is the table a drop that destroyed it
    was about.
    """
    try:
        if location is None:
            location = native.call(ns, "describe_table", DescribeTableRequest(id=segments, check_declared=True)).location or None
        claim = location_claims.read(store, location) if location else None
    except Exception as exc:  # noqa: BLE001 — an event is announced without what it cannot read
        log.info("event_table_uuid_unread", extra={"table": segments, "error": str(exc)[:300]})
        return None
    return claim.claim.uuid if claim is not None else None


def of_table(ns: LanceNamespace, store: ClaimStore, segments: list[str], *, location: str | None = None, uuid: str | None = None) -> DatasetUuids:
    """A table's uuid and its namespace's. ``uuid`` is the one the door resolved, when it holds one: it wins."""
    return DatasetUuids(table_uuid=uuid or table_uuid(ns, store, segments, location=location), namespace_uuid=namespace_uuid(ns, segments[:-1]))


def of_namespace(ns: LanceNamespace, segments: list[str]) -> ControlUuids:
    """A namespace's uuid and its parent namespace's."""
    return ControlUuids(object_uuid=namespace_uuid(ns, segments), namespace_uuid=namespace_uuid(ns, segments[:-1]))
