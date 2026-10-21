"""How maintenance names a dataset: by the table the catalog's ``__manifest`` says it is, and by nothing else ([[LH-520]]).

A table the catalog makes lives at ``<root>/<table uuid>`` (ADR 0173 §5), so no name can be read off its location, and a
producer's ``lineage.dataset_id`` stamp is a claim, not the catalog's word ([[LH-141]]). Every maintenance reader that
acts on a dataset, or reports one the catalog does not know, asks the one rule here.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from service_kit.governed import fga
from service_kit.lakehouse import location_claims
from service_kit.lakehouse.base_refs import normalise
from service_kit.lakehouse.branch_layout import BRANCH_CONTAINER
from service_kit.lakehouse.naming import uuid_of
from service_kit.openlineage import DatasetUuids


log = logging.getLogger(__name__)


class TableIds(BaseModel):
    """The catalog's table ids by location, read off every maintained root's ``__manifest``."""

    #: Normalised absolute location -> the table's ``object_id``.
    by_location: dict[str, str] = Field(default_factory=dict)
    #: A namespace's ``object_id`` -> the uuid its own ``__manifest`` row holds, read in the same pass as the tables.
    namespace_uuids: dict[str, str] = Field(default_factory=dict)
    #: A table's ``object_id`` -> its uuid, read from its location's claim the first time a reader asks (:func:`table_uuid`).
    table_uuids: dict[str, str] = Field(default_factory=dict)
    #: ``False`` when a root's manifest could not be read: a table missing from ``by_location`` may then still exist.
    complete: bool = True

    def of(self, location: str) -> str | None:
        return self.by_location.get(normalise(location))

    def location_of(self, table: str) -> str | None:
        """The normalised location the table's ``__manifest`` row names, or ``None`` when no row names the table."""
        return next((location for location, named in self.by_location.items() if named == table), None)

    def table_of(self, dataset: str) -> str | None:
        """The table a discovered dataset belongs to: the one whose row names its location, or whose branch it is.

        A branch is a whole dataset discovery lists at ``<table>/tree/<branch>``, and no row names it, so its table is the
        one whose location comes before a ``tree`` segment of its path. Every prefix is asked, since a branch's own name
        may hold ``tree`` and a registered table's location may too ([[LH-520]]).
        """
        segments = normalise(dataset).split("/")
        held = (self.by_location.get("/".join(segments[:end])) for end, segment in enumerate(segments) if segment == BRANCH_CONTAINER)
        return self.of(dataset) or next((table for table in held if table is not None), None)


def manifest_uuid(row: dict[str, Any]) -> str | None:
    """The uuid a namespace's ``__manifest`` row holds in its JSON ``metadata`` (``catalog/dir/index.md`` § Manifest Table
    Schema), or ``None`` when the row holds none or its metadata does not parse."""
    try:
        metadata = json.loads(row.get("metadata") or "{}")
    except ValueError:
        return None
    return uuid_of(metadata)


class PolicyOwners(BaseModel):
    """The objects whose maintenance policy may govern one table (ADR 0173 §8): the table, then its namespaces."""

    table_uuid: str
    #: The uuids of the table's ancestor namespaces, outermost first.
    namespace_uuids: list[str]


def table_uuid(names: TableIds, table: str, *, claims: location_claims.ClaimStore) -> str | None:
    """The uuid of ``table``: its location's claim's, and only while that claim names ``table``, since a claim another
    table holds is that table's uuid. ``None`` when no row or claim records one.

    Read once per table a reader asks about and kept on ``names``, rather than for every row of every manifest: the event
    lane reads a whole bucket's ``__manifest`` per write. A store failure raises.
    """
    if table in names.table_uuids:
        return names.table_uuids[table]
    location = names.location_of(table)
    found = location_claims.read(claims, location) if location is not None else None
    if found is None or found.claim.table != table or found.claim.uuid is None:
        return None
    names.table_uuids[table] = found.claim.uuid
    return found.claim.uuid


def read_uuids(names: TableIds, table: str, *, claims: location_claims.ClaimStore, delimiter: str) -> DatasetUuids:
    """The uuids of ``table`` and of its namespace, for the events that name it (ADR 0173 §10).

    Best-effort, as the events are: a uuid that cannot be read is ``None`` and the event still goes.
    """
    namespace = fga.parent_namespace_id(table, delimiter=delimiter)
    namespace_uuid = names.namespace_uuids.get(namespace) if namespace else None
    try:
        uuid = table_uuid(names, table, claims=claims)
    except Exception as exc:  # noqa: BLE001 — an unread claim costs the event its uuid, never the event
        log.warning("maintenance_table_uuid_unread", extra={"table": table, "error": f"{type(exc).__name__}: {exc}"})
        uuid = None
    return DatasetUuids(table_uuid=uuid, namespace_uuid=namespace_uuid)


def policy_owners(names: TableIds, table: str, *, claims: location_claims.ClaimStore, delimiter: str) -> PolicyOwners | None:
    """The uuids a maintenance policy for ``table`` is kept under, or ``None`` when the table's or an ancestor
    namespace's cannot be read.

    ``None`` refuses the table this pass, never maintains it under the defaults: the record it cannot find may be a
    legal hold (``cleanup_enabled=False``), and expiring without it would reclaim history its owner kept.
    """
    try:
        uuid = table_uuid(names, table, claims=claims)
    except Exception as exc:  # noqa: BLE001 — an unread claim refuses the table, logged; the next pass reads it again
        log.warning("maintenance_table_uuid_unread", extra={"table": table, "error": f"{type(exc).__name__}: {exc}"})
        return None
    if uuid is None:
        return None
    segments = table.split(delimiter)
    ancestors = [delimiter.join(segments[:depth]) for depth in range(1, len(segments))]
    unnamed = [ancestor for ancestor in ancestors if ancestor not in names.namespace_uuids]
    if unnamed:
        log.warning("maintenance_namespace_uuid_unread", extra={"table": table, "namespaces": unnamed})
        return None
    return PolicyOwners(table_uuid=uuid, namespace_uuids=[names.namespace_uuids[ancestor] for ancestor in ancestors])


def under_a_declared_root(dataset: str, declared_roots: tuple[str, ...]) -> bool:
    """Whether ``dataset`` lives in a store the platform owns and opens by explicit URI, never registered ([[LH-164]]).

    The model registry is one: no ``__manifest`` row names its datasets, so they are accounted for rather than reported
    or refused as unnamed. Matched with the trailing delimiter, because a bare prefix match would declare
    ``medallion/models-scratch`` platform-owned for sharing eight characters.
    """
    return f"{normalise(dataset)}/".startswith(tuple(f"{normalise(root)}/" for root in declared_roots))
