"""Shared FastAPI dependencies (Annotated type aliases)."""

from __future__ import annotations

import logging
import time
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
    ServiceUnavailableError,
)
from openfga_sdk import OpenFgaClient

from catalog.core import run_context
from catalog.core.config import Settings, StorePlane, get_settings
from catalog.core.identifiers import is_spelled_as_a_uuid, parse_identifier
from catalog.core.lineage_emit import LineageEmitter, NoopEmitter, OriginatorBoundEmitter, is_person_subject
from catalog.core.namespace import NamespaceConnections
from catalog.core.run_context import RunContext
from catalog.core.vending import CredentialVendor, ModeBVendor
from catalog.services import object_uuids, table_claims, warehouses
from catalog.services.table_claims import DoorTable, ResolvedTable
from service_kit.control_emit import ControlEmitter, NoopControlEmitter
from service_kit.control_events import CatalogControlEvent, ControlUuids
from service_kit.governed import fga
from service_kit.lakehouse import trash
from service_kit.openlineage import DatasetUuids


log = logging.getLogger(__name__)

SettingsDep = Annotated[Settings, Depends(get_settings)]


async def _resolve_warehouse_root(request: Request, settings: Settings, top_ns: str) -> tuple[str, str | None] | None:
    """The bound warehouse's ``(root_uri, endpoint)`` for ``top_ns`` (#3-A), or ``None`` → default root.

    The ENDPOINT rides along because the same live record read already answers it ([[LH-067]]), and
    the connection builder JUDGES it: a record naming another object store is refused there
    ([[LH-205]]) instead of being opened with the estate's key, and dropping it here would instead open
    the estate's store, a wrong-bucket read that looks like a missing table. ``None`` for the endpoint
    means the estate default.

    FAILS CLOSED. A registry read ERROR raises ``ServiceUnavailableError`` (503) — it is NOT swallowed to
    ``None``. Swallowing it would route a possibly-BOUND tenant's table to the shared default bucket on a
    transient S3 blip: a tenant-isolation violation caused by a hiccup. Only a clean "not found" (an
    unbound namespace) resolves to ``None`` and the default root.

    DEACTIVATION GATE (P2.3): if the bound warehouse is deactivated, raises ``PermissionDeniedError`` (403) —
    a quarantined warehouse suspends EVERY op on its bound namespaces (the tenant-offboarding step). Status is
    read LIVE, so a deactivate/activate takes effect on the very next request.

    (This docstring previously asserted the opposite — "swallowed to None ... can NEVER break an existing
    flow" — describing the pre-audit behavior while the code below fail-closed. A docstring that lies about
    a fail-open/fail-closed decision is worse than none: it is the sentence a future reader trusts.)

    Cached in-process, POSITIVES ONLY (bindings are immutable, so a resolved root is safe forever; a
    negative must not be cached — see the comment below). Blocking S3 read runs in the threadpool."""
    # Cache POSITIVES only: a binding is immutable, so a resolved binding is safe to cache forever. A negative
    # (unbound) result must NOT be cached — else a namespace looked up while still unbound, then bound on
    # another replica, would keep routing to the default bucket on this one (a table meant for the tenant's
    # bucket would land in the shared one — an isolation violation). Unbound namespaces re-read the
    # authoritative binding each request; that read only happens when warehouses_enabled (default off).
    # The cache holds the FULL binding record ({warehouse_id, root_uri}) so the deactivation check below has
    # the warehouse_id without a second binding read.
    cache: dict[str, dict[str, str]] = request.app.state.warehouse_binding_cache
    binding = _fresh_cached_binding(cache, top_ns, settings.warehouse_binding_cache_ttl_seconds)
    if binding is None:
        try:
            binding = await run_in_threadpool(warehouses.binding_for_namespace, settings.registry_root, settings.storage_options(), top_ns)
        except Exception as exc:
            # FAIL CLOSED on a registry READ ERROR. A legitimately-unbound namespace returns a clean None (no
            # exception) from binding_for_namespace, so reaching here means the read itself failed (S3 blip,
            # timeout). Falling through to the default shared bucket would physically create/read a
            # MAYBE-bound tenant's table in the wrong bucket — an isolation break from a transient fault. 503
            # instead; the caller retries once the registry is reachable.
            log.warning("warehouse_binding_lookup_failed", extra={"top_ns": top_ns, "error": str(exc)})
            raise ServiceUnavailableError(f"warehouse binding lookup failed for {top_ns!r}") from exc
        if binding is not None:
            # The freshness stamp rides INSIDE the record under a reserved key rather than in a
            # parallel dict: `evict_stale_bindings` mutates this very dict in place and scans its
            # VALUES for a warehouse_id, so a second structure would be one more thing to keep in
            # sync — and the one that drifts is the one nothing reads on the hot path.
            cache[top_ns] = {**binding, _CACHED_AT: str(time.monotonic())}
    if binding is None:
        return None
    # Deactivation gate (P2.3 lifecycle): a deactivated warehouse quarantines EVERY op on its bound
    # namespaces. Status is MUTABLE, so it is read LIVE each request (never cached, unlike the immutable
    # root_uri) — one metadata GET, only for bound namespaces (the minority; the whole lookup is skipped when
    # warehouses are off). FAIL-CLOSED on BOTH failure kinds (audit #3/#5): a transient registry read error is
    # wrapped as 503 (NOT an unhandled 500) — symmetric with the binding read above; and a MISSING warehouse
    # record for a still-bound namespace (a clean status None) is NOT-active → 403, never silently allowed.
    # THE WHOLE RECORD, not just the status, and it is the same single GET either way. The routing
    # path also needs the warehouse's own `endpoint` when it declares one ([[LH-067]]), and reading it
    # from this already-live read is what keeps "which store" as fresh as "is it active" — a cached
    # endpoint would outlive a correction to it, and the failure would be an open against the wrong
    # store rather than a stale allow.
    try:
        record = await run_in_threadpool(
            warehouses.get_warehouse,
            settings.registry_root,
            settings.storage_options(),
            binding["warehouse_id"],
        )
    except Exception as exc:
        log.warning(
            "warehouse_status_lookup_failed",
            extra={"top_ns": top_ns, "warehouse_id": binding["warehouse_id"], "error": str(exc)},
        )
        raise ServiceUnavailableError(f"warehouse status lookup failed for {binding['warehouse_id']!r}") from exc
    # A MISSING record is NOT active — the same fail-closed answer `warehouse_status` gave for a clean
    # `None`, kept explicit here now that this reads the record itself. An ABSENT `status` field is
    # active (records written before the lifecycle feature have none and are live).
    if record is None or record.get("status", "active") != "active":
        raise PermissionDeniedError(
            f"warehouse {binding['warehouse_id']!r} is deactivated (quarantined); operations on namespace {top_ns!r} are suspended until it is reactivated"
        )
    return binding["root_uri"], record.get("endpoint") or None


#: Reserved key stamping when a cached binding was resolved. Underscore-prefixed so it cannot collide
#: with a real record field (`top_ns` / `warehouse_id` / `root_uri`).
_CACHED_AT = "_cached_at"


def _fresh_cached_binding(cache: dict[str, dict[str, str]], top_ns: str, ttl_seconds: float) -> dict[str, str] | None:
    """The cached binding for ``top_ns`` if it is still fresh, evicting it if not (diff2 F10 item 3).

    The cache is POSITIVE-FOREVER on the premise that a binding is immutable, and #46 added
    broadcast eviction for the three mutations that break that premise. This is the FLOOR under that
    broadcast, because the broadcast is best-effort: it rides a pub/sub subscription with no
    dead-lettering, so a dropped event leaves an entry that nothing will ever evict.

    What that costs is narrower than it first looks and the finding's own text corrects it: warehouse
    STATUS is read live on every request and a missing record fails closed to 403, so a stale entry
    does NOT route at a deleted bucket. The residual is persistent 403s on a since-re-bound namespace
    until the process restarts, plus wrong-bucket routing during a partial-delete window or under
    warehouse-id reuse. A TTL bounds all three to the window rather than to the process lifetime.

    Minutes, not seconds: the read this avoids is one S3 GET on the request hot path, and bindings
    genuinely almost never change — the TTL is a backstop for a lost event, not a consistency
    mechanism. `0` disables it, restoring the pre-F10.3 forever-positive behaviour.
    """
    entry = cache.get(top_ns)
    if entry is None:
        return None
    if ttl_seconds <= 0:
        return entry
    if time.monotonic() - float(entry.get(_CACHED_AT) or 0.0) >= ttl_seconds:
        # pop by key, matching `evict_stale_bindings`' own concurrency argument: dict ops are atomic
        # under the GIL and a racing resolver simply re-reads the registry, which is the right outcome.
        cache.pop(top_ns, None)
        return None
    return entry


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
    """The backend namespace for this request.

    Default: the estate-rooted connection under the key pair in force. With ``warehouses_enabled`` (#3-A) and a
    request that targets a warehouse-BOUND top-level namespace, returns that warehouse's bucket-rooted
    connection instead — so create/describe/commit/list/drop all operate on the physically isolated bucket.
    An UNBOUND namespace (the common case) always falls through to the default root: existing single-bucket
    behaviour is untouched, and the binding lookup is skipped entirely when the feature is off."""
    # SETTINGS BY INJECTION, not by calling the provider. FastAPI keys `app.dependency_overrides` on the
    # dependency CALLABLE, so a body call to `get_settings()` opts this dependency out of the override
    # map without saying so: an override that reaches every `SettingsDep` route silently missed this
    # one, and a test exercising a warehouse-routed path under it would have been green against the
    # wrong configuration. `get_settings` is cached per request, so a route that already declares
    # `SettingsDep` pays nothing for this.
    resolved = await _route_for(request, settings)
    if resolved is None or resolved[0] == settings.root:
        return await estate_namespace(request, settings)
    return await namespace_for_root(request, settings, resolved[0], endpoint=resolved[1])


async def _route_for(request: Request, settings: Settings) -> tuple[str, str | None] | None:
    """The ``(root, endpoint)`` of the warehouse this request's ``{id}`` is bound to, or ``None`` for the default root."""
    if not settings.warehouses_enabled:
        return None
    object_id = request.path_params.get("id")
    if not object_id:  # collection routes (list/health) have no id to route by
        return None
    segments = parse_identifier(object_id)
    if not segments:
        # THE ROOT NAMESPACE. Its id IS the delimiter (spec), so it parses to no segments — and there is
        # no warehouse above the root to route to, which makes the default namespace the right answer
        # rather than an error. Indexing `[0]` here raised `IndexError` into the handler and answered
        # `GET /v1/namespace/$/list` with a 500 (measured on the deployed estate 2026-09-03). Only
        # reachable with `warehouses_enabled`, since the branch above returns first when it is off —
        # which is why every id with a segment routed fine and the one naming the root did not.
        return None
    return await _resolve_warehouse_root(request, settings, segments[0])


async def get_namespace_root(request: Request, settings: SettingsDep) -> str:
    """The root the request's namespace connection is rooted at: the bound warehouse's, else ``settings.root``.

    The register door needs it to judge a location it has not attached yet ([[LH-279]]): the backend
    resolves a relative location against exactly this root, so the location-exclusivity rule has to
    ask the same one. Resolved through the same routing as :func:`get_namespace`, so the two cannot
    disagree about which root a request lands in; for a warehouse-bound id that repeats the live status
    read, which only the register door pays.
    """
    resolved = await _route_for(request, settings)
    return settings.root if resolved is None else resolved[0]


NamespaceRootDep = Annotated[str, Depends(get_namespace_root)]


async def namespace_for_top_ns(request: Request, settings: Settings, top_ns: str) -> LanceNamespace:
    """The rooted connection for ONE top-level namespace — the per-seed half of :func:`get_namespace`.

    Collection routes (``GET /v1/table``) carry no ``{id}`` for `get_namespace` to route by, so their
    request-scoped ``ns`` is ALWAYS the default root — structurally blind to every warehouse-BOUND
    namespace's tables (measured live: the estate listing showed 2 of 9 while the per-namespace routes
    saw everything, because those routes resolve the binding per id). A collection route that wants a
    bound namespace's contents resolves it explicitly through this helper, one seed at a time, using
    the SAME binding lookup + per-warehouse connection cache the id routes use.
    """
    if not settings.warehouses_enabled:
        return await estate_namespace(request, settings)
    resolved = await _resolve_warehouse_root(request, settings, top_ns)
    if resolved is None or resolved[0] == settings.root:
        return await estate_namespace(request, settings)
    return await namespace_for_root(request, settings, resolved[0], endpoint=resolved[1])


NamespaceDep = Annotated[LanceNamespace, Depends(get_namespace)]


class Resolver:
    """The objects a request's names are governed as, each name resolved once per request (ADR 0173 §7, [[LH-524]]).

    A door is addressed by name and its grants are held by the uuid that name resolves to, so the router's authorization,
    the tier judge and the door itself read ONE resolution of a name: a rename or a re-create landing between two
    resolutions would have the door act on a table other than the one it authorized. Each name is resolved through the
    connection its top namespace routes to, the one :func:`get_namespace` builds for the request's own ``{id}``.
    """

    def __init__(self, request: Request, settings: Settings) -> None:
        self._request = request
        self._settings = settings
        self._tables: dict[tuple[str, ...], ResolvedTable] = {}
        self._namespaces: dict[tuple[str, ...], str | None] = {}
        self._trashed: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = {}
        self._admitted: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = {}

    async def _connection(self, segments: list[str]) -> LanceNamespace:
        return await namespace_for_top_ns(self._request, self._settings, segments[0])

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

        Raises:
            UnidentifiedObjectError: The object carries no uuid, which no door leaves.
        """
        if kind == "table":
            resolved = await self.table(segments)
            return None if resolved.location is None else fga.table_object(resolved.require_uuid())
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
    return DoorTable(ns, table_claims.claim_store(settings), resolved)


DoorTableDep = Annotated[DoorTable, Depends(get_door_table)]


async def get_data_namespace(request: Request, settings: SettingsDep, ns: NamespaceDep) -> LanceNamespace:
    """The request's namespace connection under the DATA plane's object-store bounds ([[LH-247]]).

    For the native operations that move rows (`/query`, `count_rows`, inserts, merges, index builds,
    `analyze_plan`): the metadata connection's whole-request bound would cut off a legitimate transfer
    (`CatalogReadBounds`). It is the data twin OF the connection :func:`get_namespace` resolved, found by
    identity, so it follows the same warehouse routing without a second binding read, and a connection
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


def get_storage_options(settings: SettingsDep) -> dict[str, str]:
    """Object-store options (S3 credentials, region) for direct pylance access, under the DATA plane's bounds.

    Every door opens its pylance handle with these and reads or writes rows through it, and a handle's
    object-store bounds are fixed when it opens, so they are the data plane's (`CatalogReadBounds`). A
    door's first store call is the metadata connection's describe, which finds a silent store first.
    """
    return settings.storage_options(plane="data")


StorageOptionsDep = Annotated[dict[str, str], Depends(get_storage_options)]


#: The longest an originator claim may be. It becomes an inbox actor id, so an unbounded header would
#: be an unbounded actor name in somebody else's durable state.
_MAX_ORIGINATOR = 128


def originator_hint(raw: str | None) -> str | None:
    """Reduce a caller-supplied ``x-lance-originator`` header to a person's subject, or ``None``.

    A TARGETING hint and nothing else: it never authorizes, because the notifications plane re-derives
    every recipient's visibility at delivery, so at worst a forged value puts a row in an inbox whose
    owner can already see the run's outputs. That is what makes an unverified header sound here — and
    it is also why the value must still be a PERSON. A userset (``team:acme#member``), a wildcard, a
    ``user:``-prefixed object id and a role literal each address either everyone or nobody; the estate
    has written into an inbox actor literally named ``*`` before.

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
    """Reads the uuids a door's events name a table or namespace by (ADR 0173 §10), through the connection its top namespace routes to.

    Per request, because the routing is: an event names inputs that may live in another warehouse than the table the
    door wrote. Best-effort like the events it serves: what cannot be read is ``None``, and nothing raises into a door
    whose act has already happened.
    """

    def __init__(self, request: Request, settings: Settings) -> None:
        self._request = request
        self._settings = settings

    async def _connection(self, segments: list[str]) -> LanceNamespace | None:
        try:
            return await namespace_for_top_ns(self._request, self._settings, segments[0])
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

    async def namespace(self, segments: list[str]) -> ControlUuids:
        """A namespace's uuid and its parent namespace's."""
        ns = await self._connection(segments) if segments else None
        return ControlUuids() if ns is None else await run_in_threadpool(object_uuids.of_namespace, ns, segments)

    async def namespace_of(self, name: str) -> str | None:
        """The uuid of the namespace the table or namespace ``name`` is in; ``None`` for a top-level namespace or a failed read."""
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
