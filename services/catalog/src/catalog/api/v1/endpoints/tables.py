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
from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    ConcurrentModificationError,
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
    RestoreTableRequest,
    RestoreTableResponse,
    TableAlreadyExistsError,
    TableExistsRequest,
    TableNotFoundError,
    UnsupportedOperationError,
)
from openfga_sdk import OpenFgaClient

from catalog.api import fga_deps, lineage_deps, listings
from catalog.api import idempotency as idem
from catalog.api.announcing import Announcer, AnnouncerDep, Filing
from catalog.api.dependencies import (
    ControlEmitterDep,
    DoorTableDep,
    FgaClientDep,
    LineageEmitterDep,
    NamespaceDep,
    NamespaceRootDep,
    ObjectUuidsDep,
    ResolvedTableDep,
    ResolverDep,
    SettingsDep,
    StorageOptionsDep,
    TrashedUuidQuery,
    VendorDep,
    namespace_for_warehouse,
)
from catalog.api.pagination import ListLimit, paginate
from catalog.api.rask_params import RaskFlag
from catalog.api.security import Caller, CurrentToken, current_subject
from catalog.core import json_arrow, provenance_guard, write_judge
from catalog.core.base_judge import BaseJudge, GovernedStorage
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
from catalog.core.modes import RegisterMode
from catalog.core.namespace import open_resolved_dataset_unchecked, registered_dataset_facts
from catalog.core.vending import (
    CredentialVendor,
    ModeBVendor,
    TableRecords,
    Tier,
    VendableBases,
    dataset_facts,
    require_a_table_maintain_policy_that_fits,
    require_vendable_bases,
    role_session_name,
    unsanctioned_bases,
    vend_or_refuse,
)
from catalog.schemas import ProtectionResponse, RegisteredTable, RenamedTable, SetProtectionRequest, TrashEntry
from catalog.services import clone_endings, commit_reconcile, dataplane, native, reclaims, table_bases, table_claims, trash_doors, warehouses
from catalog.services.announcement_intents import Announcement, ControlMessage, NameResolves
from catalog.services.table_claims import DoorTable, ResolvedTable
from lakehouse import base_registry, condemned_versions, history_fences, location_claims, object_records, protection, trash
from lakehouse.clone_endings import CloneEnding
from lakehouse.control_emit import ControlEmitter, emit_control
from lakehouse.control_events import CatalogControlEvent, ControlAction
from lakehouse.features import CREATE_FORMAT, table_file_format
from lakehouse.fences import Hold
from lakehouse.governed import fga
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit
from lakehouse.governed.deps import ANONYMOUS_SUBJECT
from lakehouse.history_fences import HistoryWork
from lakehouse.lineage_events import DatasetUuids
from lakehouse.naming import uuid_of
from lakehouse.ns_errors import PartiallyApplied
from lakehouse.storage_options import StorageOptions
from lakehouse.version_retention import MAIN_BRANCH, recorded_branch


log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/table", tags=["table"])

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
            found.extend(fga.canonical_object_id([*parent, name]) for name in tables)
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


@router.get("", response_model_exclude_none=True)
async def list_all_tables(
    request: Request,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    client: FgaClientDep,
    resolver: ResolverDep,
    limit: ListLimit,
    page_token: str | None = None,
    include_declared: bool = True,
) -> ListTablesResponse:
    """List every table of every warehouse; when FGA is on, only those the caller can ``can_read_data``.

    ``include_declared=false`` lists only the tables that hold a committed version, each judged by its own storage
    (:func:`catalog.api.listings.holding_a_version`, [[LH-528]]).

    Pagination is applied to the merged, sorted result, never to a native call, whose ``limit`` would truncate one
    warehouse's walk and drop tables from every page. The cursor is keyset-style: ``page_token`` is the last name of
    the page, stateless and stable across pages. The walk costs the full estate server-side per call, acceptable at
    admin frequency.
    """
    response = ListTablesResponse(tables=[])
    candidates = [(name, parse_identifier(name)) for name in sorted(await _tables_of_every_warehouse(request, settings))]
    if not include_declared:
        candidates = await listings.holding_a_version(resolver, so, candidates)
    # With FGA on, return only the tables the caller can read, each judged on the object its name resolves to; pages
    # count only tables the caller can see.
    if (authz := fga_deps.authorization(settings, client, token)) is not None:
        response.tables, response.page_token = await fga_deps.authorized_page(
            authz,
            resolver,
            relation="can_read_data",
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
    bases = require_vendable_bases(location, facts, BaseJudge.from_settings(settings))
    if facts.classified or unsanctioned_bases(location, bases.bases, settings.multibase_data_base_list):
        return None
    return bases


def _mint(
    location: str, *, tier: Tier, bases: VendableBases, vendor: CredentialVendor, session_name: str, at_most_seconds: int | None
) -> dict[str, str] | None:
    """The ``storage_options`` the vendor mints for ``location`` at ``tier``, with the expiry the spec reads, lasting at
    most ``at_most_seconds`` where that is set.

    THE EXPIRY GOES INSIDE ``storage_options``, where the spec puts it and the only place a stock client looks
    (``lance_docs/ns_catalog/spec.yaml`` § DescribeTableResponse: "If the vended credentials are temporary, the
    ``expires_at_millis`` key ..."). Without it a temporary credential looks permanent, and the client discovers
    the TTL by failing at the 900-second mark instead of refreshing.
    """
    vended = vend_or_refuse(
        vendor,
        table_location=location,
        tier=tier,
        session_name=session_name,
        bases=bases.bases,
        data_directories=bases.data_directories,
        at_most_seconds=at_most_seconds,
    )
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
    settings: Settings,
    so: StorageOptions,
    vendor: CredentialVendor,
    token: Caller | None,
    client: OpenFgaClient | None,
    asked: bool,
    at_most_seconds: int | None = None,
) -> dict[str, str] | None:
    """Vend the highest tier FGA grants the caller on ``segments`` (ADR 0156 C3), and record the issuance (#41).

    ``table_uuid`` and ``location`` are the table's as the door read them together, so the tier is decided on the
    table the credential reaches.

    A stock client opening a table through the namespace signs with what the spec door returns, and one that
    gets nothing signs with whatever its environment holds; vending here is what keeps it off an ambient key.

    The order is the cost and the record: whether the table is directly readable at all is settled first (a base
    nothing sanctioned is refused there, whatever the vendor), so a server-mediated table, or a deployment that
    vends nothing, asks FGA nothing. The tier is
    :func:`fga_deps.spec_vend_tier`'s. Only then is a credential minted, and only a mint writes the
    ``vend_credentials`` issuance record.

    ``asked`` is ``vend_credentials`` set true. Left unset, the credential is the server's offer: a refusal or a
    failed exchange leaves the answer without one and is logged, because a metadata read, the catalog's most
    called door, must not fail on the object store's STS. Asked for, the refusal is the answer.

    ``at_most_seconds`` caps the credential's life: a declaration's is no longer than its fence
    (:func:`table_claims.declaration_credential_seconds`).

    Raises:
        PermissionDeniedError: The caller asked and FGA grants it no rung that opens a tier.
        LanceNamespaceError: The caller asked and the vend was refused or failed (409, 401, 503).
    """
    try:
        bases = await run_in_threadpool(_vendable_bases, location, settings, so)
        if bases is None or isinstance(vendor, ModeBVendor):
            return None
        tier = await fga_deps.spec_vend_tier(settings, token, client, ns, segments, table_uuid=table_uuid, door=door)
        if tier is None:
            if asked:
                raise PermissionDeniedError(f"can_read_data required on table:{fga.canonical_object_id(segments)} to vend credentials")
            return None
        subject = token.subject if token is not None else ANONYMOUS_SUBJECT
        options = await run_in_threadpool(
            partial(_mint, location, tier=tier, bases=bases, vendor=vendor, session_name=role_session_name(subject), at_most_seconds=at_most_seconds)
        )
    except LanceNamespaceError:
        if asked:
            raise
        log.warning("spec_vend_skipped", extra={"door": door, "table": "/".join(segments)}, exc_info=True)
        return None
    if options is not None:
        resource = fga.table_object(table_uuid)
        audit(AuditAction.VEND_CREDENTIALS, AuditOutcome.SUCCESS, subject=token.subject if token is not None else None, resource=resource, tier=tier, door=door)
    return options


# `paginate` lives in `catalog.api.pagination`, shared by every catalog listing: two keyset implementations in one
# service is two things to keep in step.
@router.post("/{id}/declare", response_model_exclude_none=True)
async def declare_table(
    id: str,
    ns: NamespaceDep,
    root: NamespaceRootDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    vendor: VendorDep,
    token: CurrentToken,
    client: FgaClientDep,
    uuids: ObjectUuidsDep,
    announcer: AnnouncerDep,
    resolver: ResolverDep,
    body: DeclareTableRequest | None = None,
) -> DeclareTableResponse:
    """Declare a new (empty) table at ``id``, at the namespace root under the uuid the catalog mints for it
    (``dataplane.declare_table``, [[LH-520]]), then seed the caller's FGA ownership and emit a versionless
    DECLARE_TABLE marker (the table's first provenance — who reserved it + where).

    The catalog chooses a declared table's location, which the spec leaves to the implementation
    (`lance_docs/ns_catalog/spec.yaml:3831-3835`), so a request naming one is refused. The declaration keeps no
    ``properties``: it is recorded through RegisterTable, which keeps none (`spec.yaml:2719-2724`).

    The response carries the credential the declarer writes the table with, the highest tier FGA grants
    (:func:`_vend_on_spec_door`), unless the request sets ``vend_credentials`` false.
    """
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
    segments = parse_identifier(id)
    await fga_deps.require_parent_exists(ns, "table", segments)
    req = body or DeclareTableRequest()
    req.id = reconcile_body_id(segments, req.id)
    canonical = fga.canonical_object_id(segments)
    claims = table_claims.claim_store(settings)
    declarer = current_subject(token)
    # A DECLARATION IS ITS DECLARER'S (ADR 0227 §9): sent again by the subject that made it, nothing having acted on it
    # since, the declaration is renewed under the ETag its resolution read and answered with a credential vended after
    # the renewal, lasting no longer than the fence (RedeclareRenews). One another request acted on since is answered as a
    # first declare of the name is now.
    own = await run_in_threadpool(partial(dataplane.own_declaration, ns, so, segments, claims=claims, declarer=declarer))
    if own is not None and own.location is not None:
        return await _vended_declaration(
            ns, so, segments, req, location=own.location, table_uuid=own.require_uuid(), undo=None, settings=settings, vendor=vendor, token=token, client=client
        )
    # The table's uuid is minted with its location's claim, before anything grants or announces it (ADR 0173 §2, §5),
    # and its grants are written once the claim records it, before the backend records the table ([[LH-588]]). The claim
    # is fenced for the declarer until its first version or the fence's lease (ADR 0227 §9).
    table_uuid = str(uuid4())
    # The fence the declare takes its claim under: the declaration's, until a request of the declarer renews it.
    declared_under = table_claims.DoorFence(location=f"{root.rstrip('/')}/{table_uuid}", token=uuid4().hex)
    grants = fga_deps.GrantsBeforeWrite(client, settings, token, resolver, segments=segments)
    try:
        location = await run_in_threadpool(
            partial(
                dataplane.declare_table,
                ns,
                so,
                segments,
                root=root,
                claims=claims,
                holder=canonical,
                table_uuid=table_uuid,
                before_register=grants,
                fence_for=declared_under.token,
                declarer=declarer,
            )
        )
    except Exception:
        await grants.take_back_unless_standing(ns)
        raise

    async def _undo_declare() -> None:
        # The rollback renews this declare's own fence first, a plain fence of its own (ADR 0227 §8's rule for an effect
        # it cannot take back): a fence another request wrote since, the declarer's DeclareTable sent again and answered
        # this declaration included, raises FenceLostError, and the declaration is left standing as that request's.
        await run_in_threadpool(table_claims.renew_fence, claims, declared_under)
        await run_in_threadpool(native.call, ns, "drop_table", DropTableRequest(id=segments))
        await run_in_threadpool(location_claims.release, claims, location, canonical)

    async def _take_back_declare() -> None:
        await fga_deps.undo_then_take_back(client, settings, token, segments=segments, uuid=table_uuid, undo=_undo_declare, reason="not_announced")

    # What the declaration announces is filed once the location the backend describes it at is known, before its
    # ownership is seeded, so a door failing after that still announces it, and a store refusing the filing takes the
    # declaration back ([[LH-576]]). It landed while the name resolves to the table.
    named = NameResolves(segments=segments, uuid=table_uuid, held=True)
    # Versionless (no data yet): records who declared it + the reserved location, and keys the CREATED edge
    # (declare_table ∈ lineage _CREATE_OPS). Reconcile fills the real version once data lands at the URI.
    declared = _marker(
        announcer, token, named, segments, operation=DECLARE_TABLE, uuids=await uuids.table(segments, location=location, uuid=table_uuid), source_uri=location
    )
    declared_event = _table_event(token, named, action="table_declared", table_uuid=table_uuid, name=canonical, extra={"location": location})
    async with _filed_or_undone(
        announcer, door="declare_table", target=fga.table_object(table_uuid), announcements=[declared, declared_event], undo=_take_back_declare
    ) as filing:
        response = await _vended_declaration(
            ns, so, segments, req, location=location, table_uuid=table_uuid, undo=_undo_declare, settings=settings, vendor=vendor, token=token, client=client
        )
        await filing.send(declared)
        await filing.send(declared_event)
    return response


async def _vended_declaration(
    ns: LanceNamespace,
    so: StorageOptions,
    segments: list[str],
    req: DeclareTableRequest,
    *,
    location: str,
    table_uuid: str,
    undo: Callable[[], Awaitable[None]] | None,
    settings: Settings,
    vendor: CredentialVendor,
    token: Caller | None,
    client: OpenFgaClient | None,
) -> DeclareTableResponse:
    """The declaration of ``segments`` at ``location`` as its declarer is answered, owned by them since before it was
    recorded ([[LH-588]]), with the credential they write it with, ``undo`` taking the declaration and its grants back
    when the vend fails (:func:`fga_deps.undo_then_take_back`: the table first, its grants once it is gone). ``None``
    for a declaration answered again to its declarer, which a failed vend leaves as it stands.

    The credential lasts no longer than the declaration's fence, which the door wrote or renewed right before
    (:func:`table_claims.declaration_credential_seconds`)."""
    response = DeclareTableResponse(location=location)
    # The credential is part of the answer, so a vend that fails takes the declaration back with it, before
    # anything announces the table: a declared, owned table whose declarer was told it failed is one a retry
    # can only meet as AlreadyExists.
    if req.vend_credentials is not False and response.location:
        try:
            response.storage_options = await _vend_on_spec_door(
                ns,
                door="declare",
                segments=segments,
                table_uuid=table_uuid,
                location=response.location,
                settings=settings,
                so=so,
                vendor=vendor,
                token=token,
                client=client,
                asked=req.vend_credentials is True,
                at_most_seconds=table_claims.declaration_credential_seconds(),
            )
        except Exception:
            if undo is not None:
                await fga_deps.undo_then_take_back(client, settings, token, segments=segments, uuid=table_uuid, undo=undo, reason="vend_failed")
            raise
    response.managed_versioning = True
    return response


def _marker(
    announcer: Announcer,
    token: Caller | None,
    evidence: NameResolves,
    segments: list[str],
    *,
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
    evidence: NameResolves,
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
        object_id=fga.table_object(table_uuid),
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
    dataplane.refuse_a_branch_this_door_cannot_honour(branch, door="describe", remedy=f"{named_by}. {remedy}" if named_by else remedy)


@router.post("/{id}/describe", response_model_exclude_none=True)
async def describe_table(
    id: str,
    table: DoorTableDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    vendor: VendorDep,
    token: CurrentToken,
    client: FgaClientDep,
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
    (``lance_ray.utils``, which calls ``describe_table(DescribeTableRequest(id=...))`` and merges
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
        # A stock client refreshes the credential its declare answered through this door: a declaration's declarer is
        # vended one only after renewing the declaration, and for no longer than its fence (ADR 0227 §9, RedeclareRenews).
        try:
            at_most_seconds = await run_in_threadpool(
                partial(table_claims.renewed_for_its_declarer, table_claims.claim_store(settings), table.resolved, declarer=current_subject(token))
            )
        except ConcurrentModificationError:
            if vend_credentials is True:
                raise
            log.warning("spec_vend_skipped", extra={"door": "describe", "table": "/".join(segments), "reason": "declaration_changed"})
            return response
        response.storage_options = await _vend_on_spec_door(
            table.namespace,
            door="describe",
            segments=segments,
            table_uuid=table_uuid,
            location=response.location,
            settings=settings,
            so=so,
            vendor=vendor,
            token=token,
            client=client,
            asked=vend_credentials is True,
            at_most_seconds=at_most_seconds,
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
    (``dataplane.holds_a_version``). ``is_only_declared`` is answered when ``check_declared`` or ``load_detailed_metadata``
    asks for it, as the spec says (`lance_docs/ns_catalog/spec.yaml:2926-2934`).
    """
    segments = table.segments
    try:
        if tag is not None:
            ref = dataplane.tag_reference(table.open_unchecked(so), tag)
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
            response.is_only_declared = not dataplane.holds_a_version(response.location, so)
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
    ([[LH-529]]). Only an open that finds no dataset asks the storage itself (``dataplane.holds_a_version``), which tells
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
            if dataplane.holds_a_version(str(response.location), so):
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
    the table's file format (`lakehouse.features.DATA_STORAGE_VERSION_KEY`, `STABLE_ROW_IDS_KEY`): ``metadata`` is the
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
    response.metadata = {**dataplane.user_schema_metadata(dataset.schema), **table_file_format(dataset).as_metadata()}


#: The spec gives `exists` a 200 with NO `content:` block — *"Success, no content"* — so a conforming
#: server sends an empty body. A handler annotated `-> None` sends `application/json` with the four
#: bytes `null`, which a strict client reads as a JSON document where the contract promised none.
#: `response_class=Response` is what makes FastAPI emit nothing; `status_code=200` alone does not.
@router.post("/{id}/exists", status_code=200, response_class=Response)
def table_exists(id: str, table: DoorTableDep, so: StorageOptionsDep, body: TableExistsRequest | None = None) -> Response:
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


@router.post("/{id}/drop", response_model_exclude_none=True)
async def drop_table(
    id: str,
    keyed: idem.KeyedDoorDep,
    ns: NamespaceDep,
    table: ResolvedTableDep,
    settings: SettingsDep,
    client: FgaClientDep,
    uuids: ObjectUuidsDep,
    announcer: AnnouncerDep,
    token: CurrentToken,
    so: StorageOptionsDep,
    force: RaskFlag = False,
    purge: RaskFlag = False,
) -> DropTableResponse:
    """Drop the table at ``id`` via ``drop_table``, then revoke its FGA tuples and
    emit a best-effort ``drop_table`` lineage event.

    Deletion protection (#73, the warehouse door's Decision-5 contract extended to the rung where a
    drop deletes BYTES): a ``protected`` control-root record refuses 409 unless ``force=true``, and
    ``force`` turns the protection lock ONLY — the FGA gate ran before this handler, identically
    with or without it.

    An ``Idempotency-Key`` makes a resent drop converge on the first attempt's answer; the same key with another body or
    other query parameters is refused 400 (code 13), and a key bound to another object or door is refused alike."""
    # SAME AMPLIFIER AS create (see `catalog.api.idempotency`): a replay re-enters a door that
    # DELETES BYTES or files a trash record. OPTIONAL, because the spec defines no such header and a stock client must keep working.
    converge = await keyed.begin(endpoint="POST /v1/table/{id}/drop", target=fga.canonical_object_id(parse_identifier(id)))
    if converge.replay is not None:
        return DropTableResponse.model_validate(converge.replay.body)

    segments = parse_identifier(id)
    canonical = fga.canonical_object_id(segments)
    await fga_deps.require_table_unprotected(settings, table, force=force)
    table_uuid = table.require_uuid()
    # What the drop's announcements name the table by, read while it still stands (ADR 0173 §10).
    known = await uuids.table(segments, uuid=table.uuid)
    # #75 the drop→undrop path. With a grace period configured, a drop DEREGISTERS (detaches the
    # pointer; the bytes stay exactly where they are) and files a trash record naming the location and
    # the deadline. This is what makes a fat-fingered drop survivable: time-travel cannot help here,
    # because `restore_table` rewinds a LIVE table and a real drop leaves no version to rewind to.
    # `purge=true` is the explicit opt-out — a caller who means "destroy the bytes now" says so. The grace period is the
    # table's warehouse's delete profile, else the estate's (ADR 0146).
    grace = 0.0 if purge else await trash_doors.drop_grace_days(settings, segments)
    trashed = grace > 0
    # What the drop announces is filed before anything changes, so a door failing after the drop still announces it
    # ([[LH-576]]): it landed when the name no longer resolves to this table.
    gone = NameResolves(segments=segments, uuid=table_uuid, held=False)
    dropped = _marker(announcer, token, gone, segments, operation=DROP_TABLE, uuids=known)
    dropped_event = _table_event(token, gone, action="table_dropped", table_uuid=table_uuid, name=canonical, extra={"recoverable": trashed}, named_by=known)
    async with announcer.announcing(door="drop_table", target=fga.table_object(table_uuid), announcements=[dropped, dropped_event]) as filing:
        response = await _drop(ns, settings, table, segments=segments, table_uuid=table_uuid, known=known, grace=grace, token=token, so=so, force=force)
        # Record the drop as lineage — provenance of the deletion (the dataset node persists in the graph, named a
        # `drop_table` run). Sent BEFORE the revoke: on the http transport the caller's bearer authorizes ingest against
        # their (still-live) write grant — revoking first would 403 the very event that records who dropped the table.
        await filing.send(dropped)
        # Revoke the table's FGA tuples so a later table reusing this id can't inherit stale grants — but
        # ONLY on a destructive drop. A RECOVERABLE drop (#75) leaves an object that still exists and whose
        # owner is the one person who needs to undrop it; revoking here made undrop unreachable for exactly
        # that caller (found by driving the deployed catalog, not by a unit test — the unit tests run FGA
        # off). The grants die with the bytes instead: at purge, or when the sweep's expiry reclaims it.
        if not trashed:
            await fga_deps.revoke_ownership(client, settings, obj=fga.table_object(table_uuid), token=token, origin="lifecycle_delete")
        await filing.send(dropped_event)
    await converge.remember(200, response)
    return response


async def _drop(
    ns: LanceNamespace,
    settings: Settings,
    table: ResolvedTable,
    *,
    segments: list[str],
    table_uuid: str,
    known: DatasetUuids,
    grace: float,
    token: Caller | None,
    so: StorageOptions,
    force: bool,
) -> DropTableResponse:
    """Drop the table ``table`` resolved to: into the trash under a grace period, else its bytes, under the fence that
    holds the claim its resolution read."""
    canonical = fga.canonical_object_id(segments)
    trashed = grace > 0
    # THE DOOR ACTS ON THE TABLE ITS NAME RESOLVED TO (ADR 0173 §7, [[LH-521]]): the fence holds the claim that
    # resolution read, so a rename landing since answers this drop 409, and one landing after the fence answers 409
    # itself, rather than the drop reaching whatever table holds the name by then. The drop deletes the LOCATION, so it
    # also refuses while the claim names another table that still resolves to it ([[LH-204]]).
    claims = table_claims.claim_store(settings)
    fenced = await run_in_threadpool(partial(table_claims.fence, ns, claims, table, holder=canonical, verb="drop"))
    location = fenced.location
    registry = base_registry.BaseRegistry(control_root=settings.registry_root, storage_options=settings.storage_options())
    ending: CloneEnding | None = None
    try:
        # PROTECTION IS READ AGAIN UNDER THE FENCE (ADR 0227): the protection door writes its record and then asks
        # whether a request fences the claim (`table_claims.require_unfenced`), and this drop wrote its fence before this
        # read, so a protection answered 200 is one this read sees. The door's first read refuses early.
        await fga_deps.require_table_unprotected(settings, table, force=force)
        if trashed:
            # RECORD FIRST, DETACH SECOND (diff2 F6 leg a). This was deregister-then-record, and a
            # crash in that window produced the worst state in the plane: the manifest row was gone,
            # no trash record existed, and the bytes sat on storage unreachable by everything.
            # `undrop` 404s (no record), the purge never sees it (it enumerates records), and the
            # obvious retry cannot converge either — `describe_table` 404s, so the door can never reach
            # the write again. Only a hand-run `register_table` recovers it.
            #
            # Reversed, the crash window leaves a record on a LIVE table, which every consumer already
            # handles: the purge refuses it `STILL_REGISTERED` (`live_ids` is checked before anything
            # destructive), the drop retry re-describes cleanly and overwrites the same hashed key,
            # and `undrop` converges on it now that the register tolerates `TableAlreadyExists`. The
            # residue is bounded by one retry; the old one was permanent.
            #
            # Every record this branch writes owns bytes, which is why it is sound HERE and why the cascade
            # path is not reversed with it: a byte-LESS record (a declared-only table, a namespace row) skips
            # the purge's estate test, so a record filed on a still-live object inside a DEACTIVATED
            # warehouse would be revoked and cleared.
            #
            # Known interaction, deliberate: while the record stands on a live table the maintenance
            # sweep excludes that table (F6 leg d). Bounded by the same retry, and the alternative is
            # losing the data outright.
            record = trash.make_record(
                canonical,
                uuid=table_uuid,
                location=location,
                dropped_by=f"user:{token.subject}" if token is not None else None,
                grace_days=grace,
                namespace_uuid=known.namespace_uuid,
            )
            await run_in_threadpool(trash.put, settings.registry_root, settings.storage_options(), record)
            await run_in_threadpool(native.call, ns, "deregister_table", DeregisterTableRequest(id=segments))
            # The bytes are deleted later, by the purge, so the spec's answer is a transaction id to track the deletion
            # by (`spec.yaml` DropTableRequest): `<table id>$<its uuid>`, which DescribeTransaction answers from the record.
            response = DropTableResponse(transaction_id=fga.canonical_object_id([*segments, table_uuid]))
        else:
            # [[LH-532]] The bytes go at the location this door resolved, at the key Lance opens, never where the
            # backend's own drop looks: the backend removes only the row. RECORD FIRST here too: a residue record is filed
            # before the row goes and cleared once the bytes are, so bytes a failed delete leaves keep their claim. A clone's
            # ending is filed once every judgement has passed, right before the row goes (ADR 0198 §1), so a refusal files
            # none: its tags are released after the row is gone, by this door or, when that fails, by maintenance's audit.
            filed: list[CloneEnding] = []

            def _file_the_ending() -> None:
                if (
                    found := clone_endings.file_ending(registry, claims, location, table=canonical, forget_record=True, door="drop", filing=fenced.token)
                ) is not None:
                    filed.append(found)

            response = await run_in_threadpool(
                partial(
                    dataplane.destroy_table,
                    ns,
                    so,
                    segments,
                    location,
                    estate_roots=(settings.registry_root, settings.root),
                    table_uuid=table_uuid,
                    namespace_uuid=known.namespace_uuid,
                    control=claims,
                    dropped_by=f"user:{token.subject}" if token is not None else None,
                    before_removal=_file_the_ending,
                )
            )
            ending = filed[0] if filed else None
    except dataplane.TableBytesLeftError:
        # The row is gone and bytes remain: the claim stays held, kept by the residue record once the fence lapses, and the
        # grants stay. The trash purge reclaims the objects; the drop's announcements wait in the door's filing, which the
        # finish sends, since the name no longer resolves to the table ([[LH-576]]).
        raise
    except Exception:
        # An ending filed before a failure stays: a describe cannot tell whether the row the failure followed is still
        # the clone's, and the finish judges it on its own reads once the claim's lease has passed.
        await run_in_threadpool(table_claims.lift_fence, claims, fenced)
        raise
    if trashed:
        # [[LH-204]] The trashed id keeps its location's claim until undrop or purge, so no other id registers over
        # bytes someone can still restore.
        await run_in_threadpool(table_claims.lift_fence, claims, fenced)
    else:
        # [[LH-279]] The base record goes with the bytes: left behind, it would sanction its bases for the next
        # table registered at this location. The location's claim goes too ([[LH-204]]), and the clone's ending last.
        await run_in_threadpool(partial(base_registry.forget_base_record, registry, location, uuid=table_uuid))
        # The condemned versions are the bytes' (ADR 0246 §16), and the bytes are gone; ended before the claim, so no
        # table registered at the location afterwards reads a record of versions it does not hold.
        await run_in_threadpool(condemned_versions.end, settings.registry_root, settings.storage_options(), location)
        await run_in_threadpool(partial(location_claims.release, claims, location, canonical, uuid=table_uuid))
        if ending is not None:
            await run_in_threadpool(clone_endings.clear_ending, claims, ending)
    # What was kept for the table under its uuid ends with it ([[LH-523]]); a table created later at this name holds
    # another uuid, so nothing left here could govern it. A recoverable drop keeps its maintenance policy for the undrop,
    # as it keeps its grants, and the trash purge ends both. Its protection goes now, because the drop it guarded has
    # happened.
    if not trashed:
        await run_in_threadpool(object_records.end_object_records, settings.registry_root, settings.storage_options(), "table", table_uuid)
    else:
        await run_in_threadpool(protection.clear_protection, settings.registry_root, settings.storage_options(), "table", table_uuid)
    return response


@router.post("/{id}/deregister", response_model_exclude_none=True)
async def deregister_table(
    id: str,
    keyed: idem.KeyedDoorDep,
    ns: NamespaceDep,
    table: ResolvedTableDep,
    settings: SettingsDep,
    client: FgaClientDep,
    uuids: ObjectUuidsDep,
    control: ControlEmitterDep,
    announcer: AnnouncerDep,
    token: CurrentToken,
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
    door failing after it still announces it ([[LH-576]]).

    An ``Idempotency-Key`` makes a resent deregister converge on the first attempt's answer; the same key with another
    body or other query parameters is refused 400 (code 13), and a key bound to another object or door is refused alike."""
    # SAME AMPLIFIER AS create (see `catalog.api.idempotency`): a replay re-enters a door that
    # retires the id while the bytes stay. OPTIONAL, because the spec defines no such header and a stock client must keep working.
    converge = await keyed.begin(endpoint="POST /v1/table/{id}/deregister", target=fga.canonical_object_id(parse_identifier(id)))
    if converge.replay is not None:
        return DeregisterTableResponse.model_validate(converge.replay.body)

    segments = reconcile_body_id(parse_identifier(id), body.id if body else None)
    canonical = fga.canonical_object_id(segments)
    if table.location is not None and table.uuid is None:
        response = await _deregister_unidentified(ns, settings, table, control, token, segments=segments)
        await converge.remember(200, response)
        return response
    await fga_deps.require_table_unprotected(settings, table, force=force)
    table_uuid = table.require_uuid()
    known = await uuids.table(segments, uuid=table.uuid)
    gone = NameResolves(segments=segments, uuid=table_uuid, held=False)
    detached = _marker(announcer, token, gone, segments, operation=DEREGISTER_TABLE, uuids=known)
    detached_event = _table_event(token, gone, action="table_deregistered", table_uuid=table_uuid, name=canonical, extra={}, named_by=known)
    async with announcer.announcing(door="deregister_table", target=fga.table_object(table_uuid), announcements=[detached, detached_event]) as filing:
        # The table leaves governance, so what was kept for it under its uuid goes, ended by the detach under the
        # table's history fence. Its condemned versions are its bytes', which stay: the next registration at the
        # location reads them (ADR 0246 §16).
        response = await _detach(ns, settings, table, segments=segments)
        # Record the detach as lineage — asymmetric with drop (which deletes data), deregister only detaches, so without
        # this marker the feed reads it as a still-live, never-touched table. Versionless (no data was written), sent
        # BEFORE the revoke: on the http transport the caller's bearer authorizes ingest against their (still-live) write
        # grant — revoking first would 403 the very marker this endpoint exists to record.
        await filing.send(detached)
        await fga_deps.revoke_ownership(client, settings, obj=fga.table_object(table_uuid), token=token, origin="lifecycle_delete")
        await filing.send(detached_event)
    await converge.remember(200, response)
    return response


async def _detach(ns: LanceNamespace, settings: Settings, table: ResolvedTable, *, segments: list[str]) -> DeregisterTableResponse:
    """Remove ``table``'s row from ``__manifest`` and release its location's claim; the bytes stay where they are.

    The door acts on the table its name resolved to, under the fence that holds the claim it read (ADR 0173 §7,
    [[LH-521]]): a rename landing since answers this deregister 409, as one landing after the fence answers itself.

    An identified table is detached under its history fence held alone (``HistoryWork.DETACH``, ADR 0246 §17), and the
    records kept under its uuid, that fence among them, are ended under it: the fence is keyed by the uuid, so a detach
    beside an erasure's release or a ref write would end that work's hold, and a registration made again at the location
    would take a fence of its own that excludes nothing of it. Such a detach answers 409 (code 14), nothing detached.
    """
    canonical = fga.canonical_object_id(segments)
    claims = table_claims.claim_store(settings)
    fenced = await run_in_threadpool(partial(table_claims.fence, ns, claims, table, holder=canonical, verb="deregister"))
    history = (
        None
        if table.uuid is None
        else history_fences.TableHistory(control_root=settings.registry_root, storage_options=settings.storage_options(), table_uuid=table.uuid)
    )
    try:
        hold = None if history is None else await run_in_threadpool(history.taking, history_fences.HistoryWork.DETACH)
    except history_fences.HistoryFencedError as exc:
        await run_in_threadpool(table_claims.lift_fence, claims, fenced)
        raise ConcurrentModificationError(f"{exc}; the table was not deregistered, retry once that work is done") from exc
    try:
        return await _detach_fenced(
            ns,
            settings,
            table,
            claims,
            fenced,
            segments=segments,
            held=None if history is None or hold is None else Hold(history, hold.token, history_fences.HistoryWork.DETACH),
        )
    finally:
        if history is not None and hold is not None:
            await run_in_threadpool(history.release, hold.token)


async def _detach_fenced(
    ns: LanceNamespace,
    settings: Settings,
    table: ResolvedTable,
    claims: location_claims.ClaimStore,
    fenced: table_claims.DoorFence,
    *,
    segments: list[str],
    held: Hold | None,
) -> DeregisterTableResponse:
    """:func:`_detach` once its fences are held: ``held`` the table's history fence, confirmed right before the row goes,
    so a detach that stalled past its hold detaches nothing once other work may hold it."""
    canonical = fga.canonical_object_id(segments)
    registry = base_registry.BaseRegistry(control_root=settings.registry_root, storage_options=settings.storage_options())
    try:
        if held is not None:
            try:
                await run_in_threadpool(held.confirm)
            except history_fences.HistoryFencedError as exc:
                raise ConcurrentModificationError(f"{exc}; the table was not deregistered") from exc
        # A clone's ending is filed before its row goes (ADR 0198 §1), so a tag release that fails below is finished by
        # maintenance's audit rather than left for good: the row it would be re-issued against is gone by then. One filed
        # before a failed native call stays too: a describe cannot tell whether the row the failure followed is still the
        # clone's, and the finish judges it on its own reads once the claim's lease has passed.
        ending = await run_in_threadpool(
            partial(clone_endings.file_ending, registry, claims, fenced.location, table=canonical, forget_record=False, door="deregister", filing=fenced.token)
        )
        response: DeregisterTableResponse = await run_in_threadpool(native.call, ns, "deregister_table", DeregisterTableRequest(id=segments))
    except Exception:
        await run_in_threadpool(table_claims.lift_fence, claims, fenced)
        raise
    # The detached bytes are nobody's now, so the version a clone among them was promised on its sources goes (ADR 0198
    # §1), and then the id's claim on them ([[LH-204]]): the base record stays, for the bytes it describes. The tags go
    # first, while the fence still holds the location, so no clone registered there meanwhile can hold a tag of the same
    # name, and a tag that cannot be deleted leaves the location held rather than free with the tag standing.
    try:
        await run_in_threadpool(partial(base_registry.release_clone_sources, registry, fenced.location, uuid=table.uuid))
    except (OSError, ValueError, base_registry.UnreadableBaseRecordError) as exc:
        log.error("deregister_clone_tags_kept", extra={"table": canonical, "location": fenced.location, "error": f"{type(exc).__name__}: {exc}"[:300]})
        raise PartiallyApplied(
            f"table {canonical} was detached, and its tag on a table it clones could not be deleted: its location stays held, and "
            "maintenance's hourly audit releases the tag and the location from the record of the clone's end this door filed",
            problem_extra={"table": canonical, "detached": True},
        ) from exc
    await run_in_threadpool(location_claims.release, claims, fenced.location, canonical)
    if ending is not None:
        await run_in_threadpool(clone_endings.clear_ending, claims, ending)
    if held is not None:
        await run_in_threadpool(object_records.end_object_records, settings.registry_root, settings.storage_options(), "table", table.require_uuid())
    return response


async def _deregister_unidentified(
    ns: LanceNamespace, settings: Settings, table: ResolvedTable, control: ControlEmitter, token: Caller | None, *, segments: list[str]
) -> DeregisterTableResponse:
    """Detach a table whose location carries no claim recording a uuid, and announce it ([[LH-534]]).

    Its control event names it by name (``table:<name>``), because no uuid object exists to name it by.
    """
    canonical = fga.canonical_object_id(segments)
    response = await _detach(ns, settings, table, segments=segments)
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


def _registration_points_at(registered: str, claimed: str) -> bool:
    """Whether an existing ABSOLUTE registration is the location a register body CLAIMS.

    The claim is relative (`8f3a_ns$t` or `t.lance`), the registration absolute
    (`s3://bucket/8f3a_ns$t`), so this asks whether the claim is the registration's TAIL on a path
    BOUNDARY. A bare `endswith` would accept `ns$t` against `s3://b/other_ns$t`, which is a different
    table — and the whole point of the check is that a 409 at a different location is a genuine id
    conflict this must not converge.
    """
    left, right = registered.rstrip("/"), claimed.rstrip("/")
    return left == right or left.endswith("/" + right)


def absolute_table_location(ns: LanceNamespace, segments: list[str], registered: str | None) -> str | None:
    """The table's ABSOLUTE location, falling back to what the register returned.

    `register_table` ECHOES the caller's own path: measured 2026-09-11 on a `dir` namespace, registering
    `location="t.lance"` answers `response.location == 't.lance'` while `describe_table` on the same
    table answers `/<root>/t.lance`. Emitting the echo puts a relative URI on the CREATED edge and on the
    control event's `location` extra.

    WHAT THAT COSTS IS A LOCATION NO READER CAN OPEN: the `source_uri` rides the event into the feed
    and on to a forwarded graph backend as the dataset's dataSource, and a relative path there names
    nothing outside the caller's own working directory. Nothing re-runs to correct it, because the feed
    keeps the event it was given.

    THE BACKEND RESOLVES IT AGAINST THE RIGHT ROOT, which is why this asks rather than joining against a
    configured one: a table belongs to its warehouse's root, and a caller-side join would
    be wrong for exactly the tenant tables that matter.

    NEVER FAILS THE REGISTER. The registration is already committed, so an unreachable describe or an
    empty answer leaves the registered value — no worse than before, where raising would turn a
    successful register into a 500 the caller cannot retry into a better state. Neither caller judges
    with it: the 409 branch compares the resolved location against the body's, and a fresh registration
    keys its location claim by it ([[LH-204]]).
    """
    try:
        described = native.call(ns, "describe_table", DescribeTableRequest(id=segments))
    except Exception as exc:  # noqa: BLE001 — any failure degrades to the registered value, never to a 500
        log.warning("register_location_unresolved", extra={"table": segments, "error": str(exc)})
        return registered
    return getattr(described, "location", None) or registered


def _require_a_maintain_policy_that_fits(settings: Settings, *, location: str) -> None:
    """Refuse registering a dataset whose table's maintain-tier vend could never be minted (ADR 0243 R6): its location and
    the bases its manifest declares, judged as :func:`catalog.core.vending.require_a_table_maintain_policy_that_fits` does,
    the table's committed-file records named as a pass's vend names them.

    Asked after the caller's authorization (a clone's sources), and only of a dataset on S3, where a vend is an STS
    session: a dataset it cannot read is left to the door's own refusal of it, which names what is wrong.

    Raises:
        InvalidInputError: The policy would be past what STS takes (400, code 13).
    """
    if not location.startswith("s3://"):
        return
    try:
        facts = dataset_facts(location, settings.storage_options())
    except (LanceNamespaceError, OSError, ValueError):
        return
    records = TableRecords(control_root=settings.registry_root, table_uuid=str(uuid4()))
    require_a_table_maintain_policy_that_fits(location, bases=facts.base_uris, records=records, kms_key=settings.s3_sse_kms_key_id)


@router.post("/{id}/register", response_model_exclude_none=True)
async def register_table(
    id: str,
    keyed: idem.KeyedDoorDep,
    body: RegisterTableRequest,
    ns: NamespaceDep,
    root: NamespaceRootDep,
    settings: SettingsDep,
    token: CurrentToken,
    so: StorageOptionsDep,
    client: FgaClientDep,
    uuids: ObjectUuidsDep,
    announcer: AnnouncerDep,
    resolver: ResolverDep,
) -> RegisteredTable:
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
    registration ([[LH-576]]).

    An ``Idempotency-Key`` makes a resent registration converge on the first attempt's answer; the same key with another
    body or other query parameters is refused 400 (code 13), and a key bound to another object or door is refused alike.
    The backend records the registration only while the key is still this attempt's, so an attempt a resend took the key
    over from while it ran answers 409 (code 14) and attaches nothing (ADR 0240)."""
    # `mode` WAS ACCEPTED AND NEVER READ. The generated model states two: "Create (default): the
    # operation fails with 409. Overwrite: the existing table registration is replaced with the new
    # registration." This door passes `body` straight to the backend, and the backend ignores the
    # field — measured 2026-09-13 on the namespace twin, where every mode raised the same conflict —
    # so a caller asking to REPLACE a registration got the 409 that means "it already exists".
    #
    # Replacing a registration is not implemented here, and refusing says which of the two it is. It
    # is also not a cosmetic difference: `register_table` ATTACHES bytes the catalog does not own, so
    # silently treating "replace" as "conflict" leaves the caller believing their new location was
    # rejected as a duplicate rather than never attempted.
    #
    # A value outside those two is refused by the parse itself, as InvalidInput naming it — `ExistOk`
    # included, which the spec gives `create` and not this door (`RegisterMode`).
    #
    # BEFORE `idem.begin`, deliberately: this is a SHAPE refusal, and minting an idempotency record
    # for a request the door was never going to attempt would make the replay of a malformed call
    # converge on nothing.
    # Declined as the spec's Unsupported (406, code 0), which RegisterTable lists: the mode is well formed, and this
    # catalog does not serve it.
    if RegisterMode.parse(body.mode) is RegisterMode.OVERWRITE:
        raise UnsupportedOperationError(
            "mode 'Overwrite' is not supported on this door: replacing a registration would detach bytes this "
            "catalog does not own from the table that currently points at them. Deregister the existing table "
            "first, then register the new location."
        )
    # LANCE-ONLY (2026-08-15 ruling), the platform's keys and every other property, as at `declare_table`. Shape refusals
    # like the mode's, so before `idem.begin` for the same reason.
    reject_unsupported_properties(body.properties)
    provenance_guard.refuse_reserved_keys(body.properties or {}, door="register properties")
    refuse_properties_kept_nowhere(body.properties, door="register")
    # LOCATION-EXCLUSIVITY, THE SHAPE HALF ([[LH-279]]) — a shape refusal like the mode's, so before
    # `idem.begin` for the same reason. What the path alone decides is refused before anything is
    # attached: the namespace root, a control prefix, the control root and every configured base.
    context = table_bases.RegistrationContext(
        root=root,
        registry=base_registry.BaseRegistry(control_root=settings.registry_root, storage_options=settings.storage_options()),
        configured=settings.external_blob_base_list,
        data_allowlist=settings.multibase_data_base_list,
        governed=GovernedStorage.from_settings(settings),
        holder=fga.canonical_object_id(parse_identifier(id)),
    )
    body.location = table_bases.require_registrable_location(
        body.location,
        root=root,
        control_root=settings.registry_root,
        configured=[*context.configured, *context.data_allowlist],
    )
    # SAME AMPLIFIER AS create (see `catalog.api.idempotency`): a replay re-enters a door that
    # MINTS an id and seeds its ownership. OPTIONAL, because the spec defines no such header and a stock client must keep working.
    converge = await keyed.begin(endpoint="POST /v1/table/{id}/register", target=fga.canonical_object_id(parse_identifier(id)))
    if converge.replay is not None:
        return RegisteredTable.model_validate(converge.replay.body)

    segments = parse_identifier(id)
    await fga_deps.require_parent_exists(ns, "table", segments)
    body.id = reconcile_body_id(segments, body.id)
    # A shallow clone reads its source's files, and registering it tags the source (ADR 0198 §1), so its caller must be
    # one who may tag each source, asked BEFORE anything is attached: a refused caller leaves no row, mark or claim behind.
    # A location another standing table holds is refused first, so the dataset there is never read for a caller it is
    # not theirs. One this table's own name holds, with the table standing, is a registration made again: the backend
    # answers it TableAlreadyExists below (the spec's 409), and the dataset is not read for what it clones on the way
    # there. A claim whose holder no longer resolves to the location is nobody's: the registration goes on, and the
    # claim's own take decides whether its lease has lapsed.
    stated_claim = await run_in_threadpool(location_claims.holder, table_claims.claim_store(settings), f"{root.rstrip('/')}/{body.location}")
    held_by_a_standing_table = stated_claim is not None and not await run_in_threadpool(
        table_claims.holder_is_gone, ns, table_claims.claim_store(settings), stated_claim
    )
    if stated_claim is not None and held_by_a_standing_table and stated_claim.table != fga.canonical_object_id(segments):
        log.warning("register_refused_held_location", extra={"table": id, "location": stated_claim.location, "holder": stated_claim.table})
        raise table_bases.occupied_location_refusal(body.location)
    registered_already = held_by_a_standing_table
    asked: set[str] = set()
    if not registered_already:
        asked = await _require_clone_sources(client, settings, token, location=f"{root.rstrip('/')}/{body.location}", so=so, context=context)
        await run_in_threadpool(_require_a_maintain_policy_that_fits, settings, location=f"{root.rstrip('/')}/{body.location}")
    # A new uuid for this table ([[LH-519]]), even where an earlier table's claim was released, and its grants under it,
    # written before the backend records the table so it never stands without them ([[LH-588]]). A request that
    # registers nothing takes them back: a refused registration changes nobody's access ([[LH-586]]).
    table_uuid = str(uuid4())
    await fga_deps.seed_before_write(client, settings, token, resolver, segments=segments, uuid=table_uuid)
    try:
        # The run rule the write judge read is read again under the namespace's add fence, held through the add (ADR 0244 R1).
        placed = write_judge.Write(op="register", obj=f"table:{fga.canonical_object_id(segments)}", rung=None, table=tuple(segments))
        async with write_judge.placing(placed, ns, control_root=settings.registry_root, storage_options=settings.storage_options()):
            # The registration lands only while the request's key is still this attempt's: one a resend took over while
            # this attempt ran would attach the dataset again after whatever the key's registration met since (ADR 0240).
            # Renewed once the fence is held and the rule read, so nothing slow stands between it and the add (ADR 0227 §8).
            await run_in_threadpool(dataplane.renew_held, converge.held)
            response: RegisterTableResponse = await run_in_threadpool(native.call, ns, "register_table", body)
    except TableAlreadyExistsError:
        await fga_deps.take_back_seed(client, settings, token, segments=segments, uuid=table_uuid, reason="not_registered")
        # A REFUSED REGISTRATION WRITES NO TUPLE ([[LH-586]], owner 2026-11-11). The spec gives this door two
        # modes — `Create` (409) and `Overwrite` — and no ExistOk, so the 409 is the answer, and a request that
        # is refused changes nobody's access: not an owner for its caller, and not the table's link to this
        # namespace either. A table a rename is moving, or left half-moved, is named here and inherits from the
        # namespace it entered, and an edge written here put every reader the move shut out back on it (measured
        # 2026-11-11). A table registered whose seed never ran carries no tuple at all and denies every
        # principal: the drift report lists it as ungoverned, and the estate admin writes its edges at the
        # tuples door.
        #
        # THE TABLE'S BASE RECORD STILL CONVERGES, AND ONLY WHEN THE LOCATION MATCHES. A 409 at a different
        # location is a genuine id conflict, a different table living there. Same location means this IS that
        # table, so its record is the one a fresh registration would have written.
        #
        # THE CLAIM IS CHECKED AS THE TAIL OF THE REGISTRATION, not by equality. The body carries a
        # RELATIVE location — the dir backend refuses an absolute one on this door — while the
        # registered one is absolute. Resolving both through `absolute_table_location` would be dead
        # code that looks live: it PREFERS the described location over its argument, so both sides come
        # back identical and the guard would admit every 409 regardless of where it pointed.
        registered = await run_in_threadpool(absolute_table_location, ns, segments, None)
        claimed = (body.location or "").strip("/")
        if registered and claimed and _registration_points_at(registered, claimed):
            # A refused register whose detach failed also lands here on retry; a flag-256 dataset's
            # record is never converged, and a dataset that cannot be read is not judged clean.
            try:
                facts = await run_in_threadpool(registered_dataset_facts, registered, so)
            except Exception as exc:
                log.warning("register_converge_unjudged", extra={"table": id, "location": registered, "error": str(exc)[:300]})
                raise
            if facts is not None and facts.mixed:
                log.warning("register_converge_refused_mixed_file_versions", extra={"table": id, "location": registered})
                raise
            if facts is not None and not facts.stable_row_ids:
                log.warning("register_converge_refused_unstable_row_ids", extra={"table": id, "location": registered})
                raise
            # THE RECORD CONVERGES ([[LH-279]]). A writer that registers its output on every run re-enters
            # this door each time, so a table registered before its record existed gets one here — judged
            # exactly as a fresh registration's bases are, and never for a base nothing sanctions.
            if facts is not None:
                # A registered clone's roots stand on the pins its record holds, which its tags keep, and only a registration
                # takes those tags (`table_bases.admit_registered_table`): a record naming a source whose version nothing
                # keeps is never written ([[LH-576]]). A clone left attached without its record is deregistered and
                # registered again.
                clone = await run_in_threadpool(partial(table_bases.clone_of, facts.clone, facts.bases, location=registered, context=context))
                if clone.sources:
                    log.warning("register_converge_refused_clone", extra={"table": id, "location": registered})
                    raise
                unsanctioned = False
                entries: list[base_registry.RecordedBase] = []
                try:
                    entries = await run_in_threadpool(
                        partial(
                            table_bases.entries_for_registration,
                            registered,
                            facts.bases,
                            registry=context.registry,
                            configured=context.configured,
                            data_allowlist=context.data_allowlist,
                            clone=clone,
                        )
                    )
                except InvalidInputError:
                    unsanctioned = True
                if unsanctioned:
                    log.warning("register_converge_refused_unrecorded_bases", extra={"table": id, "location": registered})
                    raise
                # [[LH-252]] A data directory another table holds is never converged into this one's record, and one is
                # claimed only for the uuid the location's claim records ([[LH-575]]).
                standing = await run_in_threadpool(location_claims.holder, table_claims.claim_store(settings), registered)
                if standing is None or standing.uuid is None:
                    log.warning("register_converge_refused_unidentified", extra={"table": id, "location": registered})
                    raise
                claimant = location_claims.DirectoryClaimant(uuid=standing.uuid, root=registered, table=context.holder, segments=segments)
                held = False
                try:
                    await run_in_threadpool(table_bases.claim_registered_directories, context.registry, entries, claimant)
                except InvalidInputError:
                    held = True
                if held:
                    raise
                await run_in_threadpool(base_registry.claim_bases, context.registry, registered, entries)
                log.info("register_converged_base_record", extra={"table": id, "location": registered})
        raise
    except Exception:
        await fga_deps.take_back_seed(client, settings, token, segments=segments, uuid=table_uuid, reason="not_registered")
        raise

    claims = table_claims.claim_store(settings)
    canonical = fga.canonical_object_id(segments)
    # The location as the backend RESOLVED it, the spelling every other door keys the claim by: a `%` in
    # the body is stored re-encoded, so the body's spelling can decode to another key.
    stated_location = f"{root.rstrip('/')}/{body.location}"
    claimed_location = await run_in_threadpool(absolute_table_location, ns, segments, stated_location) or stated_location

    async def _deregister() -> None:
        # DEREGISTER, never drop. Register ATTACHES bytes that already existed and are not ours to
        # destroy — the whole point of the door. Deregister is the exact inverse: it removes the
        # catalog object this request created and leaves the data exactly where it was found.
        await run_in_threadpool(native.call, ns, "deregister_table", DeregisterTableRequest(id=segments))

    async def _free_location() -> None:
        # The location's claim goes with the row; one another table holds is not this registration's to free.
        # The records kept under this request's uuid go last, once no table holds it.
        # Only the claim this registration took, under the uuid it minted: a take refused (another request's fence) took
        # none, and the claim standing there is another table's under the same name.
        await run_in_threadpool(partial(location_claims.release, claims, claimed_location, canonical, uuid=table_uuid))
        await run_in_threadpool(object_records.end_object_records, settings.registry_root, settings.storage_options(), "table", table_uuid)

    async def _detach() -> None:
        await _deregister()
        await _free_location()

    # AFTER THE REGISTER, because only the backend knows which root a relative location resolves
    # against and the overlap check must see this registration's own row; BEFORE ANY RECORD, so a refused
    # dataset gains no record entry, lineage node or event, and the grants written for it are taken back. The commit marks first, at
    # each ref's head ([[LH-550]]): every version the dataset holds now was committed past every door, and
    # the claim is what makes the table one the commit reconciler reads. Then the location's claim
    # ([[LH-204]]): a rename holding it, or a table a recoverable drop is keeping it for, refuses
    # this registration even when the manifest shows no other row at the location. A rename between its two names
    # holds the claim fenced ([[LH-586]]), and that refusal is a concurrent change (409, code 14): the location is
    # held by a table in flight, and the same registration is judged afresh once the rename has ended.
    try:
        await run_in_threadpool(partial(commit_reconcile.mark_registered_heads, settings, so, location=claimed_location, table_uuid=table_uuid))
        try:
            await run_in_threadpool(
                partial(table_claims.claim_new_table, ns, claims, claimed_location, table=canonical, segments=segments, table_uuid=table_uuid)
            )
        except location_claims.LocationFencedError as fenced:
            log.warning("register_refused_fenced_location", extra={"table": id, "location": fenced.claim.location, "holder": fenced.claim.table})
            raise ConcurrentModificationError(f"cannot register {id}: another request is acting on the table at its location; retry") from None
        except location_claims.LocationHeldError as held:
            log.warning("register_refused_held_location", extra={"table": id, "location": held.claim.location, "holder": held.claim.table})
            raise table_bases.occupied_location_refusal(body.location) from None
        # Holding the claim, so of this registration and the finish of an ending at the location, the second sees the first
        # (ADR 0198 §1): the clone that ended here may hold tags of the very names this one would take.
        await run_in_threadpool(clone_endings.refuse_registration_while_ending, claims, claimed_location)
        verdict = await run_in_threadpool(table_bases.judge_registered_table, ns, so, segments, body.location, context)
        # The sources judged on the registered dataset are the ones its caller was asked for: one that differs (the source
        # changed hands meanwhile) was asked of no one, and is refused as one the caller may not tag.
        if unasked := [source for source in verdict.sources if source.table_uuid not in asked]:
            log.warning("register_refused_unasked_clone_source", extra={"table": id, "sources": [source.table for source in unasked]})
            raise table_bases.refused_clone(unasked[0].location)
        admitted = await run_in_threadpool(
            partial(table_bases.admit_registered_table, so, segments=segments, verdict=verdict, clone_uuid=table_uuid, context=context)
        )
    except Exception as refusal:
        try:
            await _detach()
        except Exception as undo_exc:
            log.error("register_refusal_compensation_failed", extra={"table": id, "error": str(undo_exc)})
            raise PartiallyApplied(
                f"the dataset at {id} was refused ({refusal}) but its registration could not be detached",
                problem_extra={
                    "table": id,
                    "attached": True,
                    "refused": "refused" if isinstance(refusal, InvalidInputError) else "unjudged",
                    "remedy": "deregister it before retrying",
                },
            ) from undo_exc
        await fga_deps.take_back_seed(client, settings, token, segments=segments, uuid=table_uuid, reason="refused")
        raise
    location = verdict.location

    async def _undo_register() -> None:
        # The row first: while the table is attached its record must stay, because its manifest still declares those
        # bases. Then the entries, directories and clone tags this registration took, and the location's claim last,
        # as at every other end of a clone: a tag the store keeps leaves the location held, so no clone registered
        # there next takes a tag of the same name while this one stands.
        await _deregister()
        await run_in_threadpool(partial(table_bases.release_admitted, admitted, clone_location=location, so=so, context=context))
        await _free_location()

    async def _take_back_register() -> None:
        await fga_deps.undo_then_take_back(client, settings, token, segments=segments, uuid=table_uuid, undo=_undo_register, reason="not_announced")

    named = NameResolves(segments=segments, uuid=table_uuid, held=True)
    # RESOLVED, not echoed — `response.location` is the caller's own relative path, which names nothing a
    # reader of the feed can open; the flag judgement above already resolved it.
    # Versionless + source_uri=the attached location (register_table ∈ _CREATE_OPS, so the feed records it as a
    # create). The registered table already holds data at some version, which this marker does not name.
    registered = _marker(
        announcer, token, named, segments, operation=REGISTER_TABLE, uuids=await uuids.table(segments, location=location, uuid=table_uuid), source_uri=location
    )
    registered_event = _table_event(token, named, action="table_registered", table_uuid=table_uuid, name=canonical, extra={"location": location})
    async with _filed_or_undone(
        announcer, door="register_table", target=fga.table_object(table_uuid), announcements=[registered, registered_event], undo=_take_back_register
    ) as filing:
        # The grants written before the backend call, written again now that the location's claim records their uuid: the
        # drift repair revokes grants no claim records, and the claim is taken only after the backend resolved the
        # location, so a repair landing between the two would have left the table without them.
        try:
            await fga_deps.seed_ownership(client, settings, token, resolver, resource="table", segments=segments, uuid=table_uuid)
        except Exception:
            await fga_deps.undo_then_take_back(client, settings, token, segments=segments, uuid=table_uuid, undo=_undo_register, reason="grant_failed")
            raise
        # Admitted and owned: a clone's ending filed for the registration goes, its own end filing one from its record.
        await run_in_threadpool(partial(table_bases.settle_admitted, admitted, context=context))
        await filing.send(registered)
        await filing.send(registered_event)
    # Read after the registration landed (HoldWritesFirst, ADR 0246 §17): the record is the location's, so a batch of an
    # earlier registration past its policy read may still delete what this names, which a hold over the new name
    # therefore does not keep.
    condemned = await run_in_threadpool(
        partial(reclaims.condemned_beneath, settings.registry_root, settings.storage_options(), table_claims.claim_store(settings), location=location)
    )
    answered = RegisteredTable(**response.model_dump(exclude_none=True), **condemned.model_dump())
    await converge.remember(200, answered)
    return answered


async def _require_clone_sources(
    client: OpenFgaClient | None, settings: Settings, token: Caller | None, *, location: str, so: StorageOptions, context: table_bases.RegistrationContext
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
        ServiceUnavailableError: The dataset, a source's claim or a record could not be read (503).
    """
    clone = await run_in_threadpool(table_bases.clone_read, location, so, context)
    if clone is None:
        return set()
    for source in clone.sources:
        try:
            for relation in table_bases.CLONE_RELATIONS:
                await fga_deps.require_relation_on_a_path(
                    client, settings, token, relation=relation, obj=fga.table_object(source.table_uuid), path=source.location
                )
        except PermissionDeniedError:
            log.warning("register_refused_clone_source", extra={"location": location, "source": source.table})
            raise
    if clone.ungoverned is not None:
        log.warning("register_refused_ungoverned_clone_source", extra={"location": location, "source": clone.ungoverned})
        await fga_deps.require_relation_on_a_path(client, settings, token, relation=table_bases.CLONE_RELATIONS[0], obj=None, path=clone.ungoverned)
    return {source.table_uuid for source in clone.sources}


@management_router.get("/{id}/protection", response_model_exclude_none=True)
async def get_table_protection(id: str, table: ResolvedTableDep, settings: SettingsDep) -> ProtectionResponse:
    """Read the deletion-protection flag (#123). The SET door shipped a year of writes with NO read:
    an operator could not arm, disarm-check, or audit protection — they discovered it by eating a
    409. Owner-gated like the set door (the observer of a safety is whoever might trip it), and the
    record's `set_by` comes back so a refused drop can name who armed it."""
    canonical = fga.canonical_object_id(table.segments)
    record = await run_in_threadpool(protection.get_protection, settings.registry_root, settings.storage_options(), "table", table.require_uuid())
    return ProtectionResponse(id=canonical, protected=bool(record), set_by=(record or {}).get("set_by"))


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
    id: str,
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

    The undrop places the table back as a create places one (ADR 0244): under its namespace's add fence it reads that
    the namespace stands and is the one the table was dropped from, and registers it (``trash_doors.restore_kept_table``).
    A namespace dropped and made again since, perhaps under the run rule, answers 409 (code 19); one being dropped or made
    afresh meanwhile answers 409 (code 14); one that does not stand answers 404 (code 1).
    """
    segments = parse_identifier(id)
    canonical = fga.canonical_object_id(segments)
    trashed = await resolver.admitted_trash("table", segments)
    if not trashed:
        raise TableNotFoundError(
            f"no recoverable drop for table: {canonical}. Either it was never dropped recoverably "
            "(the grace period was off), or the purge has already reclaimed its bytes."
        )
    record = trash_doors.the_one_to_restore(trashed, kind="table", name=canonical)
    trash_doors.require_restorable(record, name=canonical)
    record_uuid = trash.object_uuid(record)
    if record_uuid is None:
        raise InternalError(f"the trash record of {canonical} keeps no uuid, so its grants cannot be found")
    standing = await resolver.table(segments, ns=ns)
    if standing.location is not None and standing.uuid != record_uuid:
        raise TableAlreadyExistsError(f"cannot undrop {canonical}: another table holds the name now; rename or drop it first")
    # The absolute URL the record kept, for an operator and for the announcement; the step re-attaches the table at the
    # relative key it decodes to ([[LH-517]]).
    location = str(record["location"])
    named = NameResolves(segments=segments, uuid=record_uuid, held=True)
    restored = _marker(
        announcer, token, named, segments, operation=REGISTER_TABLE, uuids=await uuids.table(segments, location=location, uuid=record_uuid), source_uri=location
    )
    restored_event = _table_event(token, named, action="table_undropped", table_uuid=record_uuid, name=canonical, extra={})
    async with announcer.announcing(door="undrop_table", target=fga.table_object(record_uuid), announcements=[restored, restored_event]) as filing:
        # NO SEED — undrop is a PURE RESTORE (owner ruling, diff2 F10 item 4b). The recoverable drop keeps the table's
        # tuples (#75 — the owner is the one person who needs to undrop it), so the original owner's grants are live when
        # the undrop runs; seeding the caller would only ADD whoever pressed the button as a second owner with no grant
        # ever having been made. A table that was ALREADY ownerless before the drop stays ownerless after the undrop,
        # rather than being papered over by granting the rescuer; surfacing stranded objects is the reconciler's job.
        #
        # THE TABLE EXISTS AGAIN, and lineage has to hear it: the drop stamped the dataset DROP, and without a CREATE fact
        # the reconcile would skip a live governed table for good ([[LH-144]]). The same versionless marker the register
        # door sends, at the absolute location the record kept, sent before the record is cleared, under the claim's
        # fence the step holds (ADR 0227).
        response = await trash_doors.restore_kept_table(ns, settings, record, segments=segments, root=root, before_clear=partial(filing.send, restored))
        await filing.send(restored_event)
    return response


@management_router.post("/{id}/protection", response_model_exclude_none=True)
async def set_table_protection(
    id: str,
    body: SetProtectionRequest,
    table: ResolvedTableDep,
    ns: NamespaceDep,
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
    canonical = fga.canonical_object_id(table.segments)
    table_uuid = table.require_uuid()
    so = settings.storage_options()
    if body.protected:
        record = {
            "kind": "table",
            "id": canonical,
            "uuid": table_uuid,
            "protected": "true",
            "set_by": f"user:{token.subject}" if token is not None else "anonymous",
        }
        await run_in_threadpool(protection.set_protection, settings.registry_root, so, record)
        # WRITTEN FIRST, JUDGED AFTER (ADR 0227): a drop writes its fence first and reads protection after, so of the two
        # one sees what the other wrote. A request fencing the claim now (a drop that may have read protection before
        # this write) or a claim changed since the resolution answers 409, and the record stays: it protects, and a drop
        # that has not read it yet refuses on it.
        await run_in_threadpool(table_claims.require_unfenced, ns, table_claims.claim_store(settings), table)
    else:
        await run_in_threadpool(protection.clear_protection, settings.registry_root, so, "table", table_uuid)
    await emit_control(
        control,
        action="table_protected" if body.protected else "table_unprotected",
        object_type="table",
        object_id=fga.table_object(table_uuid),
        object_name=canonical,
        actor=f"user:{token.subject}" if token is not None else None,
        extra={},
    )
    return ProtectionResponse(id=canonical, protected=body.protected)


@router.post("/{id}/rename", response_model_exclude_none=True)
async def rename_table(
    id: str,
    keyed: idem.KeyedDoorDep,
    body: RenameTableRequest,
    ns: NamespaceDep,
    table: ResolvedTableDep,
    so: StorageOptionsDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    uuids: ObjectUuidsDep,
    announcer: AnnouncerDep,
    resolver: ResolverDep,
    root: NamespaceRootDep,
    force: RaskFlag = False,
) -> RenamedTable:
    """Rename the table at ``id`` by moving its POINTER, and emit lineage; its grants stay on its uuid.

    Lance has no format-level rename (table rename is a namespace-layer remap, not a data-plane commit) and
    the ``dir`` backend's ``rename_table`` answers only Unsupported, which is also off-spec — the Namespace REST spec
    makes rename a first-class Metadata op. It is served here as what the spec describes: the destination id
    is registered at the SOURCE's existing location and the source id is deregistered. **No data byte is
    read, written or deleted**, so the cost is O(1) rather than the dataset's size — see
    ``dataplane.rename_table`` for the lance-ns V2 naming rule that makes this correct and for the one
    shape (a V1 root-namespace table) it refuses.

    Within its namespace the caller must hold ``can_drop`` on the source (owner tier, gated by the router
    ``authorize``) AND ``can_create_table`` on that namespace. The table keeps its uuid, so every record and every grant
    kept for it stays where it is ([[LH-523]], [[LH-524]]), and a rename into ANOTHER namespace therefore takes its grants
    along: it asks ``can_move`` on the source and ``can_accept_moved_table`` on the destination's namespace instead,
    which under managed access need grant authority on the table and on the destination ([[LH-568]]), else a writer
    could bring grants into a managed namespace and an owner inside one take a table, and the authority to grant on it,
    out. Into another namespace only where it inherits from moves (``fga_deps.parent_edge_mover``): its ``parent`` edge
    in one write before the name, its ``child`` edge taken from the source with it and written on the destination once
    the name is there, and both go back if the rename is undone. Its data directories' claims stay as they are: each
    records the table's uuid and location, which a rename changes neither of ([[LH-575]]). One versionless ``rename_table`` DatasetEvent announces it on the new id, its standard
    lifecycle state ``RENAME`` naming the old id as ``previousIdentifier``: one table under a new name, never a table dropped
    and another created ([[LH-554]]), filed before the rename so a door failing after it still announces it ([[LH-576]]).
    Source missing → 404 ``TableNotFound``; destination name taken → 409 ``TableAlreadyExists``; the location held by a
    concurrent rename or registration → 409 ``ConcurrentModification`` (code 14, [[LH-204]]).

    An ``Idempotency-Key`` makes a resent rename converge on the first attempt's answer; the same key with another body
    or other query parameters is refused 400 (code 13), and a key bound to another object or door is refused alike."""
    # SAME AMPLIFIER AS create (see `catalog.api.idempotency`): a replay re-enters a door that
    # retires the source id and re-registers the destination. OPTIONAL, because the spec defines no such header and a stock client must keep working.
    segments = parse_identifier(id)
    body.id = reconcile_body_id(segments, body.id)  # a contradictory body id is a 400, like every {id} route
    # Rename mints a new table identifier: ``new_namespace_id`` (else the source's own namespace) + ``new_table_name``.
    new_segments = rename_destination(segments, body.new_namespace_id, body.new_table_name)
    # The key binds the destination too: under it, a rename to another name is another operation ([[LH-576]]).
    converge = await keyed.begin(
        endpoint="POST /v1/table/{id}/rename", target=f"{fga.canonical_object_id(segments)} -> {fga.canonical_object_id(new_segments)}"
    )
    if converge.replay is not None:
        return RenamedTable.model_validate(converge.replay.body)

    # #73: a rename RETIRES the source id, so the source's protection gates it exactly like drop even
    # though no byte moves: every reader and pipeline addressing the table by its id loses it. `force` rides
    # the query string as on the sibling doors, and the protection travels with the table to its new name.
    # Checked FIRST — before the parent/create gates — so a protected source refuses identically regardless
    # of destination.
    await fga_deps.require_table_unprotected(settings, table, force=force)
    # A rename can MINT an orphan as surely as a create, so its destination's namespace must exist. Same rule, same
    # door — checked before the native call, so a refused rename leaves the source exactly where it was.
    fga_deps.require_rename_within_its_warehouse(segments, new_segments)
    await fga_deps.require_parent_exists(ns, "table", new_segments)
    # A rename mints a name in the destination's namespace, so the caller must be one who may put a table there, asked
    # BEFORE the (destructive, relocating) rename. (authorize already asked the source's rung.)
    await fga_deps.require_rename_destination(client, settings, token, resolver, source=segments, destination=new_segments)
    renamed = fga.table_object(table.require_uuid())
    # A rename moves no grant ([[LH-524]]): every grant is held by the table's uuid, which the rename keeps. Into
    # another namespace its parent and child edges move with it, and those are all it writes in OpenFGA.
    reparent = await fga_deps.parent_edge_mover(client, settings, token, resolver, obj=renamed, source=segments, destination=new_segments)
    # A rename keeps the table's uuid (ADR 0173 §2): the event and the control event name the one the door resolved, and
    # its location, which a rename keeps (`dataplane.rename_table`). Both are filed before the rename, which landed once
    # the destination's name resolves to the table ([[LH-576]]).
    destination_ids = await uuids.table(new_segments, uuid=table.uuid)
    named = NameResolves(segments=new_segments, uuid=table.require_uuid(), held=True)
    # Versionless: the version history moved with the pointer, and the event names none of it. The location rides it so
    # the table's symlink is the one its earlier events named.
    moved = _marker(announcer, token, named, new_segments, operation=RENAME_TABLE, uuids=destination_ids, source_uri=table.location, renamed_from=segments)
    moved_event = _table_event(
        token,
        named,
        action="table_renamed",
        table_uuid=table.require_uuid(),
        name=fga.canonical_object_id(new_segments),
        extra={"from": fga.canonical_object_id(segments), "to": fga.canonical_object_id(new_segments)},
        named_by=destination_ids,
    )
    # The run rule the write judge read is read again under the destination's add fence, held through the move (ADR 0244 R1).
    placed = write_judge.Write(op="rename", obj=f"table:{fga.canonical_object_id(segments)}", rung=None, table=tuple(new_segments), moved_from=tuple(segments))
    async with (
        write_judge.placing(placed, ns, control_root=settings.registry_root, storage_options=settings.storage_options()),
        announcer.announcing(door="rename_table", target=renamed, announcements=[moved, moved_event]) as filing,
    ):
        await run_in_threadpool(
            partial(
                dataplane.rename_table,
                ns,
                table,
                body.new_table_name,
                body.new_namespace_id,
                so=so,
                claims=table_claims.claim_store(settings),
                root=root,
                reparent=reparent,
            )
        )
        await filing.send(moved)
        await filing.send(moved_event)
    # Read after the rename landed (HoldWritesFirst, ADR 0246 §17): a batch past its policy read under the old name may
    # still delete what this names, which a hold over the destination therefore does not keep.
    condemned = await run_in_threadpool(
        partial(reclaims.condemned_beneath, settings.registry_root, settings.storage_options(), table_claims.claim_store(settings), location=table.location)
    )
    response = RenamedTable(**condemned.model_dump())
    await converge.remember(200, response)
    return response


@router.post("/{id}/restore", response_model_exclude_none=True)
async def restore_table(
    id: str,
    keyed: idem.KeyedDoorDep,
    body: RestoreTableRequest,
    table: DoorTableDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    uuids: ObjectUuidsDep,
    authorization: Annotated[str | None, Header()] = None,
) -> RestoreTableResponse:
    """Restore the table at ``id`` to a prior version via ``restore_table``; emits a RESTORE_TABLE event at
    the NEW current version (restore mints a fresh version pointing at the restored data).

    An ``Idempotency-Key`` makes a resent restore converge on the first attempt's answer; the same key with another body
    is refused 400 (code 13). The version the restore lands carries the key and is landed only while the key is still this
    attempt's, so an attempt a resend took the key over from while it ran answers 409 (code 14) and lands nothing, and a
    resend taking the key over from an attempt lost unanswered answers the restore it landed (ADR 0240); one whose search
    for it meets a version since removed (a cleanup, or ``version/delete``) is refused 409 (code 19) and writes nothing: the catalog cannot tell whether the write landed there (ADR 0240 §12)."""
    # SAME AMPLIFIER AS create (see `catalog.api.idempotency`): a replay re-enters a door that
    # REWINDS the table to an older version. OPTIONAL, because the spec defines no such header and a stock client must keep working.
    converge = await keyed.begin(endpoint="POST /v1/table/{id}/restore", target=fga.canonical_object_id(parse_identifier(id)))
    if converge.replay is not None:
        return RestoreTableResponse.model_validate(converge.replay.body)

    segments = parse_identifier(id)
    body.id = reconcile_body_id(segments, body.id)
    # THE BRANCH IS SERVED: `dataplane.restore` opens the ref named at the door's location, where a missing branch
    # answers 22 and a version the branch lacks answers 11, the codes `_open_ref` mints for every other door, and
    # restores through that handle, which commits on the ref it is on (measured on pylance 13.0.0: a branch restored
    # to an earlier version took a new version of its own, and main kept its version).
    # The event names the version THAT restore committed on the ref it restored, and the shared trailer reads
    # its schema off one pinned open (best-effort: a readback failure never fails the committed restore).
    landed = None
    if converge.held is not None and converge.held.written_from is not None:
        landed = await run_in_threadpool(
            lambda: dataplane.landed_under_key(
                table.open_unchecked(so, branch=body.branch), converge.held, table_location=table.location, branch=body.branch, so=so, replaces=True
            )
        )
    if landed is not None:
        # The restore a lapsed attempt under the key landed and never answered is the key's answer (ADR 0240).
        response, committed = RestoreTableResponse(transaction_id=landed[1].uuid), landed[0]
        await converge.remember(200, response)
        return response
    response, committed = await run_in_threadpool(
        partial(_restore_holding_history, table, so, body, transaction_properties=lineage_deps.door_stamp(token, RESTORE_TABLE))
    )
    # Recorded as soon as the restore has committed, as an append's answer is: a failure after this line answers the caller
    # an error, and its resend under the key is answered with this commit rather than restoring again.
    await converge.remember(200, response)
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        location=table.location,
        so=so,
        token=token,
        operation=RESTORE_TABLE,
        authorization=authorization,
        uuids=uuids,
        pin_version=committed,
        branch=body.branch,
    )
    return response


def _restore_holding_history(
    table: DoorTable, so: StorageOptions, body: RestoreTableRequest, *, transaction_properties: dict[str, str]
) -> tuple[RestoreTableResponse, int]:
    """:func:`dataplane.restore`, with the version it brings back pinned first, while the door holds the table's history as
    a ref write does (ADR 0246, ADR 0233).

    A restore brings back the files of the version it names, so it pins that version as a tag door does for as long as it
    runs: a reclaim that listed the table's versions before the restore committed would delete the files the restored
    head names (measured on pylance 13.0.0: 2 restores of 40 beside a cleanup at age 0 left a head no reader could read).
    The pin is written, then the table's condemned versions read (:func:`dataplane.require_whole`): a version a reclaim
    condemned, which may stand over deleted files, is refused, and one it did not is kept by every reclaim while the pin
    stands. A branch the table does not have is left to the restore's own refusal (code 22). The commit is made only once
    the hold is confirmed (:meth:`DoorTable.history_write`), which an erasure's release excludes.

    Raises:
        ConcurrentModificationError: An erasure's release holds the table's history, or the hold lapsed before the commit
            (code 14).
        InvalidTableStateError: The version is condemned (code 19).
        ServiceUnavailableError: The store did not answer whether the version is condemned (503).
    """
    branch = recorded_branch(body.branch)
    dataset = table.open_unchecked(so)
    with dataplane.pinning(dataset) as pins:

        def judge(_hold: Hold) -> None:
            if branch is None or branch in dataset.branches.list():
                pins.add(dataplane.require_whole(table, dataset, branch=branch, version=body.version, kind="restore", name=branch or MAIN_BRANCH))

        def restore() -> tuple[RestoreTableResponse, int]:
            pins.require_alive()
            return dataplane.restore(table, so, body, transaction_properties=transaction_properties)

        return table.history_write(HistoryWork.REFS, restore, judge=judge)


@router.post("/{id}/stats", response_model_exclude_none=True)
def get_table_stats(
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
    dataplane.refuse_a_branch_this_door_cannot_honour(branch, door="get_table_stats")
    # The request carries no version to pin, so a base planted between this judgement and the native
    # open is read unjudged; the window is the two opens apart ([[LH-279]]).
    table.open(so)
    stats = table.read_natively("get_table_stats", req)
    return stats
