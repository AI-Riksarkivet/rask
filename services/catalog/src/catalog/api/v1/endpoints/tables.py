"""Table metadata + lifecycle endpoints (no data plane)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from typing import Annotated, Any
from uuid import uuid4

import lance
from fastapi import APIRouter, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    DeclareTableRequest,
    DeclareTableResponse,
    DeregisterTableRequest,
    DeregisterTableResponse,
    DescribeTableRequest,
    DescribeTableResponse,
    DropTableRequest,
    DropTableResponse,
    GetTableStatsRequest,
    GetTableStatsResponse,
    InternalError,
    InvalidInputError,
    LanceNamespace,
    LanceNamespaceError,
    ListNamespacesRequest,
    ListTablesRequest,
    ListTablesResponse,
    PermissionDeniedError,
    RegisterTableRequest,
    RegisterTableResponse,
    RenameTableRequest,
    RenameTableResponse,
    RestoreTableRequest,
    RestoreTableResponse,
    TableAlreadyExistsError,
    TableExistsRequest,
    TableNotFoundError,
)

from catalog.api import idempotency as idem
from catalog.api import lineage_deps, listings
from catalog.api.announcing import Announcer, AnnouncerDep, Filing
from catalog.api.authz import decisions, door_gates, hierarchy, object_grants, visible_pages, write_doors
from catalog.api.authz.context import AuthzContext, AuthzContextDep
from catalog.api.dependencies import (
    ControlEmitterDep,
    DoorTableDep,
    LineageEmitterDep,
    NamespaceDep,
    NamespaceRootDep,
    ObjectUuids,
    ObjectUuidsDep,
    ResolvedTableDep,
    Resolver,
    ResolverDep,
    SettingsDep,
    StorageOptionsDep,
    TrashedUuidQuery,
    VendorDep,
    declared_base_judge,
    governed_storage,
    namespace_for_warehouse,
)
from catalog.api.pagination import ListLimit
from catalog.api.rask_params import RaskFlag
from catalog.api.security import ANONYMOUS_SUBJECT, Caller, CurrentToken
from catalog.core import json_arrow, native, provenance_guard, table_objects
from catalog.core.config import Settings
from catalog.core.formats import refuse_properties_kept_nowhere, reject_unsupported_properties
from catalog.core.identifiers import MAX_NAMESPACE_DEPTH, UUID_PROPERTY, parse_identifier, reconcile_body_id, rename_destination
from catalog.core.lineage_emit import (
    DECLARE_TABLE,
    DEREGISTER_TABLE,
    DROP_TABLE,
    REGISTER_TABLE,
    RENAME_TABLE,
    RESTORE_TABLE,
)
from catalog.core.namespace import open_resolved_dataset_unchecked, warn_if_mixed_file_versions
from catalog.core.pagination import paginate
from catalog.core.vending import (
    CredentialVendor,
    ModeBVendor,
    Tier,
    VendableBases,
    dataset_facts,
    require_vendable_bases,
    role_session_name,
    unsanctioned_bases,
    vend_or_refuse,
)
from catalog.schemas import ProtectionResponse, SetProtectionRequest, TrashEntry
from catalog.services import protections, table_bases, table_claims, trash_doors, warehouses
from catalog.services.announcement_intents import Announcement, ControlMessage, Evidence, NameResolves, TrashedUnder, Unregistered
from catalog.services.table_claims import DoorTable, ResolvedTable
from catalog.services.tables import creation, hidden_branches, reads, refs, registering, rename, retirement, schema, writes
from lakehouse.events.control_emit import ControlEmitter, emit_control
from lakehouse.events.control_events import CatalogControlEvent, ControlAction
from lakehouse.events.lineage_events import DatasetUuids
from lakehouse.governed import fga_ids
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit
from lakehouse.lancekit.features import CREATE_FORMAT, table_file_format
from lakehouse.naming import uuid_of
from lakehouse.problems.ns_errors import PartiallyAppliedError
from lakehouse.records import base_registry, location_claims, object_records, protection, trash
from lakehouse.records.protection import ProtectionRecord
from lakehouse.storage.options import StorageOptions


log = logging.getLogger(__name__)

spec_router = APIRouter(prefix="/v1/table", tags=["table"])

#: THE MANAGEMENT SURFACE ([[LH-021]]). rask-only governance mounts here, not on the spec prefix: a
#: spec client meeting `undrop` or `protection` on `/v1/table` has no way to tell a rask verb from a
#: Lance one. Deletion protection and the trash are the clearest case — the spec has `drop` and no
#: notion of a drop that can be taken back. It inherits the same three rungs as the
#: spec router (`api/v1/router.py`), so moving a route does not move it out of its gate.
management_router = APIRouter(prefix="/management/v1/table", tags=["table-management"])


def _collect_tables(ns: LanceNamespace, warehouse_id: str) -> list[str]:
    """Every table in warehouse ``warehouse_id``, fully qualified, declared ones included: each namespace's, depth-first.

    Synchronous and run in a threadpool by the caller: the native namespace client is blocking, and
    a walk of N namespaces is N blocking calls. ``ns`` is the warehouse's own directory catalog. A namespace that
    cannot be listed is logged and left out, so one bad namespace never empties the listing.
    """
    found: list[str] = []
    stack: list[list[str]] = [[warehouse_id]]
    seen: set[tuple[str, ...]] = set()
    while stack:
        parent = stack.pop()
        if len(parent) >= MAX_NAMESPACE_DEPTH or tuple(parent) in seen:
            continue
        seen.add(tuple(parent))
        try:
            tables = native.call(ns, "list_tables", ListTablesRequest(id=parent, include_declared=True)).tables or []
            found.extend(fga_ids.canonical_object_id([*parent, name]) for name in tables)
            children = native.call(ns, "list_namespaces", ListNamespacesRequest(id=parent)).namespaces or []
        except Exception:  # noqa: BLE001 — one bad namespace, not a blank registry
            log.warning("could not list %s", parent, exc_info=True)
            continue
        stack.extend([*parent, child] for child in children)
    return found


async def _tables_of_every_warehouse(request: Request, settings: Settings) -> list[str]:
    """Every table of every warehouse the catalog serves, fully qualified (`acme$bronze$pages`), unordered.

    ONE WALK PER WAREHOUSE, each through that warehouse's own directory catalog ([[LH-593]]): a collection route has no
    `{id}` to route by, and a table's name begins with its warehouse. Concurrent, since each warehouse is an independent
    round trip, and tolerant: a warehouse that cannot be listed (deactivated, an unreadable bucket) is logged and left
    out, never a blank registry.
    """
    warehouse_ids = await run_in_threadpool(
        partial(warehouses.served_ids, settings.registry_root, settings.storage_options(), estate=settings.estate_warehouse)
    )

    async def _tables_in(warehouse_id: str) -> list[str]:
        return await run_in_threadpool(_collect_tables, await namespace_for_warehouse(request, settings, warehouse_id), warehouse_id)

    names: list[str] = []
    for warehouse_id, listed in zip(warehouse_ids, await asyncio.gather(*(_tables_in(w) for w in warehouse_ids), return_exceptions=True), strict=True):
        if isinstance(listed, BaseException):
            log.warning("could not list warehouse %s", warehouse_id, exc_info=listed)
        else:
            names.extend(listed)
    return names


@spec_router.get("", response_model_exclude_none=True)
async def list_all_tables(
    *,
    request: Request,
    context: AuthzContextDep,
    so: StorageOptionsDep,
    resolver: ResolverDep,
    limit: ListLimit,
    page_token: str | None = None,
    include_declared: bool = True,
) -> ListTablesResponse:
    """List every table of every warehouse; when FGA is on, only those the caller may see (``can_get_metadata``).

    ``include_declared=false`` lists only the tables that hold a committed version, each judged by its own storage
    (:func:`catalog.api.listings.holding_a_version`, [[LH-528]]).

    Pagination is applied to the merged, sorted result, never to a native call, whose ``limit`` would truncate one
    warehouse's walk and drop tables from every page. The cursor is keyset-style: ``page_token`` is the last name of
    the page, stateless and stable across pages. The walk costs the full estate server-side per call, acceptable at
    admin frequency.
    """
    response = ListTablesResponse(tables=[])
    candidates = [(name, parse_identifier(name)) for name in sorted(await _tables_of_every_warehouse(request, context.settings))]
    if not include_declared:
        candidates = await listings.holding_a_version(resolver, so, candidates)
    # With FGA on, return only the tables the caller may see (`can_get_metadata`, the describer's rung, [[LH-570]]),
    # each judged on the object its name resolves to; pages count only those.
    if (authz := decisions.authorization(context)) is not None:
        response.tables, response.page_token = await visible_pages.authorized_page(
            authz,
            resolver,
            relation="can_get_metadata",
            kind="table",
            candidates=candidates,
            page_token=page_token,
            limit=limit,
        )
        return response
    response.tables, response.page_token = paginate([name for name, _ in candidates], page_token, limit)
    return response


def _vendable_bases(location: str, settings: Settings, so: StorageOptions) -> VendableBases | None:
    """The bases a credential for the table at ``location`` grants, or ``None`` where only the catalog may read it.

    ``None`` is the management vend door's ``server_mediated``, off the same read ([[LH-057]]):

    * a classified column cannot be excluded from an object-store grant, because Lance's field-to-file mapping
      is write-order dependent ([[LH-058]], `credentials.py`);
    * a sanctioned base the session policy cannot address is reachable only with the catalog's own credential.

    Raises:
        InvalidTableStateError: A base the catalog did not sanction, refused rather than read through ([[LH-279]]).
    """
    facts = dataset_facts(location, so)
    bases = require_vendable_bases(location, facts, declared_base_judge(settings))
    if facts.classified or unsanctioned_bases(location, bases.bases, settings.multibase_data_base_list):
        return None
    return bases


def _mint(location: str, *, tier: Tier, bases: VendableBases, vendor: CredentialVendor, session_name: str) -> dict[str, str] | None:
    """The ``storage_options`` the vendor mints for ``location`` at ``tier``, with the expiry the spec reads.

    THE EXPIRY GOES INSIDE ``storage_options``, where the spec puts it and the only place a stock client looks
    (``lance_docs/ns_catalog/spec.yaml`` § DescribeTableResponse: "If the vended credentials are temporary, the
    ``expires_at_millis`` key ..."). Without it a temporary credential looks permanent, and the client discovers
    the TTL by failing at the 900-second mark instead of refreshing.
    """
    vended = vend_or_refuse(vendor, table_location=location, tier=tier, session_name=session_name, bases=bases.bases, data_directories=bases.data_directories)
    if vended is None:
        return None
    # The secret and the session token leave the catalog in the door's answer and nowhere else ([[LH-072]]).
    return {**vended.as_storage_options().in_clear(), "expires_at_millis": str(vended.expires_at_millis)}


async def _vend_on_spec_door(
    ns: LanceNamespace,
    *,
    door: str,
    segments: list[str],
    table_uuid: str,
    location: str,
    context: AuthzContext,
    so: StorageOptions,
    vendor: CredentialVendor,
    asked: bool,
) -> dict[str, str] | None:
    """Vend the highest tier FGA grants the caller on ``segments`` (ADR 0156 C3), and record the issuance (#41).

    ``table_uuid`` and ``location`` are the table's as the door read them together, so the tier is decided on the
    table the credential reaches.

    A stock client opening a table through the namespace signs with what the spec door returns, and one that
    gets nothing signs with whatever its environment holds; vending here is what keeps it off an ambient key.

    The order is the cost and the record: whether the table is directly readable at all is settled first (a base
    nothing sanctioned is refused there, whatever the vendor), so a server-mediated table, or a deployment that
    vends nothing, asks FGA nothing. The tier is
    :func:`write_doors.spec_vend_tier`'s. Only then is a credential minted, and only a mint writes the
    ``vend_credentials`` issuance record.

    ``asked`` is ``vend_credentials`` set true. Left unset, the credential is the server's offer: a refusal or a
    failed exchange leaves the answer without one and is logged, because a metadata read, the catalog's most
    called door, must not fail on the object store's STS. Asked for, the refusal is the answer.

    Raises:
        PermissionDeniedError: The caller asked and FGA grants it no rung that opens a tier.
        LanceNamespaceError: The caller asked and the vend was refused or failed (409, 401, 503).
    """
    try:
        bases = await run_in_threadpool(_vendable_bases, location, context.settings, so)
        if bases is None or isinstance(vendor, ModeBVendor):
            return None
        tier = await write_doors.spec_vend_tier(context, ns=ns, segments=segments, table_uuid=table_uuid, door=door)
        if tier is None:
            if asked:
                raise PermissionDeniedError(f"can_read_data required on table:{fga_ids.canonical_object_id(segments)} to vend credentials")
            return None
        subject = context.token.subject if context.token is not None else ANONYMOUS_SUBJECT
        options = await run_in_threadpool(partial(_mint, location, tier=tier, bases=bases, vendor=vendor, session_name=role_session_name(subject)))
    except LanceNamespaceError:
        if asked:
            raise
        log.warning("spec_vend_skipped", extra={"door": door, "table": "/".join(segments)}, exc_info=True)
        return None
    if options is not None:
        resource = fga_ids.table_object(table_uuid)
        audit(
            AuditAction.VEND_CREDENTIALS,
            AuditOutcome.SUCCESS,
            subject=context.token.subject if context.token is not None else None,
            resource=resource,
            tier=tier,
            door=door,
        )
    return options


# `paginate` lives in `catalog.core.pagination`, shared by every catalog listing: two keyset implementations in one
# service is two things to keep in step.
@spec_router.post("/{id}/declare", response_model_exclude_none=True)
async def declare_table(
    *,
    id: str,
    keyed: idem.KeyedDoorDep,
    ns: NamespaceDep,
    root: NamespaceRootDep,
    context: AuthzContextDep,
    so: StorageOptionsDep,
    vendor: VendorDep,
    uuids: ObjectUuidsDep,
    announcer: AnnouncerDep,
    resolver: ResolverDep,
    body: DeclareTableRequest | None = None,
) -> DeclareTableResponse:
    """Declare a new (empty) table at ``id``, at the namespace root under the uuid the catalog mints for it
    (``tables.creation.declare_table``, [[LH-520]]), then seed the caller's FGA ownership and emit a versionless
    DECLARE_TABLE marker (the table's first provenance — who reserved it + where).

    The catalog chooses a declared table's location, which the spec leaves to the implementation
    (`lance_docs/ns_catalog/spec.yaml:3831-3835`), so a request naming one is refused. The declaration keeps no
    ``properties``: it is recorded through RegisterTable, which keeps none (`spec.yaml:2719-2724`).

    The response carries the credential the declarer writes the table with, the highest tier FGA grants
    (:func:`_vend_on_spec_door`), unless the request sets ``vend_credentials`` false.
    """
    _refuse_an_unservable_declaration(body)
    segments = parse_identifier(id)
    # As the table doors (`catalog.api.idempotency`): a replay under the caller's key answers the first attempt and re-enters nothing.
    converge = await keyed.begin(target=fga_ids.canonical_object_id(segments), takeover="rerun")
    if converge.replay is not None:
        return DeclareTableResponse.model_validate(converge.replay.body)
    await hierarchy.require_parent_exists(ns, "table", segments)
    req = body or DeclareTableRequest()
    req.id = reconcile_body_id(segments, req.id)
    canonical = fga_ids.canonical_object_id(segments)
    claims = table_claims.claim_store(context.settings)
    # The table's uuid is minted with its location's claim, before anything grants or announces it (ADR 0173 §2, §5),
    # and its grants are written once the claim records it, before the backend records the table ([[LH-588]]).
    table_uuid = str(uuid4())
    grants = object_grants.GrantsBeforeWrite(context, resolver=resolver, segments=segments)
    declare = partial(
        creation.declare_table,
        ns,
        so,
        segments,
        root=root,
        claims=claims,
        holder=canonical,
        table_uuid=table_uuid,
        before_register=grants,
        after_register=partial(grants.parent_as_authorized, ns),
    )
    location = await _declare_or_take_back_the_grants(ns, declare=declare, grants=grants)

    async def _undo_declare() -> None:
        await run_in_threadpool(native.call, ns, "drop_table", DropTableRequest(id=segments))
        await run_in_threadpool(location_claims.release, claims, location, canonical)

    async def _take_back_declare() -> None:
        await object_grants.undo_then_take_back(context, segments=segments, uuid=table_uuid, undo=_undo_declare, reason="not_announced")

    # What the declaration announces is filed once the location the backend describes it at is known, before its
    # ownership is seeded, so a door failing after that still announces it, and a store refusing the filing takes the
    # declaration back ([[LH-576]]). It landed while the name resolves to the table.
    named = NameResolves(segments=segments, uuid=table_uuid)
    # Versionless (no data yet): records who declared it + the reserved location, and keys the CREATED edge
    # (declare_table ∈ lineage _CREATE_OPS). Reconcile fills the real version once data lands at the URI.
    declared = _marker(
        announcer,
        context.token,
        named,
        segments=segments,
        operation=DECLARE_TABLE,
        uuids=await uuids.table(segments, location=location, uuid=table_uuid),
        source_uri=location,
    )
    declared_event = _table_event(context.token, named, action="table_declared", table_uuid=table_uuid, name=canonical, extra={"location": location})
    async with _filed_or_undone(
        announcer, door="declare_table", target=fga_ids.table_object(table_uuid), announcements=[declared, declared_event], undo=_take_back_declare
    ) as filing:
        response = await _vended_declaration(
            ns,
            so,
            segments,
            context=context,
            req=req,
            location=location,
            table_uuid=table_uuid,
            undo=_undo_declare,
            vendor=vendor,
        )
        await filing.send(declared)
        await filing.send(declared_event)
    # Remembered without the credential the declaration vended: a record on the control root holds no secret, and a
    # replay's caller asks DescribeTable for a credential of its own, as any reader of the table does.
    await converge.remember(200, response.model_copy(update={"storage_options": None}))
    return response


def _refuse_an_unservable_declaration(body: DeclareTableRequest | None) -> None:
    """Refuse a declaration this door does not serve, from the request alone."""
    # LANCE-ONLY (2026-08-15 ruling): a format-selecting property answers the format's own refusal first, a platform key
    # (the table's uuid is the catalog's to mint, [[LH-519]]) the provenance refusal, and every other property is refused
    # as one this door keeps nowhere, before anything is reserved.
    reject_unsupported_properties(body.properties if body else None)
    provenance_guard.refuse_reserved_keys((body.properties if body else None) or {}, door="declare properties")
    refuse_properties_kept_nowhere(body.properties if body else None, door="declare")
    if body is not None and body.location:
        raise InvalidInputError(
            f"a declaration names no location: the catalog places a table it declares under the uuid it mints for it, got {body.location!r}"
        )


async def _declare_or_take_back_the_grants(ns: LanceNamespace, *, declare: Callable[[], str], grants: object_grants.GrantsBeforeWrite) -> str:
    """Run ``declare``, which writes ``grants`` first, taking them back when the declaration fails."""
    try:
        location = await run_in_threadpool(declare)
    except Exception:
        await grants.take_back_unless_standing(ns)
        raise
    return location


async def _vended_declaration(
    ns: LanceNamespace,
    so: StorageOptions,
    segments: list[str],
    *,
    req: DeclareTableRequest,
    location: str,
    table_uuid: str,
    undo: Callable[[], Awaitable[None]],
    context: AuthzContext,
    vendor: CredentialVendor,
) -> DeclareTableResponse:
    """The declaration of ``segments`` at ``location`` as its declarer is answered, owned by them since before it was
    recorded ([[LH-588]]), with the credential they write it with, ``undo`` taking the declaration and its grants back
    when the vend fails (:func:`object_grants.undo_then_take_back`: the table first, its grants once it is gone)."""
    response = DeclareTableResponse(location=location)
    # The credential is part of the answer, so a vend that fails takes the declaration back with it, before
    # anything announces the table: a declared, owned table whose declarer was told it failed is one a retry
    # can only meet as AlreadyExists.
    if req.vend_credentials is not False and response.location:
        try:
            response.storage_options = await _vend_on_spec_door(
                ns,
                context=context,
                door="declare",
                segments=segments,
                table_uuid=table_uuid,
                location=response.location,
                so=so,
                vendor=vendor,
                asked=req.vend_credentials is True,
            )
        except Exception:
            await object_grants.undo_then_take_back(context, segments=segments, uuid=table_uuid, undo=undo, reason="vend_failed")
            raise
    response.managed_versioning = True
    return response


def _marker(
    announcer: Announcer,
    token: Caller | None,
    evidence: Evidence,
    *,
    segments: list[str],
    operation: str,
    uuids: DatasetUuids,
    source_uri: str | None = None,
    renamed_from: list[str] | None = None,
) -> Announcement:
    """A table's lifecycle marker as its door files it: authored as the verified caller, landed when ``evidence`` shows."""
    author = token.subject if token is not None else None
    marker = announcer.marker(segments, author=author, operation=operation, uuids=uuids, source_uri=source_uri, renamed_from=renamed_from)
    return Announcement(evidence=evidence, message=marker)


def _table_event(
    token: Caller | None,
    evidence: Evidence,
    *,
    action: ControlAction,
    table_uuid: str,
    name: str,
    extra: dict[str, Any],
    named_by: DatasetUuids | None = None,
) -> Announcement:
    """A table's control event as its door files it: by the verified caller, naming the table by its uuid object and its
    name, and by the uuids ``named_by`` holds where the door read them before its act ended or moved the table (ADR 0173
    §10)."""
    event = CatalogControlEvent(
        action=action,
        object_type="table",
        object_id=fga_ids.table_object(table_uuid),
        object_name=name,
        object_uuid=named_by.table_uuid if named_by is not None else None,
        namespace_uuid=named_by.namespace_uuid if named_by is not None else None,
        actor=f"user:{token.subject}" if token is not None else None,
        extra=extra,
    )
    return Announcement(evidence=evidence, message=ControlMessage(event=event))


@asynccontextmanager
async def _filed_or_undone(
    announcer: Announcer, *, door: str, target: str, announcements: list[Announcement], undo: Callable[[], Awaitable[None]]
) -> AsyncGenerator[Filing]:
    """File ``announcements`` for a door whose backend call already landed; a store refusing the filing takes that call
    back with ``undo`` before the refusal is answered. An undo that fails is logged, and the refusal is answered as it
    was."""
    async with AsyncExitStack() as stack:
        try:
            filing = await stack.enter_async_context(announcer.announcing(door=door, target=target, announcements=announcements))
        except Exception:
            try:
                await undo()
            except Exception as undo_exc:  # noqa: BLE001 — the refusal is the answer; what the undo left is logged for the operator
                log.error("unfiled_change_not_undone", extra={"door": door, "target": target, "error": f"{type(undo_exc).__name__}: {undo_exc}"[:300]})
            raise
        yield filing


def _refuse_a_branch_describe_cannot_honour(branch: str | None, *, named_by: str | None = None) -> None:
    """Refuse a branch this door cannot describe — with the code its fifteen siblings use.

    THIS DOOR DESCRIBES MAIN. `describe_table` answers off the namespace manifest, which has no branch
    selector, and the branch-aware read lives in the dataplane (`DoorTable.open(branch=...)`). Refusing is
    the point: SILENTLY describing main for a caller who pinned a branch is the same class of
    wrong-but-plausible answer as the ignored `version` this door used to give.

    IT DELEGATES RATHER THAN RAISING ITS OWN, and that is the substance rather than tidiness. Refusing
    inline here answered `InvalidInputError` (spec code 13) while the fifteen doors on
    `refuse_a_branch_this_door_cannot_honour` answered `UnsupportedOperationError` (code 0) for the
    identical condition — and the spec settles which is right: 13 is "Malformed request or invalid
    parameters" (`lance_docs/ns_catalog/spec.yaml:2425`), 0 is "Operation not supported by this backend"
    (`:2412`). A well-formed branch name this backend does not serve is the second. Clients dispatch on
    those codes, so two answers for one condition is a contract defect, not a wording one.

    ONE FUNCTION FOR EVERY CHANNEL. `branch` reaches this door through the body AND the query string,
    and the query channel was added later precisely because a refusal covering one of two is why this
    door read as settled while `?branch=work` still answered 200 for main (measured 2026-08-31). A `tag`
    names a branch too, the one its version lives on, and ``named_by`` says so: the response has no field
    to name a branch, so a branch tag described at its number reads as main's version to its caller.

    `main` is read as no branch by `refuse_a_branch_this_door_cannot_honour` itself, as at every door it guards.
    """
    remedy = f"Use the branch operations under /v1/table/{{id}}/branches, or read the branch through `count_rows`. (got {branch!r})"
    reads.refuse_a_branch_this_door_cannot_honour(branch, door="describe", remedy=f"{named_by}. {remedy}" if named_by else remedy)


@spec_router.post("/{id}/describe", response_model_exclude_none=True)
async def describe_table(
    *,
    id: str,
    table: DoorTableDep,
    context: AuthzContextDep,
    so: StorageOptionsDep,
    vendor: VendorDep,
    body: DescribeTableRequest | None = None,
    with_table_uri: bool | None = None,
    load_detailed_metadata: bool | None = None,
    check_declared: bool | None = None,
    version: int | None = None,
    tag: str | None = None,
    branch: Annotated[str | None, Query(description="The ref to describe. Omit for main.")] = None,
    vend_credentials: bool | None = None,
) -> DescribeTableResponse:
    """Describe the table at ``id`` (schema / uri / detailed metadata) via ``describe_table``, optionally
    pinned to a ``version`` or a ``tag`` (spec 0.9: mutually exclusive).

    TWO DOORS, ONE MEANING. The Lance Namespace spec puts ``with_table_uri`` / ``load_detailed_metadata`` /
    ``check_declared`` in the QUERY and ``version`` / ``tag`` / ``vend_credentials`` in a
    ``DescribeTableRequest`` BODY (``lance_docs/ns_catalog/catalog/rest/index.md`` §DescribeTable; the generated client's
    ``_describe_table_serialize`` sends exactly that split). This route once bound all six as bare scalars,
    so FastAPI made them all query params and the body was dropped unread — a spec-conformant client
    (a stock client calling ``describe_table(DescribeTableRequest(id=...))`` and merging
    ``storage_options``) got a location and NO credentials, and any version pin silently read latest.
    Both forms are now accepted: **a field present in the body wins**, because the body is the spec's form
    and the query is rask's own older shorthand that existing callers and its tests speak. Presence is decided by
    the JSON keys actually sent, not by the model's defaults. The body is OPTIONAL here (the spec marks it
    required) so those existing query-only callers keep working — a superset, not a deviation in meaning.

    ``tag`` is resolved to its version HERE via the dataplane's tag store, from either door: the native
    backend at pylance 8.0.0 silently IGNORES a describe-request ``tag`` (probed 2026-07-10 — a nonexistent
    tag described the LATEST version with no error), so forwarding it would lie; the catalog resolves
    (404 on an unknown tag, like ``/tags/version``) and describes at the resolved version instead. A tag
    on a branch is refused as ``branch`` is: its version lives in that branch's history.

    ``branch``, in the body or the query, is REFUSED unless it names main — see the site below. ``identity`` and ``context``
    are ignored: the caller is the verified bearer token, not a field of the request body.

    CREDENTIALS COME WITH THE ANSWER unless the caller sets ``vend_credentials`` false: unset, the spec lets
    the server decide (``lance_docs/ns_catalog/spec.yaml`` § DescribeTableRequest), and a stock client opening
    through the namespace that gets none signs with whatever its environment holds. The tier is the highest FGA
    grants the caller (:func:`_vend_on_spec_door`, ADR 0156 C3): the router admitted a metadata reader, and a
    write credential is decided by the write judge like every write.
    """
    segments = parse_identifier(id)
    if body is not None:
        # PRESENCE, not value, decides. `DescribeTableRequest` defaults `with_table_uri`/`check_declared`
        # to False, so merging on the parsed model would let a body carrying only `{"version": 1}` reset a
        # caller's `?with_table_uri=true` to False. `model_fields_set` is exactly the set of keys the JSON
        # actually carried, which is the only thing that can tell "sent false" from "not sent".
        sent = body.model_fields_set
        segments = reconcile_body_id(segments, body.id)
        _refuse_a_branch_describe_cannot_honour(body.branch)
        # `identity` and `context` are deliberately NOT honoured: the caller is the bearer token the router
        # already verified, and letting a request body name a principal would be an impersonation door.
        if "with_table_uri" in sent:
            with_table_uri = body.with_table_uri
        if "load_detailed_metadata" in sent:
            load_detailed_metadata = body.load_detailed_metadata
        if "check_declared" in sent:
            check_declared = body.check_declared
        if "version" in sent:
            version = body.version
        if "tag" in sent:
            tag = body.tag
        if "vend_credentials" in sent:
            vend_credentials = body.vend_credentials
    # THE QUERY CHANNEL, which the body refusal below did not cover. `branch` was body-only, so a caller
    # who wrote `?branch=work` reached a route that does not declare it: FastAPI dropped it and the door
    # answered for MAIN with a 200 — including for a branch that had never been created (verified live
    # 2026-08-31). A refusal that covers one of two channels is why this door read as settled.
    _refuse_a_branch_describe_cannot_honour(branch)
    if tag is not None and version is not None:
        raise InvalidInputError("`tag` cannot be used together with `version` (spec 0.9)")
    response = await run_in_threadpool(
        _describe,
        table,
        so,
        with_table_uri=with_table_uri,
        load_detailed_metadata=load_detailed_metadata,
        check_declared=check_declared,
        version=version,
        tag=tag,
    )
    # ADR 0157: every commit comes back through CreateTableVersion, where the catalog judges it.
    response.managed_versioning = True
    if vend_credentials is not False and response.location:
        table_uuid = uuid_of(response.properties)
        if table_uuid is None:
            raise InternalError(f"table {segments} answers no uuid, so no credential can be vended for it")
        response.storage_options = await _vend_on_spec_door(
            table.namespace,
            context=context,
            door="describe",
            segments=segments,
            table_uuid=table_uuid,
            location=response.location,
            so=so,
            vendor=vendor,
            asked=vend_credentials is True,
        )
    return response


def _describe(
    table: DoorTable,
    so: StorageOptions,
    *,
    with_table_uri: bool | None,
    load_detailed_metadata: bool | None,
    check_declared: bool | None,
    version: int | None,
    tag: str | None,
) -> DescribeTableResponse:
    """The blocking half of :func:`describe_table`: resolve the pin, describe, judge and load the table, and answer its uuid.

    The uuid is the one the table's location's claim records ([[LH-519]], ADR 0173 §6, §11), answered under the
    ``rask.uuid`` property, which no door lets a caller set. It is read, never minted here, so every describe of
    the table answers the same one.

    THE DESCRIBE IS THE ACT, so it answers for the name's resolution ([[LH-530]]): every open it makes is at the door's
    location, and the answer stands only when its own describe found the name there, under the claim the resolution
    read (``DoorTable.require_described_at``); a failure is told the change first, as every door's open is.

    WHETHER THE TABLE IS ONLY DECLARED IS THE CATALOG'S JUDGMENT ([[LH-528]]), and so is the detailed metadata that rides
    on it. The backend is asked only for the plain describe: its own verdict misjudges a table registered at a nested
    location, and with ``load_detailed_metadata`` it then answers no version and no schema
    (``table_objects.holds_a_version``). ``is_only_declared`` is answered when ``check_declared`` or ``load_detailed_metadata``
    asks for it, as the spec says (`lance_docs/ns_catalog/spec.yaml:2926-2934`).
    """
    segments = table.segments
    try:
        if tag is not None:
            ref = refs.tag_reference(table.open_unchecked(so), tag)
            _refuse_a_branch_describe_cannot_honour(ref.branch, named_by=str(ref))
            version = ref.version
        # A VERSION THAT DOES NOT EXIST MUST NOT DESCRIBE THE ONE THAT DOES. `describe_table` answers off
        # the namespace manifest and does not resolve the pin, so `?version=9999` on a two-version table
        # returned 200 with byte-identical metadata (verified live 2026-08-31) — a caller probing whether
        # a version exists is told yes about every number they try. Opening the pinned dataset is what
        # mints the spec's own error 11 `TableVersionNotFound`; the open runs ONLY when a version was
        # named, so the unpinned describe every client makes is untouched.
        pinned = open_resolved_dataset_unchecked(table.location, so, segments, version=version) if version is not None else None
        response: DescribeTableResponse = native.call(
            table.namespace, "describe_table", DescribeTableRequest(id=segments, with_table_uri=with_table_uri, version=version)
        )
        if response.location and load_detailed_metadata:
            _load_or_judge_declared(response, table, so, pinned=pinned)
        elif response.location and check_declared:
            response.is_only_declared = not table_objects.holds_a_version(response.location, so)
    except Exception:
        table.require_named()
        raise
    table.require_described_at(response.location)
    if (uuid := table.resolved.uuid) is not None:
        response.properties = {**(response.properties or {}), UUID_PROPERTY: uuid}
    return response


def _load_or_judge_declared(response: DescribeTableResponse, table: DoorTable, so: StorageOptions, *, pinned: lance.LanceDataset | None) -> None:
    """Load the table's detailed metadata onto ``response`` from one open, and judge whether it is only declared by it.

    Lance finds a table's latest version by listing its ``_versions/`` on every open, and the store answers that listing
    with every manifest key, so the judgment rides the open the metadata needs rather than a listing of its own
    ([[LH-529]]). Only an open that finds no dataset asks the storage itself (``table_objects.holds_a_version``), which tells
    a table holding no committed version, only declared, from one whose committed version could not be opened.

    Raises:
        TableNotFoundError: The table holds a committed version that does not open.
    """
    if pinned is not None:
        dataset = pinned
    else:
        try:
            dataset = open_resolved_dataset_unchecked(table.location, so, table.segments)
        except TableNotFoundError:
            if table_objects.holds_a_version(str(response.location), so):
                raise
            response.is_only_declared = True
            # The only thing a declared table's detail holds: the format its first `version/create` must carry.
            response.metadata = CREATE_FORMAT.as_metadata()
            return
    response.is_only_declared = False
    _load_detailed_metadata(response, dataset)


def _load_detailed_metadata(response: DescribeTableResponse, dataset: lance.LanceDataset) -> None:
    """Answer ``dataset``'s version, schema and stats, and its own schema metadata.

    ``load_detailed_metadata`` "must include all detailed metadata such as `version`, `schema`, and `stats`"
    (`lance_docs/ns_catalog/spec.yaml` DescribeTableRequest); the stats are ``TableBasicStats``, which Lance's
    ``dataset_stats`` reads off the manifest the handle already holds. ``metadata`` is the table's user metadata, read
    from the same handle, so a table property a caller writes through ``schema_metadata/update`` is read back here, and
    the table's file format (`lakehouse.lancekit.features.DATA_STORAGE_VERSION_KEY`, `STABLE_ROW_IDS_KEY`): ``metadata`` is the
    spec's map for "the information of the table [that] requires loading the table", where ``properties`` is what the
    namespace manages.
    """
    stats = dataset.stats.dataset_stats()
    loaded = DescribeTableResponse.model_validate(
        {
            "version": dataset.version,
            "schema": json_arrow.table_schema_json(dataset.lance_schema),
            "stats": {"num_deleted_rows": int(stats["num_deleted_rows"]), "num_fragments": int(stats["num_fragments"])},
        }
    )
    response.version = loaded.version
    response.var_schema = loaded.var_schema
    response.stats = loaded.stats
    # The table's own schema metadata, then its file format under the platform's reserved keys, which no caller writes
    # ([[LH-260]]): a client staging files for `version/create` must write them at this version.
    response.metadata = {**schema.user_schema_metadata(dataset.schema), **table_file_format(dataset).as_metadata()}


#: The spec gives `exists` a 200 with NO `content:` block — *"Success, no content"* — so a conforming
#: server sends an empty body. A handler annotated `-> None` sends `application/json` with the four
#: bytes `null`, which a strict client reads as a JSON document where the contract promised none.
#: `response_class=Response` is what makes FastAPI emit nothing; `status_code=200` alone does not.
@spec_router.post("/{id}/exists", status_code=200, response_class=Response)
def table_exists(*, id: str, table: DoorTableDep, so: StorageOptionsDep, body: TableExistsRequest | None = None) -> Response:
    """Check the table at ``id`` exists via ``table_exists`` — 200 if present (spec 0.9), else error.

    The answer is about the table the request resolved, so it stands only while the name still resolves there ([[LH-530]]).
    A ``version`` asks whether the table holds that version (`lance_docs/ns_catalog/spec.yaml` TableExistsRequest), which
    the backend's own answer ignores, so the version is opened: one the table lacks answers 404 code 11, as describe does.
    """
    segments = reconcile_body_id(parse_identifier(id), body.id if body else None)
    version = body.version if body else None
    table.read_natively("table_exists", TableExistsRequest(id=segments, version=version))
    if version is not None:
        table.open_unchecked(so, version=version)
    return Response(status_code=200)


@spec_router.post("/{id}/drop", response_model_exclude_none=True)
async def drop_table(
    *,
    id: str,
    keyed: idem.KeyedDoorDep,
    ns: NamespaceDep,
    table: ResolvedTableDep,
    context: AuthzContextDep,
    uuids: ObjectUuidsDep,
    announcer: AnnouncerDep,
    so: StorageOptionsDep,
    force: RaskFlag = False,
    purge: RaskFlag = False,
) -> DropTableResponse:
    """Drop the table at ``id`` via ``drop_table``, then revoke its FGA tuples and
    emit a best-effort ``drop_table`` lineage event.

    Deletion protection (#73, the warehouse door's Decision-5 contract extended to the rung where a
    drop deletes BYTES): a ``protected`` control-root record refuses 409 unless ``force=true``, and
    ``force`` turns the protection lock ONLY — the FGA gate ran before this handler, identically
    with or without it."""
    # SAME AMPLIFIER AS create (see `catalog.api.idempotency`): a replay re-enters a door that
    # DELETES BYTES or files a trash record. OPTIONAL, because the spec defines no such header and a stock client must keep working.
    segments = parse_identifier(id)
    canonical = fga_ids.canonical_object_id(segments)
    converge = await keyed.begin(target=canonical, captured=idem.capture(canonical, _resolved_object(table)), takeover=announcer.landed)
    if converge.replay is not None:
        return DropTableResponse.model_validate(converge.replay.body)

    await hierarchy.require_table_unprotected(context.settings, table, force=force)
    table_uuid = table.require_uuid()
    # What the drop's announcements name the table by, read while it still stands (ADR 0173 §10).
    known = await uuids.table(segments, uuid=table.uuid)
    # #75 the drop→undrop path. With a grace period configured, a drop DEREGISTERS (detaches the
    # pointer; the bytes stay exactly where they are) and files a trash record naming the location and
    # the deadline. This is what makes a fat-fingered drop survivable: time-travel cannot help here,
    # because `restore_table` rewinds a LIVE table and a real drop leaves no version to rewind to.
    # `purge=true` is the explicit opt-out — a caller who means "destroy the bytes now" says so. The grace period is the
    # table's warehouse's delete profile, else the estate's (ADR 0146).
    grace = 0.0 if purge else await trash_doors.drop_grace_days(context.settings, segments)
    trashed = grace > 0
    # What the drop announces is filed before anything changes, so a door failing after the drop still announces it
    # ([[LH-576]]). A recoverable drop mints its own id, files its trash record under it and stamps that record once its
    # detach returned: it landed when this drop's record is stamped, or shows it detached the table (`TrashedUnder`), and
    # its keyed resend is answered from the same proof. A destroying drop landed when the table is registered under no
    # name and no recoverable drop's record keeps it (`Unregistered`): a rename since a drop that never reached it keeps it
    # registered. It leaves nothing of the attempt and intends nothing, so its takeover runs again: the name holds nothing
    # and the run refuses 404, or it still names this table, which the first never dropped. Its limit:
    # the store cannot tell which actor retired a table, so another's deregister or destroying drop after this door died
    # satisfies it too, and this door's event goes out as well, under its own ids.
    drop = uuid4().hex if trashed else None
    landed: Evidence = (
        TrashedUnder(uuid=table_uuid, attempt=drop)
        if drop is not None
        else Unregistered(segments=segments, uuid=table_uuid, location=table.location or "", trashed=False)
    )
    dropped = _marker(announcer, context.token, landed, segments=segments, operation=DROP_TABLE, uuids=known)
    dropped_event = _table_event(
        context.token, landed, action="table_dropped", table_uuid=table_uuid, name=canonical, extra={"recoverable": trashed}, named_by=known
    )
    if drop is not None:
        await converge.intend([landed], DropTableResponse(transaction_id=fga_ids.canonical_object_id([*segments, table_uuid])))
    async with announcer.announcing(door="drop_table", target=fga_ids.table_object(table_uuid), announcements=[dropped, dropped_event]) as filing:
        response = await retirement.drop_resolved(
            ns,
            context.settings,
            table,
            segments=segments,
            table_uuid=table_uuid,
            known=known,
            grace=grace,
            dropped_by=f"user:{context.token.subject}" if context.token is not None else None,
            so=so,
            drop=drop,
        )
        # Record the drop as lineage — provenance of the deletion (the dataset node persists in the graph, named a
        # `drop_table` run). Sent BEFORE the revoke, so a revoke that fails ends the request with the marker already
        # published. Lineage admits a drop this catalog signed on that signature, never on the grants the revoke takes.
        await filing.send(dropped)
        # Revoke the table's FGA tuples so a later table reusing this id can't inherit stale grants — but
        # ONLY on a destructive drop. A RECOVERABLE drop (#75) leaves an object that still exists and whose
        # owner is the one person who needs to undrop it; revoking here made undrop unreachable for exactly
        # that caller (found by driving the deployed catalog, not by a unit test — the unit tests run FGA
        # off). The grants die with the bytes instead: at purge, or when the sweep's expiry reclaims it.
        if not trashed:
            await object_grants.revoke_ownership(context, obj=fga_ids.table_object(table_uuid), origin="lifecycle_delete")
        await filing.send(dropped_event)
    await converge.remember(200, response)
    return response


def _resolved_object(table: ResolvedTable) -> str | None:
    """The authorization object ``table``'s name resolved to, ``table:<uuid>``; ``None`` when it names no identified table."""
    return fga_ids.table_object(table.uuid) if table.location is not None and table.uuid is not None else None


@spec_router.post("/{id}/deregister", response_model_exclude_none=True)
async def deregister_table(
    *,
    id: str,
    keyed: idem.KeyedDoorDep,
    ns: NamespaceDep,
    table: ResolvedTableDep,
    context: AuthzContextDep,
    uuids: ObjectUuidsDep,
    control: ControlEmitterDep,
    announcer: AnnouncerDep,
    body: DeregisterTableRequest | None = None,
    force: RaskFlag = False,
) -> DeregisterTableResponse:
    """Deregister the table at ``id`` (detach it without deleting data) via lance_namespace
    ``deregister_table``, then revoke its FGA ownership and emit a best-effort ``deregister_table`` marker.

    Protection-gated like drop (#73): deregister keeps bytes but REMOVES the object from governance —
    the flag's whole jurisdiction — so leaving it ungated would make "deregister, then delete the
    files by hand" the unprotected path around the protected drop.

    A table no uuid identifies is the estate admin's to deregister (the router asks that rung for it, [[LH-534]]): it
    holds no grant, no record and no protection, since each is kept under a uuid, so its row is detached and its
    location's claim released, and the detach is announced on the control plane alone. Lineage refuses an event naming
    a table by no uuid ([[LH-166]]), so the feed is not told, and no filing precedes that detach: its announcement names
    no uuid the finish could judge a name by. Every other deregister's announcements are filed before the detach, so a
    door failing after it still announces it ([[LH-576]])."""
    # SAME AMPLIFIER AS create (see `catalog.api.idempotency`): a replay re-enters a door that
    # retires the id while the bytes stay. OPTIONAL, because the spec defines no such header and a stock client must keep working.
    # A takeover runs again: a detach that landed leaves the name holding nothing, and the run refuses 404.
    named = fga_ids.canonical_object_id(parse_identifier(id))
    converge = await keyed.begin(target=named, captured=idem.capture(named, _resolved_object(table)), takeover="rerun")
    if converge.replay is not None:
        return DeregisterTableResponse.model_validate(converge.replay.body)

    segments = reconcile_body_id(parse_identifier(id), body.id if body else None)
    canonical = fga_ids.canonical_object_id(segments)
    if table.location is not None and table.uuid is None:
        response = await _deregister_unidentified(ns, context.settings, table, control=control, token=context.token, segments=segments)
        await converge.remember(200, response)
        return response
    await hierarchy.require_table_unprotected(context.settings, table, force=force)
    table_uuid = table.require_uuid()
    known = await uuids.table(segments, uuid=table.uuid)
    # It landed once the table is registered under no name and no record filed since keeps it (`Unregistered`). Its limit:
    # the store cannot tell which actor retired a table, so another's deregister or destroying drop after this door died
    # satisfies it too, and this door's event goes out as well, under its own ids: a dead deregister followed by another's
    # destroying drop is told as a deregister.
    gone = Unregistered(segments=segments, uuid=table_uuid, location=table.location or "", trashed=False)
    detached = _marker(announcer, context.token, gone, segments=segments, operation=DEREGISTER_TABLE, uuids=known)
    detached_event = _table_event(context.token, gone, action="table_deregistered", table_uuid=table_uuid, name=canonical, extra={}, named_by=known)
    async with announcer.announcing(door="deregister_table", target=fga_ids.table_object(table_uuid), announcements=[detached, detached_event]) as filing:
        response = await retirement.detach(ns, context.settings, table, segments=segments)
        # The detach leaves the table's bytes, and the records end next, so a branch a recoverable delete hid is deleted
        # for good here ([[LH-429]]): left behind, its directory and the catalog's tag would stand with nothing to end
        # them. A purge that fails keeps its records, which the deadline's timer works from.
        await run_in_threadpool(
            hidden_branches.purge_all, table_claims.claim_store(context.settings), context.settings.storage_options(plane="data"), table_uuid
        )
        # The table leaves governance, so what was kept for it under its uuid goes.
        await run_in_threadpool(object_records.end_object_records, context.settings.registry_root, context.settings.storage_options(), "table", uuid=table_uuid)
        # Record the detach as lineage — asymmetric with drop (which deletes data), deregister only detaches, so without
        # this marker the feed reads it as a still-live, never-touched table. Versionless (no data was written), sent
        # BEFORE the revoke, so a revoke that fails ends the request with the marker already published. Lineage admits a
        # detach this catalog signed on that signature, never on the grants the revoke takes.
        await filing.send(detached)
        await object_grants.revoke_ownership(context, obj=fga_ids.table_object(table_uuid), origin="lifecycle_delete")
        await filing.send(detached_event)
    await converge.remember(200, response)
    return response


async def _deregister_unidentified(
    ns: LanceNamespace, settings: Settings, table: ResolvedTable, *, control: ControlEmitter, token: Caller | None, segments: list[str]
) -> DeregisterTableResponse:
    """Detach a table whose location carries no claim recording a uuid, and announce it ([[LH-534]]).

    Its control event names it by name (``table:<name>``), because no uuid object exists to name it by.
    """
    canonical = fga_ids.canonical_object_id(segments)
    response = await retirement.detach(ns, settings, table, segments=segments)
    log.warning("unidentified_table_deregistered", extra={"table": canonical, "location": table.location})
    await emit_control(
        control,
        action="table_deregistered",
        object_type="table",
        object_id=f"table:{canonical}",
        object_name=canonical,
        actor=f"user:{token.subject}" if token is not None else None,
        extra={},
    )
    return response


@spec_router.post("/{id}/register", response_model_exclude_none=True)
async def register_table(
    *,
    id: str,
    keyed: idem.KeyedDoorDep,
    body: RegisterTableRequest,
    ns: NamespaceDep,
    root: NamespaceRootDep,
    context: AuthzContextDep,
    so: StorageOptionsDep,
    uuids: ObjectUuidsDep,
    announcer: AnnouncerDep,
    resolver: ResolverDep,
) -> RegisterTableResponse:
    """Register an existing table location at ``id`` via ``register_table``, refuse it when the location
    overlaps the control root, a configured base or another table, when the dataset there carries reader
    flag 256, or when it declares a base nothing sanctions; record the bases it is admitted with, and emit a
    REGISTER_TABLE marker (who attached it + where).

    The table's grants are written under the uuid this request mints before the backend records it ([[LH-588]]), and
    taken back by every refusal. A registration killed after its grants and before the backend call leaves the grants
    alone, which the drift repair revokes. One killed between the backend call and the location's claim leaves a table
    no claim identifies (``object_uuids.UnidentifiedObjectError``, [[LH-534]]), reached only by the estate admin's
    deregister, whose grants no claim records and the drift repair revokes too: the claim is keyed by the location as
    the backend resolved it, which only the registration makes known.

    What the registration announces is filed once the dataset is admitted, the first moment its location as the backend
    resolved it is known, so a door failing after that still announces it, and a store refusing the filing detaches the
    registration ([[LH-576]])."""
    # BEFORE `idem.begin`, deliberately: these are SHAPE refusals, and minting an idempotency record for a request the
    # door was never going to attempt would make the replay of a malformed call converge on nothing.
    registering.refuse_an_unservable_registration(body)
    # LOCATION-EXCLUSIVITY, THE SHAPE HALF ([[LH-279]]) — a shape refusal like the mode's, so before
    # `idem.begin` for the same reason. What the path alone decides is refused before anything is
    # attached: the namespace root, a control prefix, the control root and every configured base.
    registration_context = table_bases.RegistrationContext(
        root=root,
        registry=base_registry.BaseRegistry(control_root=context.settings.registry_root, storage_options=context.settings.storage_options()),
        # What a declared base may be admitted inside: the entries approved for this table's warehouse ([[LH-410]]).
        configured=context.settings.external_blob_bases_for(parse_identifier(id)[0]),
        data_allowlist=context.settings.multibase_data_base_list,
        governed=governed_storage(context.settings),
        holder=fga_ids.canonical_object_id(parse_identifier(id)),
    )
    body.location = table_bases.require_registrable_location(
        body.location,
        root=root,
        control_root=context.settings.registry_root,
        # Every approved entry, whichever warehouse it is approved for: a table registered inside one would be vended
        # the bytes that base's pointers name.
        configured=[*context.settings.external_blob_base_list, *registration_context.data_allowlist],
    )
    # SAME AMPLIFIER AS create (see `catalog.api.idempotency`): a replay re-enters a door that
    # MINTS an id and seeds its ownership. OPTIONAL, because the spec defines no such header and a stock client must keep working.
    converge = await keyed.begin(target=fga_ids.canonical_object_id(parse_identifier(id)), takeover="rerun")
    if converge.replay is not None:
        return RegisterTableResponse.model_validate(converge.replay.body)

    segments = parse_identifier(id)
    await hierarchy.require_parent_exists(ns, "table", segments)
    body.id = reconcile_body_id(segments, body.id)
    # A new uuid for this table ([[LH-519]]), even where an earlier table's claim was released, and its grants under it,
    # written before the backend records the table so it never stands without them ([[LH-588]]). A request that
    # registers nothing takes them back: a refused registration changes nobody's access ([[LH-586]]).
    registration = registering.Registration(
        id=id,
        segments=segments,
        canonical=fga_ids.canonical_object_id(segments),
        table_uuid=str(uuid4()),
        relative=body.location,
        ns=ns,
        settings=context.settings,
        so=so,
        claims=table_claims.claim_store(context.settings),
        context=registration_context,
    )
    asked = await _clone_sources_asked(registration, context)
    response = await _register_or_take_back_the_seed(registration, body, context=context, resolver=resolver)
    # The location as the backend RESOLVED it, the spelling every other door keys the claim by: a `%` in
    # the body is stored re-encoded, so the body's spelling can decode to another key.
    stated_location = f"{root.rstrip('/')}/{body.location}"
    claimed_location = await run_in_threadpool(registering.absolute_table_location, ns, segments, stated_location) or stated_location
    await _require_parent_or_deregister(registration, context, resolver=resolver)
    admission = await _admit_or_detach(registration, claimed_location, asked, context=context)
    announcements = await _registration_announcements(registration, admission, announcer=announcer, token=context.token, uuids=uuids)
    async with _filed_or_undone(
        announcer,
        door="register_table",
        target=fga_ids.table_object(registration.table_uuid),
        announcements=announcements,
        undo=partial(_take_back_the_registration, registration, admission, context=context, reason="not_announced"),
    ) as filing:
        await _own_and_settle_the_registration(registration, admission, context=context, resolver=resolver)
        for announcement in announcements:
            await filing.send(announcement)
    await converge.remember(200, response)
    return response


async def _clone_sources_asked(registration: registering.Registration, context: AuthzContext) -> set[str]:
    """The uuids of the clone sources the caller was asked for, empty where a standing table of this name holds the location."""
    # A shallow clone reads its source's files, and registering it tags the source (ADR 0198 §1), so its caller must be
    # one who may tag each source, asked BEFORE anything is attached: a refused caller leaves no row, mark or claim behind.
    # A claim whose holder no longer resolves to the location is nobody's: the registration goes on, and the claim's own
    # take decides whether its lease has lapsed.
    stated_claim = await run_in_threadpool(
        location_claims.holder, table_claims.claim_store(registration.settings), f"{registration.context.root.rstrip('/')}/{registration.relative}"
    )
    held_by_a_standing_table = stated_claim is not None and not await run_in_threadpool(
        table_claims.holder_is_gone, registration.ns, table_claims.claim_store(registration.settings), stated_claim
    )
    # A location another standing table holds is refused first, so the dataset there is never read for a caller it is
    # not theirs.
    if stated_claim is not None and held_by_a_standing_table and stated_claim.table != fga_ids.canonical_object_id(registration.segments):
        log.warning("register_refused_held_location", extra={"table": registration.id, "location": stated_claim.location, "holder": stated_claim.table})
        raise table_bases.occupied_location_refusal(registration.relative)
    # One this table's own name holds, with the table standing, is a registration made again: the backend answers it
    # TableAlreadyExists (the spec's 409), and the dataset is not read for what it clones on the way there.
    registered_already = held_by_a_standing_table
    asked: set[str] = set()
    if not registered_already:
        asked = await _require_clone_sources(
            context,
            location=f"{registration.context.root.rstrip('/')}/{registration.relative}",
            so=registration.so,
            registration_context=registration.context,
        )
    return asked


async def _register_or_take_back_the_seed(
    registration: registering.Registration, body: RegisterTableRequest, *, context: AuthzContext, resolver: Resolver
) -> RegisterTableResponse:
    """Seed the table's grants and register ``body`` with the backend, taking the seed back when it registers nothing."""
    await object_grants.seed_before_write(
        context,
        resolver=resolver,
        segments=registration.segments,
        uuid=registration.table_uuid,
    )
    try:
        response: RegisterTableResponse = await run_in_threadpool(native.call, registration.ns, "register_table", body)
    except TableAlreadyExistsError:
        await object_grants.take_back_seed(
            context,
            segments=registration.segments,
            uuid=registration.table_uuid,
            reason="not_registered",
        )
        # A REFUSED REGISTRATION WRITES NO TUPLE ([[LH-586]], owner 2026-11-11). The spec gives this door two
        # modes — `Create` (409) and `Overwrite` — and no ExistOk, so the 409 is the answer, and a request that
        # is refused changes nobody's access: not an owner for its caller, and not the table's link to this
        # namespace either. A table a rename is moving, or left half-moved, is named here and inherits from the
        # namespace it entered, and an edge written here put every reader the move shut out back on it (measured
        # 2026-11-11). A table registered whose seed never ran carries no tuple at all and denies every
        # principal: the drift report lists it as ungoverned, and the estate admin writes its edges at the
        # tuples door.
        await registering.converge_a_registration_made_again(registration)
        raise
    except Exception:
        await object_grants.take_back_seed(
            context,
            segments=registration.segments,
            uuid=registration.table_uuid,
            reason="not_registered",
        )
        raise
    return response


async def _require_parent_or_deregister(registration: registering.Registration, context: AuthzContext, *, resolver: Resolver) -> None:
    """Refuse a registration whose namespace is not the one its request was authorized under, deregistering it first."""
    # The row is recorded by name, so the namespace it landed in is read now that it stands there ([[LH-585]]): one
    # dropped and created again since the router authorized this request is another namespace, and the registration
    # is detached before anything is claimed, marked or announced for it.
    try:
        await object_grants.require_parent_as_authorized(
            registration.ns, context.settings, context.client, resolver=resolver, resource="table", segments=registration.segments
        )
    except Exception:
        await object_grants.undo_a_create_whose_parent_is_unread(
            context,
            resource="table",
            segments=registration.segments,
            uuid=registration.table_uuid,
            undo=partial(registering.deregister, registration),
            # No claim was taken for it yet, so it is a table no uuid identifies ([[LH-534]]).
            remedy="the estate admin deregisters it before a retry",
        )
        raise


async def _admit_or_detach(registration: registering.Registration, claimed_location: str, asked: set[str], *, context: AuthzContext) -> registering.Admission:
    """Admit the registered dataset; a refusal detaches the registration and takes its grants back before it is answered."""
    # AFTER THE REGISTER, because only the backend knows which root a relative location resolves against and the overlap
    # check must see this registration's own row; BEFORE ANY RECORD, so a refused dataset gains no record entry, lineage
    # node or event, and the grants written for it are taken back.
    table_id = registration.id
    try:
        judged = await registering.admit_the_registration(registration, claimed_location, asked)
    except Exception as refusal:
        try:
            await registering.detach_the_registration(registration, claimed_location)
        except Exception as undo_exc:
            log.error("register_refusal_compensation_failed", extra={"table": table_id, "error": str(undo_exc)})
            raise PartiallyAppliedError(
                f"the dataset at {table_id} was refused ({refusal}) but its registration could not be detached",
                extensions={
                    "table": table_id,
                    "attached": True,
                    "refused": "refused" if isinstance(refusal, InvalidInputError) else "unjudged",
                    "remedy": "deregister it before retrying",
                },
            ) from undo_exc
        await object_grants.take_back_seed(
            context,
            segments=registration.segments,
            uuid=registration.table_uuid,
            reason="refused",
        )
        raise
    return registering.Admission(claimed_location=claimed_location, location=judged.verdict.location, admitted=judged.admitted)


async def _take_back_the_registration(registration: registering.Registration, admission: registering.Admission, *, context: AuthzContext, reason: str) -> None:
    """Undo the admitted registration, then take its grants back once it is gone."""
    await object_grants.undo_then_take_back(
        context,
        segments=registration.segments,
        uuid=registration.table_uuid,
        undo=partial(registering.undo_the_admitted_registration, registration, admission),
        reason=reason,
    )


async def _registration_announcements(
    registration: registering.Registration, admission: registering.Admission, *, announcer: Announcer, token: Caller | None, uuids: ObjectUuids
) -> list[Announcement]:
    """The marker and the control event the admitted registration files."""
    named = NameResolves(segments=registration.segments, uuid=registration.table_uuid)
    # RESOLVED, not echoed — `response.location` is the caller's own relative path, which names nothing a
    # reader of the feed can open; the flag judgement above already resolved it.
    # Versionless + source_uri=the attached location (register_table ∈ _CREATE_OPS, so the feed records it as a
    # create). The registered table already holds data at some version, which this marker does not name.
    registered = _marker(
        announcer,
        token,
        named,
        segments=registration.segments,
        operation=REGISTER_TABLE,
        uuids=await uuids.table(registration.segments, location=admission.location, uuid=registration.table_uuid),
        source_uri=admission.location,
    )
    registered_event = _table_event(
        token, named, action="table_registered", table_uuid=registration.table_uuid, name=registration.canonical, extra={"location": admission.location}
    )
    return [registered, registered_event]


async def _own_and_settle_the_registration(
    registration: registering.Registration, admission: registering.Admission, *, context: AuthzContext, resolver: Resolver
) -> None:
    """Seed the registered table's ownership again and settle what its admission took."""
    # The grants written before the backend call, written again now that the location's claim records their uuid: the
    # drift repair revokes grants no claim records, and the claim is taken only after the backend resolved the
    # location, so a repair landing between the two would have left the table without them.
    try:
        await object_grants.seed_ownership(
            context,
            resolver=resolver,
            resource="table",
            segments=registration.segments,
            uuid=registration.table_uuid,
        )
    except Exception:
        await _take_back_the_registration(registration, admission, context=context, reason="grant_failed")
        raise
    # Admitted and owned: a clone's ending filed for the registration goes, its own end filing one from its record.
    await run_in_threadpool(partial(table_bases.settle_admitted, admission.admitted, context=registration.context))


async def _require_clone_sources(
    context: AuthzContext, *, location: str, so: StorageOptions, registration_context: table_bases.RegistrationContext
) -> set[str]:
    """Refuse registering the dataset at ``location`` unless its caller may read and tag every source it reads through as
    a shallow clone (:data:`table_bases.CLONE_RELATIONS`); the uuids of the sources asked for, empty for a dataset that
    is no clone.

    The sources are asked in the order the clone reads through them. A caller holding nothing on a source is refused the
    same whether a governed table is at its root or not, by a sentence naming only that root, which the caller's own
    manifest holds ([[LH-436]]); which table it was is logged for the operator.

    Raises:
        PermissionDeniedError: The caller may not read or may not tag a source, or no table the catalog governs is there
            (403).
        ServiceUnavailableError: The store did not answer a read of the dataset, a source's claim or a record (503).
        InternalError: The dataset is there and could not be read (500).
    """
    clone = await run_in_threadpool(table_bases.clone_read, location, so, registration_context)
    if clone is None:
        return set()
    for source in clone.sources:
        try:
            for relation in table_bases.CLONE_RELATIONS:
                await door_gates.require_relation_on_a_path(context, relation=relation, obj=fga_ids.table_object(source.table_uuid), path=source.location)
        except PermissionDeniedError:
            log.warning("register_refused_clone_source", extra={"location": location, "source": source.table})
            raise
    if clone.ungoverned is not None:
        log.warning("register_refused_ungoverned_clone_source", extra={"location": location, "source": clone.ungoverned})
        await door_gates.require_relation_on_a_path(context, relation=table_bases.CLONE_RELATIONS[0], obj=None, path=clone.ungoverned)
    return {source.table_uuid for source in clone.sources}


@management_router.get("/{id}/protection", response_model_exclude_none=True)
async def get_table_protection(id: str, table: ResolvedTableDep, settings: SettingsDep) -> ProtectionResponse:
    """Read the deletion-protection flag (#123). The SET door shipped a year of writes with NO read:
    an operator could not arm, disarm-check, or audit protection — they discovered it by eating a
    409. Owner-gated like the set door (the observer of a safety is whoever might trip it), and the
    record's `set_by` comes back so a refused drop can name who armed it."""
    canonical = fga_ids.canonical_object_id(table.segments)
    record = await run_in_threadpool(protections.read, settings, "table", table.require_uuid())
    return ProtectionResponse(id=canonical, protected=record is not None and record.protected, set_by=record.set_by if record is not None else None)


@management_router.get("/{id}/tasks", response_model_exclude_none=True)
async def table_tasks(
    id: str,
    resolver: ResolverDep,
    uuid: TrashedUuidQuery = None,
) -> list[TrashEntry]:
    """What is queued for THIS table (#75 brings §2.4). Today that is exactly one thing: a pending
    trash expiry. It exists the moment expiry does, because an undrop deadline the owner cannot see
    is not a safety feature — the estate's task surfaces are otherwise all estate-global, so "what is
    scheduled against my table" was unanswerable. Reader-gated by the router alongside describe.

    `expires_at` is when recovery ends (ADR 0146): past it the undrop is refused, maintenance's reconcile tick revokes
    the table's grants, and the bytes wait for the purge. `expired` says which side of it the drop is on.

    One entry per dropped table that held the name and that the caller may see, each with its uuid, which an undrop
    names when there are several ([[LH-525]]).
    """
    return [trash_doors.entry(record) for record in await resolver.admitted_trash("table", parse_identifier(id))]


@management_router.post("/{id}/undrop", response_model_exclude_none=True)
async def undrop_table(
    *,
    id: str,
    keyed: idem.KeyedDoorDep,
    ns: NamespaceDep,
    root: NamespaceRootDep,
    settings: SettingsDep,
    token: CurrentToken,
    announcer: AnnouncerDep,
    uuids: ObjectUuidsDep,
    resolver: ResolverDep,
    uuid: TrashedUuidQuery = None,
) -> RegisterTableResponse:
    """Recover a dropped table from the trash (#75) — re-register its still-present bytes at its old
    id and clear the record. Owner-gated (``undrop`` maps to the drop rung: restoring an object into
    the namespace is the same authority as removing it).

    404 when there is no trash record — a never-trashed or already-PURGED drop is genuinely
    unrecoverable, and saying so plainly beats a 200 that recovers nothing. 409 past the record's
    `expires_at`: the drop is then DropTable's, its data deleted asynchronously, even while its bytes
    stand (ADR 0146).

    THE NAME IS FREE ONCE ITS TABLE IS DROPPED ([[LH-525]], ADR 0173 §9), so the door restores exactly the trashed table
    its caller names: the one dropped under the name, or, when several were, the one ``?uuid=`` names (409 listing each
    one's uuid and deadline otherwise). It restores that table's uuid, and so its grants and records. While another
    table holds the name the undrop answers 409: two tables cannot share it. What it announces is filed before it takes
    the location back, so a door failing after the re-registration still announces it ([[LH-576]]).
    """
    segments = parse_identifier(id)
    canonical = fga_ids.canonical_object_id(segments)
    # As the table doors (`catalog.api.idempotency`): a replay under the caller's key answers the first attempt and re-enters nothing.
    converge = await keyed.begin(target=canonical, takeover="rerun")
    if converge.replay is not None:
        return RegisterTableResponse.model_validate(converge.replay.body)
    so = settings.storage_options()
    restoring = await _the_drop_to_restore(ns, resolver, segments=segments, canonical=canonical)
    # `register_table` REFUSES an absolute URI ("Location must be a relative path within the root directory"), and keeps
    # a relative one as the literal key it is given. The record keeps the URL `describe_table` answered, which
    # percent-encodes a registered table's name, so the table re-attaches at the key that URL decodes to, under the
    # connection's root, where its bytes are ([[LH-517]]). The absolute URL stays on the record for an operator.
    location = restoring.record.location
    body = RegisterTableRequest(id=segments, location=table_objects.relative_location(location, root=root))
    named = NameResolves(segments=segments, uuid=restoring.uuid)
    restored = _marker(
        announcer,
        token,
        named,
        segments=segments,
        operation=REGISTER_TABLE,
        uuids=await uuids.table(segments, location=location, uuid=restoring.uuid),
        source_uri=location,
    )
    restored_event = _table_event(token, named, action="table_undropped", table_uuid=restoring.uuid, name=canonical, extra={})
    async with announcer.announcing(door="undrop_table", target=fga_ids.table_object(restoring.uuid), announcements=[restored, restored_event]) as filing:
        await registering.take_the_location_back(ns, settings, location, segments=segments, canonical=canonical, record_uuid=restoring.uuid)
        try:
            response: RegisterTableResponse = await run_in_threadpool(native.call, ns, "register_table", body)
        except TableAlreadyExistsError:
            # ALREADY RECOVERED — converge instead of refusing, as the namespace undrop does
            # (`undrop_table_already_registered`): a crash between this register and the `trash.clear` below
            # leaves a record on a LIVE table, which maintenance excludes from compaction and version GC while
            # the record stands (F6(d)), so the retry falls through to the clear and the table rejoins the sweep.
            log.info("undrop_table_already_registered", extra={"table": canonical})
            response = RegisterTableResponse(location=location)
        await run_in_threadpool(warn_if_mixed_file_versions, location, so, table=canonical)

        # NO SEED — undrop is a PURE RESTORE (owner ruling, diff2 F10 item 4b). The recoverable drop keeps the
        # table's tuples (#75 — the owner is the one person who needs to undrop it), so the original owner's grants
        # are live when the undrop runs; seeding the caller would only ADD whoever pressed the button as a second
        # owner with no grant ever having been made.
        #
        # The deliberate trade: a table that was ALREADY ownerless before the drop stays ownerless after the
        # undrop, rather than being papered over by granting the rescuer, a privilege escalation hiding the
        # stranding. Surfacing stranded objects is the reconciler's job (diff2 F3.3), not this door's.
        #
        # Nothing between the register and the clear needs compensating: the register is idempotent above, and a
        # failed clear converges on retry.
        # THE TABLE EXISTS AGAIN, and lineage has to hear it: the drop stamped the dataset DROP, and without a
        # CREATE fact the reconcile would skip a live governed table for good ([[LH-144]]). The same
        # versionless marker the register door sends, at the absolute location the record kept. Before the
        # clear, so a failed clear that is retried announces again under the ids its filing already holds.
        await filing.send(restored)
        await run_in_threadpool(trash.clear, settings.registry_root, so, restoring.uuid)
        await filing.send(restored_event)
    await converge.remember(200, response)
    return response


async def _the_drop_to_restore(ns: LanceNamespace, resolver: Resolver, *, segments: list[str], canonical: str) -> registering.DropToRestore:
    """The trash record an undrop of ``segments`` restores, and the uuid it keeps, once nothing refuses restoring it."""
    trashed = await resolver.admitted_trash("table", segments)
    if not trashed:
        raise TableNotFoundError(
            f"no recoverable drop for table: {canonical}. Either it was never dropped recoverably "
            "(the grace period was off), or the purge has already reclaimed its bytes."
        )
    record = trash_doors.the_one_to_restore(trashed, kind="table", name=canonical)
    trash_doors.require_restorable(record, name=canonical)
    record_uuid = record.uuid
    if record_uuid is None:
        raise InternalError(f"the trash record of {canonical} keeps no uuid, so its grants cannot be found")
    standing = await resolver.table(segments, ns=ns)
    if standing.location is not None and standing.uuid != record_uuid:
        raise TableAlreadyExistsError(f"cannot undrop {canonical}: another table holds the name now; rename or drop it first")
    trash_doors.require_its_own_parent(record, standing=await resolver.namespace_uuid(segments[:-1]), name=canonical)
    return registering.DropToRestore(record=record, uuid=record_uuid)


@management_router.post("/{id}/protection", response_model_exclude_none=True)
async def set_table_protection(
    *,
    id: str,
    body: SetProtectionRequest,
    table: ResolvedTableDep,
    settings: SettingsDep,
    token: CurrentToken,
    control: ControlEmitterDep,
) -> ProtectionResponse:
    """Set or clear deletion protection on the table at ``id`` (#73 — the warehouse contract on the
    rung where a drop deletes bytes). Owner-gated by the router (``protection`` maps to
    ``can_set_protection``, a computed userset over ``owner``: whoever may destroy the table decides
    whether destroying it needs a second thought). Its OWN relation at the drop's tier rather than
    ``can_drop`` itself, because `#41` audits by the relation and the route suffix is not in the
    record — sharing the name made arming the safety and destroying the table one audit line. The flag is
    a CONTROL-ROOT record, deliberately not schema metadata — control-plane state that emits a
    control event and never creates a table version, readable even when the dataset is corrupted,
    and unreachable from the future properties write door (#78)."""
    canonical = fga_ids.canonical_object_id(table.segments)
    table_uuid = table.require_uuid()
    so = settings.storage_options()
    if body.protected:
        record = ProtectionRecord(
            kind="table", id=canonical, uuid=table_uuid, protected=True, set_by=f"user:{token.subject}" if token is not None else "anonymous"
        )
        await run_in_threadpool(protection.set_protection, settings.registry_root, so, record)
    else:
        await run_in_threadpool(protection.clear_protection, settings.registry_root, so, "table", uuid=table_uuid)
    await emit_control(
        control,
        action="table_protected" if body.protected else "table_unprotected",
        object_type="table",
        object_id=fga_ids.table_object(table_uuid),
        object_name=canonical,
        actor=f"user:{token.subject}" if token is not None else None,
        extra={},
    )
    return ProtectionResponse(id=canonical, protected=body.protected)


@spec_router.post("/{id}/rename", response_model_exclude_none=True)
async def rename_table(
    *,
    id: str,
    keyed: idem.KeyedDoorDep,
    body: RenameTableRequest,
    ns: NamespaceDep,
    table: ResolvedTableDep,
    so: StorageOptionsDep,
    context: AuthzContextDep,
    uuids: ObjectUuidsDep,
    announcer: AnnouncerDep,
    resolver: ResolverDep,
    root: NamespaceRootDep,
    force: RaskFlag = False,
) -> RenameTableResponse:
    """Rename the table at ``id`` by moving its POINTER, and emit lineage; its grants stay on its uuid.

    Lance has no format-level rename (table rename is a namespace-layer remap, not a data-plane commit) and
    the ``dir`` backend's ``rename_table`` answers only Unsupported, which is also off-spec — the Namespace REST spec
    makes rename a first-class Metadata op. It is served here as what the spec describes: the destination id
    is registered at the SOURCE's existing location and the source id is deregistered. **No data byte is
    read, written or deleted**, so the cost is O(1) rather than the dataset's size — see
    ``tables.rename.rename_table`` for the lance-ns V2 naming rule that makes this correct and for the one
    shape (a V1 root-namespace table) it refuses.

    Within its namespace the caller must hold ``can_drop`` on the source (the modifier's rung, gated by the router
    ``authorize``) AND ``can_create_table`` on that namespace. The table keeps its uuid, so every record and every grant
    kept for it stays where it is ([[LH-523]], [[LH-524]]), and a rename into ANOTHER namespace therefore takes its grants
    along: it asks ``can_move`` on the source and ``can_accept_moved_table`` on the destination's namespace instead,
    which under managed access need grant authority on the table and on the destination ([[LH-568]]), else a writer
    could bring grants into a managed namespace and an owner inside one take a table, and the authority to grant on it,
    out. Into another namespace only where it inherits from moves (``object_grants.parent_edge_mover``): its ``parent`` edge
    in one write before the name, its ``child`` edge taken from the source with it and written on the destination once
    the name is there, and both go back if the rename is undone. Its data directories' claims stay as they are: each
    records the table's uuid and location, which a rename changes neither of ([[LH-575]]). One versionless ``rename_table`` DatasetEvent announces it on the new id, its standard
    lifecycle state ``RENAME`` naming the old id as ``previousIdentifier``: one table under a new name, never a table dropped
    and another created ([[LH-554]]), filed before the rename so a door failing after it still announces it ([[LH-576]]).
    Source missing → 404 ``TableNotFound``; destination name taken → 409 ``TableAlreadyExists``; the location held by a
    concurrent rename or registration → 409 ``ConcurrentModification`` (code 14, [[LH-204]])."""
    # SAME AMPLIFIER AS create (see `catalog.api.idempotency`): a replay re-enters a door that
    # retires the source id and re-registers the destination. OPTIONAL, because the spec defines no such header and a stock client must keep working.
    segments = parse_identifier(id)
    body.id = reconcile_body_id(segments, body.id)  # a contradictory body id is a 400, like every {id} route
    # Rename mints a new table identifier: ``new_namespace_id`` (else the source's own namespace) + ``new_table_name``.
    new_segments = rename_destination(segments, body.new_namespace_id, body.new_table_name)
    # The key binds the destination too: under it, a rename to another name is another operation ([[LH-576]]). A takeover
    # runs again: a rename that landed leaves the source holding nothing, and the run refuses 404.
    source = fga_ids.canonical_object_id(segments)
    converge = await keyed.begin(
        target=f"{source} -> {fga_ids.canonical_object_id(new_segments)}", captured=idem.capture(source, _resolved_object(table)), takeover="rerun"
    )
    if converge.replay is not None:
        return RenameTableResponse.model_validate(converge.replay.body)

    # #73: a rename RETIRES the source id, so the source's protection gates it exactly like drop even
    # though no byte moves: every reader and pipeline addressing the table by its id loses it. `force` rides
    # the query string as on the sibling doors, and the protection travels with the table to its new name.
    # Checked FIRST — before the parent/create gates — so a protected source refuses identically regardless
    # of destination.
    await hierarchy.require_table_unprotected(context.settings, table, force=force)
    # A rename can MINT an orphan as surely as a create, so its destination's namespace must exist. Same rule, same
    # door — checked before the native call, so a refused rename leaves the source exactly where it was.
    hierarchy.require_rename_within_its_warehouse(segments, new_segments)
    await hierarchy.require_parent_exists(ns, "table", new_segments)
    # A rename mints a name in the destination's namespace, so the caller must be one who may put a table there, asked
    # BEFORE the (destructive, relocating) rename. (authorize already asked the source's rung.)
    await door_gates.require_rename_destination(context, resolver=resolver, source=segments, destination=new_segments)
    renamed = fga_ids.table_object(table.require_uuid())
    # A rename moves no grant ([[LH-524]]): every grant is held by the table's uuid, which the rename keeps. Into
    # another namespace its parent and child edges move with it, and those are all it writes in OpenFGA.
    reparent = await object_grants.parent_edge_mover(context, resolver=resolver, obj=renamed, source=segments, destination=new_segments)
    # A rename keeps the table's uuid (ADR 0173 §2): the event and the control event name the one the door resolved, and
    # its location, which a rename keeps (`tables.rename.rename_table`). Both are filed before the rename, which landed once
    # the destination's name resolves to the table ([[LH-576]]).
    destination_ids = await uuids.table(new_segments, uuid=table.uuid)
    named = NameResolves(segments=new_segments, uuid=table.require_uuid())
    # Versionless: the version history moved with the pointer, and the event names none of it. The location rides it so
    # the table's symlink is the one its earlier events named.
    moved = _marker(
        announcer, context.token, named, segments=new_segments, operation=RENAME_TABLE, uuids=destination_ids, source_uri=table.location, renamed_from=segments
    )
    moved_event = _table_event(
        context.token,
        named,
        action="table_renamed",
        table_uuid=table.require_uuid(),
        name=fga_ids.canonical_object_id(new_segments),
        extra={"from": fga_ids.canonical_object_id(segments), "to": fga_ids.canonical_object_id(new_segments)},
        named_by=destination_ids,
    )
    async with announcer.announcing(door="rename_table", target=renamed, announcements=[moved, moved_event]) as filing:
        await run_in_threadpool(
            partial(
                rename.rename_table,
                ns,
                table,
                body.new_table_name,
                new_namespace_id=body.new_namespace_id,
                so=so,
                claims=table_claims.claim_store(context.settings),
                root=root,
                reparent=reparent,
            )
        )
        await filing.send(moved)
        await filing.send(moved_event)
    response = RenameTableResponse()
    await converge.remember(200, response)
    return response


@spec_router.post("/{id}/restore", response_model_exclude_none=True)
async def restore_table(
    *,
    id: str,
    keyed: idem.KeyedDoorDep,
    body: RestoreTableRequest,
    table: DoorTableDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    uuids: ObjectUuidsDep,
) -> RestoreTableResponse:
    """Restore the table at ``id`` to a prior version via ``restore_table``; emits a RESTORE_TABLE event at
    the NEW current version (restore mints a fresh version pointing at the restored data)."""
    # SAME AMPLIFIER AS create (see `catalog.api.idempotency`): a replay re-enters a door that
    # REWINDS the table to an older version. OPTIONAL, because the spec defines no such header and a stock client must keep working.
    converge = await keyed.begin(target=fga_ids.canonical_object_id(parse_identifier(id)), takeover="unknown")
    if converge.replay is not None:
        return RestoreTableResponse.model_validate(converge.replay.body)

    segments = parse_identifier(id)
    body.id = reconcile_body_id(segments, body.id)
    # THE BRANCH IS SERVED: `tables.writes.restore` opens the ref named at the door's location, where a missing branch
    # answers 22 and a version the branch lacks answers 11, the codes `_open_ref` mints for every other door, and
    # restores through that handle, which commits on the ref it is on (measured on pylance 13.0.0: a branch restored
    # to an earlier version took a new version of its own, and main kept its version).
    # The event names the version THAT restore committed on the ref it restored, and the shared trailer reads
    # its schema off one pinned open (best-effort: a readback failure never fails the committed restore).
    response, committed = await run_in_threadpool(
        partial(writes.restore, table, so, body, transaction_properties=lineage_deps.door_stamp(token, RESTORE_TABLE))
    )
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        location=table.location,
        so=so,
        token=token,
        operation=RESTORE_TABLE,
        uuids=uuids,
        pin_version=committed,
        branch=body.branch,
    )
    await converge.remember(200, response)
    return response


@spec_router.post("/{id}/stats", response_model_exclude_none=True)
def get_table_stats(
    *,
    id: str,
    table: DoorTableDep,
    so: StorageOptionsDep,
    body: GetTableStatsRequest | None = None,
    branch: Annotated[
        str | None,
        Query(
            description="REFUSED: the upstream implementation answers from main regardless, so honouring this would return main's data labelled as this ref."
        ),
    ] = None,
) -> GetTableStatsResponse:
    """Return storage/row statistics for the table at ``id`` via ``get_table_stats``.

    ``branch`` is DECLARED here only so it can be refused, and the numbers are why. Verified live
    2026-08-31 on a table whose branch held 1 row while main held 4: this door reported
    ``num_rows: 4`` when asked for the branch, and again for a branch that had never been created —
    through both the query string and the body. A row count is the most quoted number a catalog
    produces; returning main's under a branch's name is the defect class at its most quotable.

    Refused rather than served because the response cannot be assembled honestly from a branch handle:
    ``FragmentStats.lengths`` and the per-index ``status``/``size_bytes`` are not what
    ``dataset_stats()`` reports, and inventing them to fill a required field is the same failure in
    miniature. Serving it properly is named in the lakehouse register, row O1 (drained 2026-09-10; in git history).
    """
    segments = reconcile_body_id(parse_identifier(id), body.id if body else None)
    if body is not None and "branch" in body.model_fields_set:
        branch = body.branch
    req = GetTableStatsRequest(id=segments, branch=branch)
    reads.refuse_a_branch_this_door_cannot_honour(branch, door="get_table_stats")
    # The request carries no version to pin, so the native read opens the table again after this judgement. No door
    # changes a table's base list between the two: every commit is judged at `version/create` or `commit_stamped`,
    # which refuse a base added, removed or repointed, and a writer's credential puts no final manifest, which every
    # warehouse's store is probed for ([[LH-511]]). A base the second open would read unjudged is therefore one written
    # past the store's own policy ([[LH-323]]).
    table.open(so)
    stats = table.read_natively("get_table_stats", req)
    return stats
