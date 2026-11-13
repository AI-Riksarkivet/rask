"""The name a table or namespace holds now, read off the uuid object its grants are held by ([[LH-591]]).

A grant names ``table:<uuid>`` or ``namespace:<uuid>`` (ADR 0173 §8), so a surface listing grants names the object as its
callers address it only by asking the catalog which name holds that uuid now. Both answers come from what the catalog
keeps on each root: every namespace and table is a row of the root's ``__manifest``, a namespace's row carrying its
``rask.uuid`` in its JSON ``metadata`` (``lance_docs/ns_catalog/catalog/dir/index.md`` § Manifest Table Schema), and a
table's uuid is in its location's claim, whose holder is the name a rename handed it to
(:func:`catalog.services.table_claims.current_name`). A table the catalog makes lives at ``<root>/<uuid>`` (ADR 0173 §5),
so its row is found by its location alone and one claim read confirms it; a registered table keeps its registrar's
location, so its claim is read only while a uuid asked about is still unnamed.

A name is a label beside the uuid, never what a decision reads, so a root whose index or a claim that cannot be read
leaves its objects unnamed and is logged at WARNING, rather than failing the answer that lists them. Every function is
BLOCKING IO; callers threadpool it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable

import lance
import pyarrow.fs as pafs
from lance_namespace import InternalError, ServiceUnavailableError
from pydantic import BaseModel

from catalog.core.config import Settings, shared_lance_session
from catalog.core.identifiers import is_spelled_as_a_uuid
from catalog.services import manifest_reclaim, table_claims
from lakehouse import location_claims
from lakehouse.governed import fga
from lakehouse.location_claims import ClaimStore
from lakehouse.naming import uuid_of
from lakehouse.objectfs import fs_and_base
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

_COLUMNS = ["object_id", "object_type", "location", "metadata"]


class _Row(BaseModel):
    """One namespace's or table's row of a root's ``__manifest``."""

    object_id: str
    object_type: str
    location: str | None = None
    metadata: str | None = None

    def namespace_uuid(self) -> str | None:
        if self.object_type != "namespace":
            return None
        try:
            properties = json.loads(self.metadata or "{}")
        except ValueError:
            return None
        return uuid_of(properties) if isinstance(properties, dict) else None

    def table_location(self, root: str) -> str | None:
        """The row's table's absolute location: its relative ``location`` joined onto ``root``, or itself when it names a scheme."""
        stored = (self.location or "").strip().strip("/")
        if self.object_type != "table" or not stored:
            return None
        return stored if "://" in stored else f"{root}/{stored}"


def current_names(settings: Settings, objects: Iterable[str]) -> dict[str, str]:
    """The name each ``table:<uuid>`` and ``namespace:<uuid>`` in ``objects`` holds now, keyed by the object.

    An object of any other type, one whose id is not a uuid, and one no root names now (gone, trashed, or unread) is
    absent from the answer.
    """
    asked = list(objects)
    tables = {ident for obj in asked if (ident := _uuid_of("table", obj)) is not None}
    namespaces = {ident for obj in asked if (ident := _uuid_of("namespace", obj)) is not None}
    if not tables and not namespaces:
        return {}
    rows = [(root, row) for root in _roots(settings) for row in _rows(root, settings.storage_options())]
    locations = [location for root, row in rows if (location := row.table_location(root)) is not None]
    return _namespace_names([row for _, row in rows], namespaces) | _table_names(table_claims.claim_store(settings), locations, tables)


def _namespace_names(rows: list[_Row], namespaces: set[str]) -> dict[str, str]:
    """The name each namespace in ``namespaces`` holds: the ``object_id`` of the row recording its uuid."""
    named: dict[str, str] = {}
    for row in rows:
        uuid = row.namespace_uuid()
        if uuid is not None and uuid in namespaces:
            named[fga.namespace_object(uuid)] = row.object_id
    return named


def _table_names(store: ClaimStore, locations: list[str], tables: set[str]) -> dict[str, str]:
    """The name each table in ``tables`` holds, read off the claims on ``locations``.

    A location named after an asked uuid is the catalog's own layout (ADR 0173 §5) and is read first; a registered
    table's location carries no uuid, so those are read only while an asked table is still unnamed.
    """
    made = [location for location in locations if _last_segment(location) in tables]
    registered = [location for location in locations if not is_spelled_as_a_uuid(_last_segment(location))]
    named: dict[str, str] = {}
    for location in [*made, *registered]:
        if len(named) == len(tables):
            break
        if (found := _claimed_name(store, location, tables)) is not None:
            named[found[0]] = found[1]
    return named


def _claimed_name(store: ClaimStore, location: str, tables: set[str]) -> tuple[str, str] | None:
    """The table object and name the claim on ``location`` holds, while it is held for one of ``tables``."""
    try:
        found = location_claims.read(store, location)
    except (OSError, ValueError) as exc:
        log.warning("object_name_claim_unread", extra={"location": location, "error": f"{type(exc).__name__}: {exc}"})
        return None
    if found is None or found.claim.released_at is not None:
        return None
    uuid = found.claim.uuid
    if uuid is None or uuid not in tables:
        return None
    return fga.table_object(uuid), found.claim.table


def _last_segment(location: str) -> str:
    return location.rstrip("/").rsplit("/", 1)[-1]


def _roots(settings: Settings) -> list[str]:
    """Every root an index can be at (:func:`manifest_reclaim.roots`); none when the warehouse registry cannot be read."""
    try:
        return manifest_reclaim.roots(settings)
    except (ServiceUnavailableError, InternalError) as exc:
        log.warning("object_name_roots_unread", extra={"error": f"{type(exc).__name__}: {exc}"})
        return []


def _uuid_of(kind: str, obj: str) -> str | None:
    typed, _, ident = obj.partition(":")
    return ident if typed == kind and is_spelled_as_a_uuid(ident) else None


def _rows(root: str, storage_options: StorageOptions) -> list[_Row]:
    """The namespace and table rows of ``root``'s ``__manifest``; none for a root holding no index or one that cannot be read."""
    try:
        fs, base = fs_and_base(root, storage_options)
        if fs.get_file_info(f"{base}/{manifest_reclaim.MANIFEST_TABLE}/_versions").type != pafs.FileType.Directory:
            return []
        index = lance.dataset(f"{root}/{manifest_reclaim.MANIFEST_TABLE}", storage_options=storage_options.in_clear(), session=shared_lance_session())
        read = index.to_table(columns=_COLUMNS, filter="object_type IN ('namespace', 'table')").to_pylist()
    except (OSError, ValueError, RuntimeError) as exc:
        log.warning("object_name_index_unread", extra={"root": root, "error": f"{type(exc).__name__}: {exc}"})
        return []
    return [_Row.model_validate(row) for row in read if row.get("object_id")]
