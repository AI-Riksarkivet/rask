"""Shared FastAPI dependencies (Annotated type aliases)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from typing import Annotated, Any

from fastapi import Depends, Header, Query, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    InternalError,
    LanceNamespace,
    PermissionDeniedError,
)
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel, ConfigDict

from catalog.core import run_context
from catalog.core.config import Settings, StorePlane, get_settings
from catalog.core.identifiers import is_spelled_as_a_uuid, parse_identifier, transaction_table
from catalog.core.lineage_emit import LineageEmitter, NoopEmitter, OriginatorBoundEmitter, is_person_subject
from catalog.core.namespace import NO_WAREHOUSE, NamespaceConnections
from catalog.core.run_context import RunContext
from catalog.core.vending import CredentialVendor, ModeBVendor
from catalog.services import dataplane, object_uuids, provenance_namespace, table_claims, warehouses
from catalog.services.table_claims import DoorTable, ResolvedTable
from lakehouse import trash
from lakehouse.control_emit import ControlEmitter, NoopControlEmitter
from lakehouse.control_events import CatalogControlEvent, ControlUuids
from lakehouse.governed import fga
from lakehouse.lineage_events import DatasetUuids
from lakehouse.naming import CONTROL_ID_RE
from lakehouse.outage import store_refusal
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

SettingsDep = Annotated[Settings, Depends(get_settings)]


class ServedWarehouse(BaseModel):
    """Where a warehouse's names are served from, and whether every operation beneath it is suspended."""

    model_config = ConfigDict(frozen=True)

    root_uri: str
    #: The object store the root is reached at; ``None`` is the estate's ([[LH-067]]).
    endpoint: str | None = None
    deactivated: bool = False


async def _resolve_warehouse_root(request: Request, settings: Settings, warehouse_id: str) -> ServedWarehouse | None:
    """Where warehouse ``warehouse_id`` is served from (:func:`_read_warehouse_root`), read once per request.

    A request resolves a warehouse once and every later resolution of it in the same request reads that answer: a
    listing of N tables resolves each through its first segment, and N reads of one warehouse record per listing grew
    with the namespace. Live across requests, so a deactivation is in force from the next request.
    """
    roots: dict[str, ServedWarehouse] | None = getattr(request.state, _REQUEST_ROOTS, None)
    if roots is None:
        roots = {}
        setattr(request.state, _REQUEST_ROOTS, roots)
    if (resolved := roots.get(warehouse_id)) is not None:
        return resolved
    resolved = await _read_warehouse_root(settings, warehouse_id)
    if resolved is not None:
        roots[warehouse_id] = resolved
    return resolved


#: Where :func:`_resolve_warehouse_root` keeps the roots a request resolved, on that request's own state.
_REQUEST_ROOTS = "warehouse_roots"


async def _read_warehouse_root(settings: Settings, warehouse_id: str) -> ServedWarehouse | None:
    """Where the warehouse a name's first segment names is served from ([[LH-593]], ADR 0206), or ``None``.

    The estate's own warehouse is served from ``settings.root`` and has no record. ``None`` is a first segment that
    names no warehouse: its names are served from no catalog (:data:`catalog.core.namespace.NO_WAREHOUSE`), so a caller
    is answered for one exactly as for any absent name and learns nothing about which warehouses exist.

    The endpoint rides along because the same record read answers it ([[LH-067]]), and the connection builder judges
    it ([[LH-205]]). A deactivated warehouse is answered as one: who is told so is the reader's to decide
    (:func:`require_active_warehouse`), since the status is itself something a caller holding nothing must not learn.

    Raises:
        ServiceUnavailableError: The registry did not answer, or the record is not a warehouse's. Reading either as
            "no such warehouse" would answer a tenant's name as absent on a store blip.
        InternalError: The record is there and is not JSON, which no retry mends.
    """
    if warehouse_id == settings.estate_warehouse:
        return ServedWarehouse(root_uri=settings.root)
    if not names_a_registered_warehouse(warehouse_id):
        return None
    try:
        record = await run_in_threadpool(warehouses.get_warehouse, settings.registry_root, settings.storage_options(), warehouse_id)
    except (OSError, ValueError) as exc:
        log.warning("warehouse_lookup_failed", extra={"warehouse_id": warehouse_id, "error": str(exc)})
        raise store_refusal(exc, reading=f"reading warehouse {warehouse_id!r}", so="its names cannot be routed") from exc
    if record is None:
        return None
    return ServedWarehouse(root_uri=record["root_uri"], endpoint=record.get("endpoint") or None, deactivated=record.get("status", "active") != "active")


def names_a_registered_warehouse(warehouse_id: str) -> bool:
    """Whether ``warehouse_id`` is spelled as a registered warehouse's id may be; the estate's own is judged by its caller."""
    return CONTROL_ID_RE.fullmatch(warehouse_id) is not None


async def require_active_warehouse(request: Request, settings: Settings, warehouse_id: str) -> ServedWarehouse | None:
    """Where warehouse ``warehouse_id`` is served from, refusing one that is deactivated; ``None`` for no warehouse.

    Raises:
        PermissionDeniedError: The warehouse is deactivated, which suspends every operation beneath it. The status
            is read on every request, so a deactivation is in force from the next one.
    """
    served = await _resolve_warehouse_root(request, settings, warehouse_id)
    if served is not None and served.deactivated:
        raise PermissionDeniedError(f"warehouse {warehouse_id!r} is deactivated (quarantined); operations beneath it are suspended until it is reactivated")
    return served


async def namespace_for_root(
    request: Request, settings: Settings, root_uri: str, *, endpoint: str | None = None, plane: StorePlane = "metadata"
) -> LanceNamespace:
    """The (cached) namespace connection rooted at ``root_uri`` — one per warehouse bucket.

    PUBLIC on purpose — the warehouse/namespace lifecycle endpoints resolve bucket-rooted connections
    through it — and every caller must pair it with a warehouse deactivation-status gate, or a
    quarantined warehouse's bucket stays reachable through that caller.

    KEYED ON ``(root, endpoint)``, not on the root alone ([[LH-067]]). A root is immutable but an
    endpoint is caller-owned and may be corrected, and a root-only key would serve the connection built
    against the OLD store for the life of the process — an open against the wrong endpoint, which reads
    as a missing table rather than as stale configuration. And on the key pair in force, so a rotation
    builds anew ([[XC-082]], :class:`catalog.core.namespace.NamespaceConnections`). In the threadpool,
    because a build reaches the store."""
    return await run_in_threadpool(partial(_connections(request).connection, settings, root=root_uri, endpoint=endpoint, plane=plane))


async def estate_namespace(request: Request, settings: Settings) -> LanceNamespace:
    """The connection rooted at the estate's own root, under the key pair in force. In the threadpool, as above."""
    return await run_in_threadpool(_connections(request).connection, settings)


def _connections(request: Request) -> NamespaceConnections:
    """The connections the lifespan published on ``app.state``."""
    connections: NamespaceConnections = request.app.state.connections
    return connections


async def get_namespace(request: Request, settings: SettingsDep) -> LanceNamespace:
    """The directory catalog this request's ``{id}`` is served from: its first segment's warehouse ([[LH-593]]).

    Settings come by injection, never by calling the provider: FastAPI keys ``app.dependency_overrides`` on the
    dependency callable, so a body call would opt this dependency out of an override every ``SettingsDep`` route takes.
    """
    return await _catalog_at(request, settings, await _route_for(request, settings))


async def _catalog_at(request: Request, settings: Settings, served: ServedWarehouse | None) -> LanceNamespace:
    """The directory catalog a warehouse is ``served`` from, or the one that holds nothing.

    A first segment naming no warehouse is never read in the estate's own catalog: a row standing there under that
    segment would answer for it, with every table beneath it.
    """
    if served is None:
        return NO_WAREHOUSE
    if served.root_uri == settings.root:
        return await estate_namespace(request, settings)
    return await namespace_for_root(request, settings, served.root_uri, endpoint=served.endpoint)


async def _route_for(request: Request, settings: Settings) -> ServedWarehouse | None:
    """Where this request's ``{id}`` is served from, or ``None`` when its first segment names no warehouse.

    A collection route carries no id, and the root namespace's id is the delimiter alone, which parses to no segment:
    both are served from the estate's own catalog.
    """
    object_id = request.path_params.get("id")
    segments = parse_identifier(object_id) if object_id else []
    if not segments:
        return ServedWarehouse(root_uri=settings.root)
    return await require_active_warehouse(request, settings, segments[0])


async def get_namespace_root(request: Request, settings: SettingsDep) -> str:
    """The root the request's namespace connection is rooted at: its warehouse's.

    The register door needs it to judge a location it has not attached yet ([[LH-279]]): the backend
    resolves a relative location against exactly this root, so the location-exclusivity rule has to
    ask the same one. Resolved through the same routing as :func:`get_namespace`, so the two cannot
    disagree about which root a request lands in.
    """
    resolved = await _route_for(request, settings)
    return settings.root if resolved is None else resolved.root_uri


NamespaceRootDep = Annotated[str, Depends(get_namespace_root)]


async def namespace_for_warehouse(request: Request, settings: Settings, warehouse_id: str) -> LanceNamespace:
    """The directory catalog of ONE warehouse, for a caller holding a name rather than a request ``{id}``.

    A collection route (``GET /v1/table``) and a tick carry no ``{id}`` for :func:`get_namespace` to route by, so they
    resolve each name's first segment here, through the same lookup and connection cache the id routes use.

    Raises:
        PermissionDeniedError: The warehouse is deactivated.
    """
    return await _catalog_at(request, settings, await require_active_warehouse(request, settings, warehouse_id))


NamespaceDep = Annotated[LanceNamespace, Depends(get_namespace)]


async def get_provenance_namespace(request: Request, settings: SettingsDep) -> LanceNamespace | None:
    """The connection holding the catalog's provenance namespace, lineage's feed and edge list (ADR 0158): the estate's own,
    since the namespace lies in the estate's warehouse (``lance_catalog$provenance``) and the catalog creates it there at
    boot, whatever warehouse the request's table lies in. ``None`` when the catalog keeps none, the code default."""
    if not settings.provenance_namespace:
        return None
    return await estate_namespace(request, settings)


ProvenanceNamespaceDep = Annotated[LanceNamespace | None, Depends(get_provenance_namespace)]


def warehouse_object(settings: Settings, warehouse_id: str) -> str | None:
    """The FGA object the warehouse a name's first segment names is governed as, or ``None`` for a segment no warehouse id can be."""
    if warehouse_id == settings.estate_warehouse:
        return settings.fga_default_warehouse_object
    return f"warehouse:{warehouse_id}" if names_a_registered_warehouse(warehouse_id) else None


class Resolver:
    """The objects a request's names are governed as, each name resolved once per request (ADR 0173 §7, [[LH-524]]).

    A door is addressed by name and its grants are held by the uuid that name resolves to, so the router's authorization,
    the tier judge and the door itself read ONE resolution of a name: a rename or a re-create landing between two
    resolutions would have the door act on a table other than the one it authorized. Each name is resolved through the
    connection of the warehouse its first segment names, the one :func:`get_namespace` builds for the request's own ``{id}``.
    """

    def __init__(self, request: Request, settings: Settings) -> None:
        self._request = request
        self._settings = settings
        self._tables: dict[tuple[str, ...], ResolvedTable] = {}
        self._namespaces: dict[tuple[str, ...], str | None] = {}
        self._trashed: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = {}
        self._admitted: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = {}

    async def _connection(self, segments: list[str]) -> LanceNamespace:
        # A deactivated warehouse's names still resolve here: the router asks OpenFGA about the object a name
        # resolves to before anyone is told the warehouse is suspended (`fga_deps.authorize`), so a caller holding
        # nothing on it is answered as for any other name.
        return await _catalog_at(self._request, self._settings, await _resolve_warehouse_root(self._request, self._settings, segments[0]))

    async def require_active_warehouse(self, warehouse_id: str) -> None:
        """Refuse a name in a deactivated warehouse, asked once the caller is known to be admitted at the door.

        Raises:
            PermissionDeniedError: The warehouse is deactivated.
        """
        await require_active_warehouse(self._request, self._settings, warehouse_id)

    async def table(self, segments: list[str], *, ns: LanceNamespace | None = None) -> ResolvedTable:
        """The table ``segments`` names, resolved to its location and that location's claim (``ns``: the connection to read it through)."""
        key = tuple(segments)
        if not segments:
            return ResolvedTable(segments=[])
        if key not in self._tables:
            connection = ns if ns is not None else await self._connection(segments)
            self._tables[key] = await run_in_threadpool(table_claims.resolve, connection, table_claims.claim_store(self._settings), list(segments))
        return self._tables[key]

    async def namespace_uuid(self, segments: list[str]) -> str | None:
        """The uuid of the namespace ``segments`` names, or ``None`` when no namespace has that name (or for the root).

        Raises:
            UnidentifiedObjectError: The namespace carries no uuid, which no door leaves.
        """
        key = tuple(segments)
        if key not in self._namespaces:
            self._namespaces[key] = (
                await run_in_threadpool(object_uuids.resolve_namespace_uuid, await self._connection(segments), list(segments)) if segments else None
            )
        return self._namespaces[key]

    async def object(self, kind: str, segments: list[str]) -> str | None:
        """The FGA object ``kind`` ``segments`` names (``table:<uuid>``, ``namespace:<uuid>``), or ``None`` when nothing has that name.

        A namespace name of one segment is a warehouse ([[LH-593]]), governed as ``warehouse:<id>`` whether or not one
        is registered under it: asked about a warehouse that is not there, OpenFGA answers as it does for one the
        caller holds nothing on.

        Raises:
            UnidentifiedObjectError: The object carries no uuid, which no door leaves.
        """
        if kind == "table":
            resolved = await self.table(segments)
            return None if resolved.location is None else fga.table_object(resolved.require_uuid())
        if len(segments) == 1:
            return warehouse_object(self._settings, segments[0])
        uuid = await self.namespace_uuid(segments)
        return None if uuid is None else fga.namespace_object(uuid)

    async def trashed(self, kind: str, segments: list[str]) -> list[dict[str, Any]]:
        """The trash records of every ``kind`` object dropped under the name ``segments``, oldest drop first (ADR 0173 §9).

        A trash door stays addressed by name, and several dropped objects may have held it: the request's ``?uuid=``
        names one, and then only its record is among them.
        """
        key = (kind, tuple(segments))
        if key not in self._trashed:
            canonical = fga.canonical_object_id(segments)
            records = await run_in_threadpool(trash.named, self._settings.registry_root, self._settings.storage_options(), canonical, kind=kind)
            wanted = self._request.query_params.get("uuid")
            self._trashed[key] = [r for r in records if (uuid := trash.object_uuid(r)) is not None and wanted in (None, uuid)]
        return self._trashed[key]

    async def trashed_as_transaction(self, segments: list[str]) -> list[dict[str, Any]]:
        """The trash record a transaction id ``<table id>$<uuid>`` names: a recoverable drop's (ADR 0146), or none.

        A drop that keeps its bytes for the purge answers its record's uuid as the spec's transaction id, so its deletion
        is tracked through DescribeTransaction on the dropped table's name.
        """
        if len(segments) < 2:
            return []
        return [record for record in await self.trashed("table", segments[:-1]) if trash.object_uuid(record) == segments[-1]]

    def admit_trashed(self, kind: str, segments: list[str], records: list[dict[str, Any]]) -> None:
        """Record which of :meth:`trashed` the request's authorization let its caller act on."""
        self._admitted[(kind, tuple(segments))] = records

    async def admitted_trash(self, kind: str, segments: list[str]) -> list[dict[str, Any]]:
        """The trashed objects under the name ``segments`` that this request's caller may act on.

        Those the router's authorization admitted, or every one where authorization is off. A request authorization
        admitted none for, with authorization on, acts on none: no door reaches this unauthorized.

        Raises:
            InternalError: Authorization is on and decided nothing about this name's trash.
        """
        key = (kind, tuple(segments))
        if key in self._admitted:
            return self._admitted[key]
        if self._settings.fga_enabled:
            raise InternalError(f"no authorization decided which trashed {kind} under {fga.canonical_object_id(segments)!r} this caller may act on")
        return await self.trashed(kind, segments)


#: A trash door's ``?uuid=``: which of the objects dropped under its name it acts on (ADR 0173 §9, [[LH-525]]). Declared
#: on each trash door for its contract; :meth:`Resolver.trashed` reads it, so the router's authorization and the door
#: judge the same candidates.
TrashedUuidQuery = Annotated[
    str | None, Query(description="The uuid of the dropped object to act on, when several dropped objects held this name; the answer lists them.")
]


def get_resolver(request: Request, settings: SettingsDep) -> Resolver:
    """This request's :class:`Resolver`: FastAPI solves one dependency once per request, so every reader shares it."""
    return Resolver(request, settings)


ResolverDep = Annotated[Resolver, Depends(get_resolver)]


async def get_resolved_table(request: Request, ns: NamespaceDep, resolver: ResolverDep) -> ResolvedTable:
    """The table the request's ``{id}`` names, resolved once to its location and that location's claim (ADR 0173 §7).

    The resolution the router's authorization read, when it read one, so a door acting on it never resolves the name
    again.
    """
    return await resolver.table(parse_identifier(request.path_params["id"]), ns=ns)


ResolvedTableDep = Annotated[ResolvedTable, Depends(get_resolved_table)]


def get_door_table(ns: NamespaceDep, settings: SettingsDep, resolved: ResolvedTableDep) -> DoorTable:
    """The table the request's ``{id}`` resolved to, as a door reading or writing its data or metadata acts on it ([[LH-530]])."""
    return _door_table(ns, settings, resolved)


def _door_table(ns: LanceNamespace, settings: Settings, resolved: ResolvedTable) -> DoorTable:
    """A door's table, answering whether a write of an operation on it is announced as the emitter judges it
    (`catalog.services.provenance_namespace`), which decides whether its author is recorded (ADR 0187)."""
    return DoorTable(ns, table_claims.claim_store(settings), resolved, announces=provenance_namespace.announcer(resolved.segments, settings))


DoorTableDep = Annotated[DoorTable, Depends(get_door_table)]


async def get_transaction_table(ns: NamespaceDep, settings: SettingsDep, resolver: ResolverDep, request: Request) -> DoorTable:
    """The table the request's transaction ``{id}`` (``<table id>$<transaction>``) names, as its door acts on it ([[LH-221]]).

    The resolution the router's authorization read, as for every table door.

    Raises:
        InvalidInputError: The id names no table.
    """
    resolved = await resolver.table(transaction_table(parse_identifier(request.path_params["id"])), ns=ns)
    return _door_table(ns, settings, resolved)


TransactionTableDep = Annotated[DoorTable, Depends(get_transaction_table)]


async def get_data_namespace(request: Request, settings: SettingsDep, ns: NamespaceDep) -> LanceNamespace:
    """The request's namespace connection under the DATA plane's object-store bounds ([[LH-247]]).

    For the native operations that move rows (`/query`, `count_rows`, inserts, merges, index builds,
    `analyze_plan`): the metadata connection's whole-request bound would cut off a legitimate transfer
    (`CatalogReadBounds`). It is the data twin OF the connection :func:`get_namespace` resolved, found by
    identity, so it follows the same warehouse routing without a second registry read, and a connection
    the catalog did not build (a test's override) is handed back unchanged.
    """
    connections: NamespaceConnections | None = getattr(request.app.state, "connections", None)
    twin = await run_in_threadpool(connections.data_twin, settings, ns) if connections is not None else None
    return twin or ns


DataNamespaceDep = Annotated[LanceNamespace, Depends(get_data_namespace)]


def get_fga_client(request: Request) -> OpenFgaClient | None:
    """The wired OpenFGA client from catalog.state, or ``None`` when FGA isn't provisioned.

    One place that knows where the client lives (``app.state.fga``), injected like
    ``NamespaceDep`` instead of re-spelled as ``getattr(request.app.state, "fga", None)``
    in every create/seed/list handler.
    """
    return getattr(request.app.state, "fga", None)


FgaClientDep = Annotated[OpenFgaClient | None, Depends(get_fga_client)]


def get_storage_options(settings: SettingsDep) -> StorageOptions:
    """Object-store options (S3 credentials, region) for direct pylance access, under the DATA plane's bounds.

    Every door opens its pylance handle with these and reads or writes rows through it, and a handle's
    object-store bounds are fixed when it opens, so they are the data plane's (`CatalogReadBounds`). A
    door's first store call is the metadata connection's describe, which finds a silent store first.
    """
    return settings.storage_options(plane="data")


StorageOptionsDep = Annotated[StorageOptions, Depends(get_storage_options)]


#: The longest an originator claim may be. A notifier keys a recipient's durable state by it, so an unbounded
#: header would be an unbounded key in somebody else's durable state.
_MAX_ORIGINATOR = 128


def originator_hint(raw: str | None) -> str | None:
    """Reduce a caller-supplied ``x-lance-originator`` header to a person's subject, or ``None``.

    A TARGETING hint and nothing else: it never authorizes, because a notifier re-derives
    every recipient's visibility at delivery, so at worst a forged value puts a row in an inbox whose
    owner can already see the run's outputs. That is what makes an unverified header sound here — and
    it is also why the value must still be a PERSON. A userset (``role:<id>#assignee``), a wildcard, a
    ``user:``-prefixed object id and a role literal each address either everyone or nobody; the estate
    has addressed a recipient literally named ``*`` before.

    Dropped rather than coerced, on the same reasoning the project facet uses: an omitted originator
    reaches the author and no one else, while a repaired one could reach the wrong person.
    """
    if raw is None:
        return None
    value = raw.strip()
    if not value or len(value) > _MAX_ORIGINATOR:
        return None
    return value if is_person_subject(value) else None


def get_lineage_emitter(
    request: Request,
    x_lance_originator: Annotated[str | None, Header()] = None,
) -> LineageEmitter:
    """The lineage emitter built in the app lifespan, bound to THIS request's originator claim.

    The emitter is app-scoped and shared by every concurrent request; the claim is per-request. Binding
    them in a frozen wrapper keeps the transport shared without letting one caller's identity ride onto
    another caller's event.
    """
    emitter = getattr(request.app.state, "lineage_emitter", None)
    return OriginatorBoundEmitter(emitter if emitter is not None else NoopEmitter(), originator_hint(x_lance_originator))


LineageEmitterDep = Annotated[LineageEmitter, Depends(get_lineage_emitter)]


def get_run(request: Request) -> RunContext | None:
    """The run the request's spec Context names (:mod:`catalog.core.run_context`), or ``None``.

    Read from the request rather than declared as ``Header`` parameters, so the Context headers stay out of the
    spec operations' published contract.
    """
    return run_context.from_headers(request.headers)


RunDep = Annotated[RunContext | None, Depends(get_run)]


class ObjectUuids:
    """Reads the uuids a door's events name a table or namespace by (ADR 0173 §10), through the connection of its first segment's warehouse.

    Per request, because the routing is: an event names inputs that may live in another warehouse than the table the
    door wrote. Best-effort like the events it serves: what cannot be read is ``None``, and nothing raises into a door
    whose act has already happened.
    """

    def __init__(self, request: Request, settings: Settings) -> None:
        self._request = request
        self._settings = settings

    async def _connection(self, segments: list[str]) -> LanceNamespace | None:
        try:
            return await namespace_for_warehouse(self._request, self._settings, segments[0])
        except Exception as exc:  # noqa: BLE001 — an unroutable namespace is a uuid the event goes without
            log.info("event_uuids_unrouted", extra={"segments": segments, "error": str(exc)[:300]})
            return None

    async def table(self, segments: list[str], *, location: str | None = None, uuid: str | None = None) -> DatasetUuids:
        """A table's uuid and its namespace's: ``uuid`` when the door resolved one, else the claim at ``location``, else the name's."""
        ns = await self._connection(segments) if segments else None
        if ns is None:
            return DatasetUuids(table_uuid=uuid)
        claims = table_claims.claim_store(self._settings)
        return await run_in_threadpool(partial(object_uuids.of_table, ns, claims, segments, location=location, uuid=uuid))

    async def branch_identifier(self, segments: list[str], branch: str) -> str | None:
        """The incarnation of the table's ``branch`` now, read off the location its name resolves to; ``None`` when unread."""
        ns = await self._connection(segments) if segments else None
        if ns is None:
            return None
        try:
            resolved = await run_in_threadpool(table_claims.resolve, ns, table_claims.claim_store(self._settings), segments)
        except Exception as exc:  # noqa: BLE001 — an unresolved input is an incarnation the event goes without
            log.info("event_branch_identifier_unresolved", extra={"segments": segments, "error": str(exc)[:300]})
            return None
        if resolved.location is None:
            return None
        # `dataplane.branch_identifier` reads best-effort and answers None on any failure, so this raises nothing.
        return await run_in_threadpool(dataplane.branch_identifier, resolved.location, self._settings.storage_options(plane="data"), segments, branch)

    async def namespace(self, segments: list[str]) -> ControlUuids:
        """A namespace's uuid and its parent namespace's."""
        ns = await self._connection(segments) if segments else None
        return ControlUuids() if ns is None else await run_in_threadpool(object_uuids.of_namespace, ns, segments)

    async def namespace_of(self, name: str) -> str | None:
        """The uuid of the namespace the table or namespace ``name`` is in; ``None`` for a warehouse's own name or a failed read."""
        segments = [segment for segment in parse_identifier(name) if segment][:-1]
        ns = await self._connection(segments) if segments else None
        return None if ns is None else await run_in_threadpool(object_uuids.namespace_uuid, ns, segments)


def get_object_uuids(request: Request, settings: SettingsDep) -> ObjectUuids:
    """This request's :class:`ObjectUuids`."""
    return ObjectUuids(request, settings)


ObjectUuidsDep = Annotated[ObjectUuids, Depends(get_object_uuids)]


class UuidNamingControlEmitter:
    """The app's control emitter, naming each event's table or namespace by its uuids (ADR 0173 §10, [[LH-524]]).

    A door names a table or namespace by the uuid object it resolved (``object_id``, ``table:<uuid>``) and its name
    (``object_name``). One place rather than every door fills what the door did not set: ``object_uuid`` from
    ``object_id`` when it names a uuid object, and the uuid of the namespace the object is in, read by name after the
    door acted. A door that ends its object or moves it (a drop, a deregister, a rename) reads that uuid before it acts
    and sets it, and it stands. An object no uuid identifies is named by its name ([[LH-534]]) and keeps no ``object_uuid``.
    """

    def __init__(self, inner: ControlEmitter, uuids: ObjectUuids) -> None:
        self._inner = inner
        self._uuids = uuids

    async def emit(self, event: CatalogControlEvent) -> None:
        kind, _, key = event.object_id.partition(":")
        if kind in ("table", "namespace") and event.object_name is not None:
            update: dict[str, str | None] = {}
            if event.object_uuid is None and is_spelled_as_a_uuid(key):
                update["object_uuid"] = key
            if event.namespace_uuid is None:
                update["namespace_uuid"] = await self._uuids.namespace_of(event.object_name)
            event = event.model_copy(update=update)
        await self._inner.emit(event)


def get_control_emitter(request: Request, uuids: ObjectUuidsDep) -> ControlEmitter:
    """The control-plane change-event emitter built in the app lifespan — a no-op when emission is off."""
    emitter = getattr(request.app.state, "control_emitter", None)
    return NoopControlEmitter() if emitter is None else UuidNamingControlEmitter(emitter, uuids)


ControlEmitterDep = Annotated[ControlEmitter, Depends(get_control_emitter)]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def get_clock() -> Callable[[], datetime]:
    """The wall clock a door reads a deadline from: the erasure door sets a release's due time by it and the
    release tick judges it by the same one, so a test moves both with one override."""
    return _utc_now


ClockDep = Annotated[Callable[[], datetime], Depends(get_clock)]


def get_vendor(request: Request) -> CredentialVendor:
    """The credential vendor built in the app lifespan — Mode B (no direct creds) by default."""
    vendor = getattr(request.app.state, "vendor", None)
    return vendor if vendor is not None else ModeBVendor()


VendorDep = Annotated[CredentialVendor, Depends(get_vendor)]
