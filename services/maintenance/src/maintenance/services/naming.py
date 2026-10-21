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

    def names(self) -> set[str]:
        return set(self.by_location.values())


def manifest_uuid(row: dict[str, Any]) -> str | None:
    """The uuid a namespace's ``__manifest`` row holds in its JSON ``metadata`` (``catalog/dir/index.md`` § Manifest Table
    Schema), or ``None`` when the row holds none or its metadata does not parse."""
    try:
        metadata = json.loads(row.get("metadata") or "{}")
    except ValueError:
        return None
    return uuid_of(metadata)


def read_uuids(names: TableIds, table: str, *, claims: location_claims.ClaimStore, delimiter: str) -> DatasetUuids:
    """The uuids of ``table`` and of its namespace, for the events that name it (ADR 0173 §10).

    The table's is its location's claim's, and only while that claim names ``table``: a claim another table holds is
    that table's uuid. Read here, once per table an event names, rather than for every row of every manifest: the event
    lane reads a whole bucket's ``__manifest`` per write. Best-effort, as the events are: a uuid that cannot be read is
    ``None`` and the event still goes.
    """
    namespace = fga.parent_namespace_id(table, delimiter=delimiter)
    namespace_uuid = names.namespace_uuids.get(namespace) if namespace else None
    location = names.location_of(table)
    if location is None:
        return DatasetUuids(namespace_uuid=namespace_uuid)
    try:
        found = location_claims.read(claims, location)
    except Exception as exc:  # noqa: BLE001 — an unread claim costs the event its uuid, never the event
        log.warning("maintenance_table_uuid_unread", extra={"table": table, "error": f"{type(exc).__name__}: {exc}"})
        found = None
    claim = found.claim if found is not None else None
    return DatasetUuids(table_uuid=claim.uuid if claim is not None and claim.table == table else None, namespace_uuid=namespace_uuid)


def under_a_declared_root(dataset: str, declared_roots: tuple[str, ...]) -> bool:
    """Whether ``dataset`` lives in a store the platform owns and opens by explicit URI, never registered ([[LH-164]]).

    The model registry is one: no ``__manifest`` row names its datasets, so they are accounted for rather than reported
    or refused as unnamed. Matched with the trailing delimiter, because a bare prefix match would declare
    ``medallion/models-scratch`` platform-owned for sharing eight characters.
    """
    return f"{normalise(dataset)}/".startswith(tuple(f"{normalise(root)}/" for root in declared_roots))
