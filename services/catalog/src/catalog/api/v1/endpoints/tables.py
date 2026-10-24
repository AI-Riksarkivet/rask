"""Table metadata + lifecycle endpoints (no data plane)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from functools import partial
from typing import Annotated

import lance
from fastapi import APIRouter, Header, Query, Request, Response
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
    GetTableTagVersionRequest,
    InternalError,
    InvalidInputError,
    InvalidTableStateError,
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
from openfga_sdk import OpenFgaClient

from catalog.api import fga_deps, lineage_deps, listings
from catalog.api import idempotency as idem
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
    namespace_for_top_ns,
)
from catalog.api.pagination import paginate
from catalog.api.rask_params import RaskFlag
from catalog.api.security import Caller, CurrentToken
from catalog.core import json_arrow, provenance_guard
from catalog.core.base_judge import BaseJudge, GovernedStorage
from catalog.core.config import Settings
from catalog.core.formats import reject_unsupported_properties
from catalog.core.identifiers import MAX_NAMESPACE_DEPTH, UUID_PROPERTY, parse_identifier, reconcile_body_id, rename_destination
from catalog.core.lineage_emit import (
    DECLARE_TABLE,
    DEREGISTER_TABLE,
    DROP_TABLE,
    REGISTER_TABLE,
    RESTORE_TABLE,
    InputPin,
    emit_write_event,
)
from catalog.core.modes import RegisterMode
from catalog.core.namespace import open_resolved_dataset_unchecked, registered_dataset_facts, warn_if_mixed_file_versions
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
from catalog.services import dataplane, native, table_bases, table_claims, trash_doors, warehouses
from catalog.services.table_claims import DoorTable, ResolvedTable
from service_kit.control_emit import ControlEmitter, emit_control
from service_kit.governed import fga
from service_kit.governed.audit import SUCCESS, audit
from service_kit.governed.deps import ANONYMOUS_SUBJECT
from service_kit.lakehouse import base_registry, location_claims, object_records, protection, trash
from service_kit.lakehouse.naming import uuid_of
from service_kit.lakehouse.ns_errors import PartiallyApplied


log = logging.getLogger(__name__)
#: Ceiling for the spec list ops' `limit`. The Lance Namespace spec pages these with
#: `page_token`, so a server answering fewer rows than asked and handing back a token is
#: SPEC-CORRECT — the cap costs a caller nothing but a second call. Declared here rather than
#: clamped in the body so the schema states the real bound. An over-limit request is refused by
#: `install_problem_handlers`, which carries the spec `code` (INVALID_INPUT) a generated client
#: dispatches on.
_MAX_LIST_LIMIT = 1000

router = APIRouter(prefix="/v1/table", tags=["table"])

#: THE MANAGEMENT SURFACE ([[LH-021]]). rask-only governance mounts here, not on the spec prefix: a
#: spec client meeting `undrop` or `protection` on `/v1/table` has no way to tell a rask verb from a
#: Lance one. Deletion protection and the trash are the clearest case — the spec has `drop` and no
#: notion of a drop that can be taken back. It inherits the same three rungs as the
#: spec router (`api/v1/router.py`), so moving a route does not move it out of its gate.
management_router = APIRouter(prefix="/management/v1/table", tags=["table-management"])


def _is_expired(expires_at: str) -> bool:
    """Has the purge-eligibility deadline passed? Unparseable or absent → NOT expired.

    Failing to "not expired" is the safe direction: this flag only ever adds urgency to a warning,
    and a malformed timestamp must not make a still-recoverable table read as beyond saving. It
    gates no behaviour — undrop checks whether the RECORD exists, never this (diff2 F10 item 5).
    """
    if not expires_at:
        return False
    try:
        deadline = datetime.fromisoformat(expires_at)
    except ValueError:
        log.warning("trash_expiry_unparseable", extra={"expires_at": expires_at})
        return False
    if deadline.tzinfo is None:  # records written before the stamp carried an offset
        deadline = deadline.replace(tzinfo=UTC)
    return deadline < datetime.now(UTC)


def _collect_tables(ns: LanceNamespace, root_tables: list[str]) -> list[str]:
    """Every table in the tree, fully qualified, declared ones included — root tables plus each namespace's, depth-first.

    Synchronous and run in a threadpool by the caller: the native namespace client is blocking, and
    a walk of N namespaces is N blocking calls.

    ``root_tables`` arrives already holding the root rows AND every bound warehouse's tables, because
    this walk alone is blind to a bound namespace: the shipped ``dir`` backend stores a namespace as a
    ``__manifest`` ROW, so ``list_namespaces`` at the ROOT yields nothing (measured live: the estate
    held media/silver/alpha/beta with registered tables while this walk returned only the two flat
    root rows, so the lakehouse Tables registry showed 2 of 9). :func:`list_all_tables` lists those
    through the BINDINGS registry, each through its own warehouse-rooted connection, before calling here.
    """
    found = list(root_tables)
    stack: list[list[str]] = [[]]
    seen: set[tuple[str, ...]] = set()
    while stack:
        parent = stack.pop()
        if len(parent) >= MAX_NAMESPACE_DEPTH or tuple(parent) in seen:
            continue
        seen.add(tuple(parent))
        if parent:
            try:
                tables = native.call(ns, "list_tables", ListTablesRequest(id=parent, include_declared=True)).tables or []
                found.extend(fga.canonical_object_id([*parent, name]) for name in tables)
            except Exception:  # noqa: BLE001 — one bad namespace, not a blank registry
                log.warning("could not list tables in %s", parent, exc_info=True)
        try:
            children = native.call(ns, "list_namespaces", ListNamespacesRequest(id=parent)).namespaces or []
        except Exception:  # noqa: BLE001 — a namespace we cannot list must not empty the whole list
            log.warning("could not list namespaces under %s", parent or "<root>", exc_info=True)
            continue
        stack.extend([*parent, child] for child in children)
    # A bound namespace the native walk ALSO found arrives twice — dedupe, deterministically.
    return sorted(set(found))


@router.get("", response_model_exclude_none=True)
async def list_all_tables(
    request: Request,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    client: FgaClientDep,
    resolver: ResolverDep,
    page_token: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=_MAX_LIST_LIMIT)] = None,
    include_declared: bool = True,
) -> ListTablesResponse:
    """List every table in the namespace via ``list_all_tables``; when FGA is on,
    filter the result down to the tables the caller can ``can_read_data``.
    ``include_declared=false`` lists only the tables that hold a committed version, each judged by its own storage
    (:func:`catalog.api.listings.holding_a_version`, [[LH-528]]).

    Pagination is applied to the FINAL merged result, never to the native root call: the response is
    root tables + bound seeds + the namespace walk, so a native-level ``limit`` would truncate only
    the first ingredient and silently DROP tables from every page (#141 — the endpoint advertised
    pagination and ignored it). The merged list is sorted and deduped, so the cursor is keyset-style:
    ``page_token`` = the last name of the page, stateless and stable across pages. The walk itself
    still costs the full estate server-side per call — acceptable at admin frequency, and honest now.
    """
    req = ListTablesRequest(id=[], include_declared=True)
    response: ListTablesResponse = await run_in_threadpool(native.call, ns, "list_all_tables", req)
    # RECURSE INTO CHILD NAMESPACES. `list_all_tables` at the root returns only tables sitting
    # DIRECTLY at the root — despite the name — so with the estate's own convention (tiers are
    # namespaces: bronze$pages, silver$features, gold$catalog) this endpoint answered `{"tables":[]}`
    # while the catalog happily reported `bronze` -> ["pages"] one call away. Both lakehouse
    # registries (Tables and Namespaces, which derives its groups from THIS list) were therefore
    # permanently empty the moment anyone did the right thing and put a table in a namespace.
    #
    # Names come back FULLY QUALIFIED (`bronze$pages`), which is what the UI already assumes — it
    # groups on the delimiter — and what the FGA filter below resolves to each table's uuid.
    # BOUND namespaces first, each through its OWN warehouse-rooted connection. The request-scoped
    # `ns` on a collection route is ALWAYS the default root (get_namespace has no {id} to route by),
    # so listing a bound seed through it structurally returns nothing — measured live as the estate
    # listing showing 2 of 9 while every per-namespace route saw everything. `namespace_for_top_ns`
    # is the same binding resolution the per-id routes use; a seed that fails (deactivated warehouse,
    # unreadable bucket) degrades to a warning, never a blank registry.
    bound = await run_in_threadpool(warehouses.list_bindings, settings.registry_root, settings.storage_options())

    async def _seed_tables(top: str) -> list[str]:
        seed_ns = await namespace_for_top_ns(request, settings, top)
        listed = await run_in_threadpool(native.call, seed_ns, "list_tables", ListTablesRequest(id=[top], include_declared=True))
        return [fga.canonical_object_id([top, name]) for name in (listed.tables or [])]

    # CONCURRENT, not one seed at a time (catalog-api-11). Each bound warehouse is an independent
    # round trip whose result is only collected — nothing between two of them makes the order matter —
    # and this runs on the registry page every render, so a sequential walk made the estate listing
    # cost N round trips in N tenants. `gather(return_exceptions=True)` keeps the per-seed tolerance
    # exactly as it was: one unreachable warehouse degrades to a warning, never a blank registry.
    # Bounded by anyio's own threadpool limiter, which every `run_in_threadpool` here shares.
    for top, listed_or_error in zip(
        [b["top_ns"] for b in bound],
        await asyncio.gather(*(_seed_tables(b["top_ns"]) for b in bound), return_exceptions=True),
        strict=True,
    ):
        if isinstance(listed_or_error, BaseException):
            log.warning("could not list bound namespace %s", top, exc_info=listed_or_error)
        else:
            response.tables.extend(listed_or_error)
    candidates = [(name, parse_identifier(name)) for name in await run_in_threadpool(_collect_tables, ns, response.tables)]
    if not include_declared:
        candidates = await listings.holding_a_version(resolver, so, candidates)
    # When FGA is on and the caller is known, return only the tables they can read, each judged on the object its name
    # resolves to; pages count only tables the caller can see.
    if settings.fga_enabled and token is not None and client is not None:
        response.tables, response.page_token = await fga_deps.authorized_page(
            client,
            resolver,
            user=token.subject,
            relation="can_read_data",
            kind="table",
            candidates=candidates,
            page_token=page_token,
            limit=limit,
        )
        return response
    response.tables, response.page_token = paginate([name for name, _ in candidates], page_token, limit)
    return response


def _vendable_bases(location: str, settings: Settings, so: dict[str, str]) -> VendableBases | None:
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


def _mint(location: str, *, tier: Tier, bases: VendableBases, vendor: CredentialVendor, session_name: str) -> dict[str, str] | None:
    """The ``storage_options`` the vendor mints for ``location`` at ``tier``, with the expiry the spec reads.

    THE EXPIRY GOES INSIDE ``storage_options``, where the spec puts it and the only place a stock client looks
    (``lance_docs/ns_catalog/spec.yaml`` § DescribeTableResponse: "If the vended credentials are temporary, the
    ``expires_at_millis`` key ..."). Without it a temporary credential looks permanent, and the client discovers
    the TTL by failing at the 900-second mark instead of refreshing.
    """
    creds = vend_or_refuse(vendor, table_location=location, tier=tier, session_name=session_name, bases=bases.bases, data_directories=bases.data_directories)
    if creds is None:
        return None
    options = dict(creds.storage_options)
    if creds.expires_at_millis is not None:
        options["expires_at_millis"] = str(creds.expires_at_millis)
    return options


async def _vend_on_spec_door(
    ns: LanceNamespace,
    *,
    door: str,
    segments: list[str],
    table_uuid: str,
    location: str,
    settings: Settings,
    so: dict[str, str],
    vendor: CredentialVendor,
    token: Caller | None,
    client: OpenFgaClient | None,
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
    :func:`fga_deps.spec_vend_tier`'s. Only then is a credential minted, and only a mint writes the
    ``vend_credentials`` issuance record.

    ``asked`` is ``vend_credentials`` set true. Left unset, the credential is the server's offer: a refusal or a
    failed exchange leaves the answer without one and is logged, because a metadata read, the catalog's most
    called door, must not fail on the object store's STS. Asked for, the refusal is the answer.

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
        options = await run_in_threadpool(partial(_mint, location, tier=tier, bases=bases, vendor=vendor, session_name=role_session_name(subject)))
    except LanceNamespaceError:
        if asked:
            raise
        log.warning("spec_vend_skipped", extra={"door": door, "table": "/".join(segments)}, exc_info=True)
        return None
    if options is not None:
        resource = fga.table_object(table_uuid)
        audit("vend_credentials", SUCCESS, subject=token.subject if token is not None else None, resource=resource, tier=tier, door=door)
    return options


# `paginate` lives in `catalog.api.pagination` (it moved when the model registry needed the same
# cursor) — two keyset implementations in one service is two things to keep in step.
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
    emitter: LineageEmitterDep,
    uuids: ObjectUuidsDep,
    control: ControlEmitterDep,
    resolver: ResolverDep,
    body: DeclareTableRequest | None = None,
    authorization: Annotated[str | None, Header()] = None,
) -> DeclareTableResponse:
    """Declare a new (empty) table at ``id``, at the namespace root under the uuid the catalog mints for it
    (``dataplane.declare_table``, [[LH-520]]), then seed the caller's FGA ownership and emit a versionless
    DECLARE_TABLE marker (the table's first provenance — who reserved it + where).

    The catalog chooses a declared table's location, which the spec leaves to the implementation
    (`lance_docs/ns_catalog/spec.yaml:3823-3827`), so a request naming one is refused. The declaration keeps no
    ``properties``: it is recorded through RegisterTable, which keeps none (`spec.yaml:2719-2724`).

    The response carries the credential the declarer writes the table with, the highest tier FGA grants
    (:func:`_vend_on_spec_door`), unless the request sets ``vend_credentials`` false.
    """
    # LANCE-ONLY (2026-08-15 ruling). This door takes `properties` through its spec request
    # model and never checked it, so a non-Lance format could be selected here while the create
    # door rejected it. Before anything is reserved, and so is the reserved-namespace rule the create
    # door holds its properties to: the table's uuid is the catalog's to mint ([[LH-519]]).
    reject_unsupported_properties(body.properties if body else None)
    provenance_guard.refuse_reserved_keys((body.properties if body else None) or {}, door="declare properties")
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
    # The table's uuid is minted with its location's claim, before anything grants or announces it (ADR 0173 §2, §5).
    location = await run_in_threadpool(partial(dataplane.declare_table, ns, so, segments, root=root, claims=claims, holder=canonical))
    table_uuid = await run_in_threadpool(table_claims.uuid_at, claims, location, segments)
    response = DeclareTableResponse(location=location)

    async def _undo_declare() -> None:
        await run_in_threadpool(native.call, ns, "drop_table", DropTableRequest(id=segments))
        await run_in_threadpool(location_claims.release, claims, location, canonical)

    # A declared table holds NO data — the undo can never destroy bytes, so this door is the safest
    # possible place to compensate and there was never a reason for it to be the riskiest. Without it a
    # failed seed left a declared-only table that its declarer could not see, could not drop, and could
    # not re-declare (native `AlreadyExists`), reserving the id against everyone, permanently (F3).
    await fga_deps.seed_ownership_or_compensate(client, settings, token, resolver, resource="table", segments=segments, uuid=table_uuid, undo=_undo_declare)
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
            )
        except Exception:
            await fga_deps.compensate_create(
                client, settings, token, resource="table", segments=segments, uuid=table_uuid, undo=_undo_declare, reason="vend_failed"
            )
            raise
    response.managed_versioning = True
    # Versionless (no data yet): records who declared it + the reserved location, and keys the CREATED edge
    # (declare_table ∈ lineage _CREATE_OPS). Reconcile fills the real version once data lands at the URI.
    await emit_write_event(
        emitter,
        segments,
        author=token.subject if token is not None else None,
        version=None,
        operation=DECLARE_TABLE,
        authorization=authorization,
        uuids=uuids,
        source_uri=response.location,
    )
    await emit_control(
        control,
        action="table_declared",
        object_type="table",
        object_id=fga.table_object(table_uuid),
        object_name=canonical,
        actor=f"user:{token.subject}" if token is not None else None,
        extra={"location": response.location},
    )
    return response


def _refuse_a_branch_describe_cannot_honour(branch: str | None) -> None:
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

    ONE FUNCTION FOR BOTH CHANNELS. `branch` reaches this door through the body AND the query string,
    and the query channel was added later precisely because a refusal covering one of two is why this
    door read as settled while `?branch=work` still answered 200 for main (measured 2026-08-31).

    `main` is permitted: naming the branch you are already on is not a request this door cannot honour.
    """
    if dataplane.recorded_branch(branch) is None:
        return
    dataplane.refuse_a_branch_this_door_cannot_honour(
        branch, door="describe", remedy=f"Use the branch operations under /v1/table/{{id}}/branches, or read the branch through `count_rows`. (got {branch!r})"
    )


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
    ``DescribeTableRequest`` BODY (``lance_docs/namespace.md`` §DescribeTable; the generated client's
    ``_describe_table_serialize`` sends exactly that split). This route once bound all six as bare scalars,
    so FastAPI made them all query params and the body was dropped unread — a spec-conformant client
    (``lance_ray.utils``, which calls ``describe_table(DescribeTableRequest(id=...))`` and merges
    ``storage_options``) got a location and NO credentials, and any version pin silently read latest.
    Both forms are now accepted: **a field present in the body wins**, because the body is the spec's form
    and the query is rask's own older shorthand that its zones and tests still speak. Presence is decided by
    the JSON keys actually sent, not by the model's defaults. The body is OPTIONAL here (the spec marks it
    required) so those existing query-only callers keep working — a superset, not a deviation in meaning.

    ``tag`` is resolved to its version HERE via the dataplane's tag store, from either door: the native
    backend at pylance 8.0.0 silently IGNORES a describe-request ``tag`` (probed 2026-07-10 — a nonexistent
    tag described the LATEST version with no error), so forwarding it would lie; the catalog resolves
    (404 on an unknown tag, like ``/tags/version``) and describes at the resolved version instead.

    ``branch`` (body-only) is REFUSED unless it names main — see the site below. ``identity`` and ``context``
    are ignored: the caller is the verified bearer token, not a field of the request body.

    CREDENTIALS COME WITH THE ANSWER unless the caller sets ``vend_credentials`` false: unset, the spec lets
    the server decide (``lance_docs/ns_catalog/spec.yaml`` § DescribeTableRequest), and a stock client opening
    through the namespace that gets none signs with whatever its environment holds. The tier is the highest FGA
    grants the caller (:func:`_vend_on_spec_door`, ADR 0156 C3): the router admitted a metadata reader, and a
    write credential is decided by the tier judge like every write.
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
        )
    return response


def _describe(
    table: DoorTable,
    so: dict[str, str],
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
            version = dataplane.get_tag_version(table, so, GetTableTagVersionRequest(id=segments, tag=tag)).version
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


def _load_or_judge_declared(response: DescribeTableResponse, table: DoorTable, so: dict[str, str], *, pinned: lance.LanceDataset | None) -> None:
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
            return
    response.is_only_declared = False
    _load_detailed_metadata(response, dataset)


def _load_detailed_metadata(response: DescribeTableResponse, dataset: lance.LanceDataset) -> None:
    """Answer ``dataset``'s version and schema as the backend's detailed describe does, and its own schema metadata.

    ``metadata`` is the table's user metadata, read from the same handle, so a table property a caller writes through
    ``schema_metadata/update`` is read back here.
    """
    loaded = DescribeTableResponse.model_validate({"version": dataset.version, "schema": json_arrow.table_schema_json(dataset.lance_schema)})
    response.version = loaded.version
    response.var_schema = loaded.var_schema
    metadata = dataplane.user_schema_metadata(dataset.schema)
    if metadata:
        response.metadata = metadata


#: The spec gives `exists` a 200 with NO `content:` block — *"Success, no content"* — so a conforming
#: server sends an empty body. A handler annotated `-> None` sends `application/json` with the four
#: bytes `null`, which a strict client reads as a JSON document where the contract promised none.
#: `response_class=Response` is what makes FastAPI emit nothing; `status_code=200` alone does not.
@router.post("/{id}/exists", status_code=200, response_class=Response)
def table_exists(id: str, table: DoorTableDep, body: TableExistsRequest | None = None) -> Response:
    """Check the table at ``id`` exists via ``table_exists`` — 200 if present (spec 0.9), else error.

    The answer is about the table the request resolved, so it stands only while the name still resolves there ([[LH-530]]).
    """
    segments = reconcile_body_id(parse_identifier(id), body.id if body else None)
    table.read_natively("table_exists", TableExistsRequest(id=segments, version=body.version if body else None))
    return Response(status_code=200)


@router.post("/{id}/drop", response_model_exclude_none=True)
async def drop_table(
    id: str,
    ns: NamespaceDep,
    table: ResolvedTableDep,
    settings: SettingsDep,
    client: FgaClientDep,
    emitter: LineageEmitterDep,
    uuids: ObjectUuidsDep,
    control: ControlEmitterDep,
    token: CurrentToken,
    so: StorageOptionsDep,
    authorization: Annotated[str | None, Header()] = None,
    force: RaskFlag = False,
    purge: RaskFlag = False,
    idempotency_key: idem.IdempotencyKeyHeader = None,
) -> DropTableResponse:
    """Drop the table at ``id`` via ``drop_table``, then revoke its FGA tuples and
    emit a best-effort ``drop_table`` lineage event.

    Deletion protection (#73, the warehouse door's Decision-5 contract extended to the rung where a
    drop deletes BYTES): a ``protected`` control-root record refuses 409 unless ``force=true``, and
    ``force`` turns the protection lock ONLY — the FGA gate ran before this handler, identically
    with or without it."""
    # SAME AMPLIFIER AS create (see `catalog.api.idempotency`): a replay re-enters a door that
    # DELETES BYTES or files a trash record. OPTIONAL, because the spec defines no such header and a stock client must keep working.
    converge = await idem.begin(settings, so, token, idempotency_key, endpoint="POST /v1/table/{id}/drop")
    if converge.replay is not None:
        return DropTableResponse.model_validate(converge.replay.body)

    segments = parse_identifier(id)
    canonical = fga.canonical_object_id(segments)
    await fga_deps.require_table_unprotected(settings, table, force=force)
    table_uuid = table.require_uuid()
    # THE DOOR ACTS ON THE TABLE ITS NAME RESOLVED TO (ADR 0173 §7, [[LH-521]]): the fence holds the claim that
    # resolution read, so a rename landing since answers this drop 409, and one landing after the fence answers 409
    # itself, rather than the drop reaching whatever table holds the name by then. The drop deletes the LOCATION, so it
    # also refuses while the claim names another table that still resolves to it ([[LH-204]]).
    claims = table_claims.claim_store(settings)
    fenced = await run_in_threadpool(partial(table_claims.fence, ns, claims, table, holder=canonical, verb="drop"))
    location = fenced.location
    # What the drop's announcements name the table by, read while it still stands (ADR 0173 §10).
    known = await uuids.table(segments, uuid=table.uuid)
    # #75 the drop→undrop path. With a grace period configured, a drop DEREGISTERS (detaches the
    # pointer; the bytes stay exactly where they are) and files a trash record naming the location and
    # the deadline. This is what makes a fat-fingered drop survivable: time-travel cannot help here,
    # because `restore_table` rewinds a LIVE table and a real drop leaves no version to rewind to.
    # `purge=true` is the explicit opt-out — a caller who means "destroy the bytes now" says so.
    trashed = settings.trash_grace_days > 0 and not purge
    try:
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
                grace_days=settings.trash_grace_days,
                namespace_uuid=known.namespace_uuid,
            )
            await run_in_threadpool(trash.put, settings.registry_root, settings.storage_options(), record)
            await run_in_threadpool(native.call, ns, "deregister_table", DeregisterTableRequest(id=segments))
            response = DropTableResponse()
        else:
            # [[LH-532]] The bytes go at the location this door resolved, at the key Lance opens, never where the
            # backend's own drop looks: the backend removes only the row. RECORD FIRST here too: a residue record is filed
            # before the row goes and cleared once the bytes are, so bytes a failed delete leaves keep their claim.
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
                )
            )
    except dataplane.TableBytesLeftError:
        # The row is gone and bytes remain: the claim stays held, kept by the residue record once the fence lapses, the
        # grants stay, and nothing announces a drop that did not finish. The trash purge reclaims the objects.
        raise
    except Exception:
        await run_in_threadpool(table_claims.lift_fence, claims, fenced)
        raise
    if trashed:
        # [[LH-204]] The trashed id keeps its location's claim until undrop or purge, so no other id registers over
        # bytes someone can still restore.
        await run_in_threadpool(table_claims.lift_fence, claims, fenced)
    else:
        # [[LH-279]] The base record goes with the bytes: left behind, it would sanction its bases for the next
        # table registered at this location. The location's claim goes too ([[LH-204]]).
        registry = base_registry.BaseRegistry(control_root=settings.registry_root, storage_options=settings.storage_options())
        await run_in_threadpool(partial(base_registry.forget_base_record, registry, location, table=canonical))
        await run_in_threadpool(location_claims.release, claims, location, canonical)
    # What was kept for the table under its uuid ends with it ([[LH-523]]); a table created later at this name holds
    # another uuid, so nothing left here could govern it. A recoverable drop keeps its maintenance policy for the undrop,
    # as it keeps its grants, and the trash purge ends both. Its protection goes now, because the drop it guarded has
    # happened.
    if not trashed:
        await run_in_threadpool(object_records.end_object_records, settings.registry_root, settings.storage_options(), "table", table_uuid)
    else:
        await run_in_threadpool(protection.clear_protection, settings.registry_root, settings.storage_options(), "table", table_uuid)
    # Record the drop as best-effort lineage — provenance of the deletion (the dataset node persists in the
    # graph, named a `drop_table` run). Inline-awaited (NOT BackgroundTasks) → reaches the durable
    # Dapr/JetStream transport before the response; best-effort, so it never fails the drop. Emitted BEFORE
    # the revoke: on the http transport the caller's bearer authorizes ingest against their (still-live)
    # write grant — revoking first would 403 the very event that records who dropped the table.
    await emit_write_event(
        emitter,
        segments,
        author=token.subject if token is not None else None,
        version=None,
        operation=DROP_TABLE,
        authorization=authorization,
        uuids=uuids,
        known=known,
    )
    # Revoke the table's FGA tuples so a later table reusing this id can't inherit stale grants — but
    # ONLY on a destructive drop. A RECOVERABLE drop (#75) leaves an object that still exists and whose
    # owner is the one person who needs to undrop it; revoking here made undrop unreachable for exactly
    # that caller (found by driving the deployed catalog, not by a unit test — the unit tests run FGA
    # off). The grants die with the bytes instead: at purge, or when the sweep's expiry reclaims it.
    if not trashed:
        await fga_deps.revoke_ownership(client, settings, obj=fga.table_object(table_uuid), token=token)
    await emit_control(
        control,
        action="table_dropped",
        object_type="table",
        object_id=fga.table_object(table_uuid),
        object_name=canonical,
        object_uuid=known.table_uuid,
        namespace_uuid=known.namespace_uuid,
        actor=f"user:{token.subject}" if token is not None else None,
        extra={"recoverable": trashed},
    )
    await converge.remember(200, response)
    return response


@router.post("/{id}/deregister", response_model_exclude_none=True)
async def deregister_table(
    id: str,
    ns: NamespaceDep,
    table: ResolvedTableDep,
    settings: SettingsDep,
    client: FgaClientDep,
    emitter: LineageEmitterDep,
    uuids: ObjectUuidsDep,
    control: ControlEmitterDep,
    token: CurrentToken,
    so: StorageOptionsDep,
    body: DeregisterTableRequest | None = None,
    authorization: Annotated[str | None, Header()] = None,
    force: RaskFlag = False,
    idempotency_key: idem.IdempotencyKeyHeader = None,
) -> DeregisterTableResponse:
    """Deregister the table at ``id`` (detach it without deleting data) via lance_namespace
    ``deregister_table``, then revoke its FGA ownership and emit a best-effort ``deregister_table`` marker.

    Protection-gated like drop (#73): deregister keeps bytes but REMOVES the object from governance —
    the flag's whole jurisdiction — so leaving it ungated would make "deregister, then delete the
    files by hand" the unprotected path around the protected drop.

    A table no uuid identifies is the estate admin's to deregister (the router asks that rung for it, [[LH-534]]): it
    holds no grant, no record and no protection, since each is kept under a uuid, so its row is detached and its
    location's claim released, and the detach is announced on the control plane alone. Lineage refuses an event naming
    a table by no uuid ([[LH-166]]), so the feed is not told."""
    # SAME AMPLIFIER AS create (see `catalog.api.idempotency`): a replay re-enters a door that
    # retires the id while the bytes stay. OPTIONAL, because the spec defines no such header and a stock client must keep working.
    converge = await idem.begin(settings, so, token, idempotency_key, endpoint="POST /v1/table/{id}/deregister")
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
    response = await _detach(ns, settings, table, segments=segments)
    # The table leaves governance, so what was kept for it under its uuid goes.
    await run_in_threadpool(object_records.end_object_records, settings.registry_root, settings.storage_options(), "table", table_uuid)
    # Record the detach as best-effort lineage — asymmetric with drop (which deletes data), deregister
    # only detaches, so without this marker the feed reads it as a still-live, never-touched table.
    # Versionless (no data was written), inline-awaited so it reaches the durable transport before the
    # reply. Emitted BEFORE the revoke: on the http transport the caller's bearer authorizes ingest against
    # their (still-live) write grant — revoking first would 403 the very marker this endpoint exists to
    # record, and the feed would keep showing the table as live.
    await emit_write_event(
        emitter,
        segments,
        author=token.subject if token is not None else None,
        version=None,
        operation=DEREGISTER_TABLE,
        authorization=authorization,
        uuids=uuids,
        known=known,
    )
    await fga_deps.revoke_ownership(client, settings, obj=fga.table_object(table_uuid), token=token)
    await emit_control(
        control,
        action="table_deregistered",
        object_type="table",
        object_id=fga.table_object(table_uuid),
        object_name=canonical,
        object_uuid=known.table_uuid,
        namespace_uuid=known.namespace_uuid,
        actor=f"user:{token.subject}" if token is not None else None,
        extra={},
    )
    await converge.remember(200, response)
    return response


async def _detach(ns: LanceNamespace, settings: Settings, table: ResolvedTable, *, segments: list[str]) -> DeregisterTableResponse:
    """Remove ``table``'s row from ``__manifest`` and release its location's claim; the bytes stay where they are.

    The door acts on the table its name resolved to, under the fence that holds the claim it read (ADR 0173 §7,
    [[LH-521]]): a rename landing since answers this deregister 409, as one landing after the fence answers itself.
    """
    canonical = fga.canonical_object_id(segments)
    claims = table_claims.claim_store(settings)
    fenced = await run_in_threadpool(partial(table_claims.fence, ns, claims, table, holder=canonical, verb="deregister"))
    try:
        response: DeregisterTableResponse = await run_in_threadpool(native.call, ns, "deregister_table", DeregisterTableRequest(id=segments))
    except Exception:
        await run_in_threadpool(table_claims.lift_fence, claims, fenced)
        raise
    # The detached bytes are nobody's now, so the id's claim on them goes ([[LH-204]]).
    await run_in_threadpool(location_claims.release, claims, fenced.location, canonical)
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
    configured one: a warehouse-bound table belongs to its warehouse's root, and a caller-side join would
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


@router.post("/{id}/register", response_model_exclude_none=True)
async def register_table(
    id: str,
    body: RegisterTableRequest,
    ns: NamespaceDep,
    root: NamespaceRootDep,
    settings: SettingsDep,
    token: CurrentToken,
    so: StorageOptionsDep,
    client: FgaClientDep,
    emitter: LineageEmitterDep,
    uuids: ObjectUuidsDep,
    control: ControlEmitterDep,
    resolver: ResolverDep,
    authorization: Annotated[str | None, Header()] = None,
    idempotency_key: idem.IdempotencyKeyHeader = None,
) -> RegisterTableResponse:
    """Register an existing table location at ``id`` via ``register_table``, refuse it when the location
    overlaps the control root, a configured base or another table, when the dataset there carries reader
    flag 256, or when it declares a base nothing sanctions; record the bases it is admitted with, then
    seed the caller's FGA ownership and emit a REGISTER_TABLE marker (who attached it + where)."""
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
    if RegisterMode.parse(body.mode) is RegisterMode.OVERWRITE:
        raise InvalidInputError(
            "mode 'Overwrite' is not supported on this door: replacing a registration would detach bytes this "
            "catalog does not own from the table that currently points at them. Deregister the existing table "
            "first, then register the new location."
        )
    # LANCE-ONLY (2026-08-15 ruling), as at `declare_table`, and the reserved-namespace rule the create door
    # holds its properties to: the table's uuid is the catalog's to mint ([[LH-519]]). Shape refusals like the
    # mode's, so before `idem.begin` for the same reason.
    reject_unsupported_properties(body.properties)
    provenance_guard.refuse_reserved_keys(body.properties or {}, door="register properties")
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
        model_roots=[settings.models_root, settings.model_artifacts_root],
    )
    # SAME AMPLIFIER AS create (see `catalog.api.idempotency`): a replay re-enters a door that
    # MINTS an id and seeds its ownership. OPTIONAL, because the spec defines no such header and a stock client must keep working.
    converge = await idem.begin(settings, so, token, idempotency_key, endpoint="POST /v1/table/{id}/register")
    if converge.replay is not None:
        return RegisterTableResponse.model_validate(converge.replay.body)

    segments = parse_identifier(id)
    await fga_deps.require_parent_exists(ns, "table", segments)
    body.id = reconcile_body_id(segments, body.id)
    try:
        response: RegisterTableResponse = await run_in_threadpool(native.call, ns, "register_table", body)
    except TableAlreadyExistsError:
        # CONVERGE THE STRUCTURAL EDGE, THEN REFUSE ANYWAY. The spec gives this door two modes —
        # `Create` (409) and `Overwrite` — and no ExistOk, so the 409 is the correct and unchanged
        # answer; what is wrong is leaving the id ungoverned while refusing it.
        #
        # A table registered whose seed never ran carries NO tuples at all, and every `table` relation
        # resolves through a direct tuple or `X from parent` — so it denies every principal, including
        # the identity that registered it, and no door can repair it because re-registering is exactly
        # what lands here. Measured 2026-09-15: five of the estate's own tiers in that state, and the
        # medallion's `register_written_dataset` re-enters this door on every run, converging on a
        # location check that never asked whether the object was governed.
        #
        # ONLY WHEN THE LOCATION MATCHES. A 409 at a different location is a genuine id conflict —
        # a different table living there — and writing an edge for it would attach somebody else's
        # object to this caller's namespace. Same location means this IS that table, so the edge is a
        # restatement of a fact the catalog already holds.
        #
        # The EDGE ALONE: `may_grant_owner=False`, so a caller cannot acquire a rung on a table it did
        # not create by re-registering it — the ownership-seizure refusal that guards the ExistOk arm
        # of `create` applies identically here.
        # THE CLAIM IS CHECKED AS THE TAIL OF THE REGISTRATION, not by equality. The body carries a
        # RELATIVE location — the dir backend refuses an absolute one on this door — while the
        # registered one is absolute. Resolving both through `absolute_table_location` would be dead
        # code that looks live: it PREFERS the described location over its argument, so both sides come
        # back identical and the guard would admit every 409 regardless of where it pointed.
        registered = await run_in_threadpool(absolute_table_location, ns, segments, None)
        claimed = (body.location or "").strip("/")
        if registered and claimed and _registration_points_at(registered, claimed):
            # A refused register whose detach failed also lands here on retry; a flag-256 dataset is
            # never converged into governance, and a dataset that cannot be read is not judged clean.
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
            # THE RECORD CONVERGES WITH THE EDGE ([[LH-279]]). The medallion re-enters this door on
            # every run, so a table registered before its record existed gets one here — judged exactly
            # as a fresh registration's bases are, and never for a base nothing sanctions.
            if facts is not None:
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
                        )
                    )
                except InvalidInputError:
                    unsanctioned = True
                if unsanctioned:
                    log.warning("register_converge_refused_unrecorded_bases", extra={"table": id, "location": registered})
                    raise
                # [[LH-252]] A data directory another table holds is never converged into this one's record.
                held = False
                try:
                    await run_in_threadpool(table_bases.claim_registered_directories, ns, context.registry, entries, context.holder, segments)
                except InvalidInputError:
                    held = True
                if held:
                    raise
                await run_in_threadpool(base_registry.claim_bases, context.registry, registered, entries)
            kept_uuid = await run_in_threadpool(table_claims.uuid_at, table_claims.claim_store(settings), registered, segments)
            await fga_deps.seed_ownership(client, settings, token, resolver, resource="table", segments=segments, uuid=kept_uuid, may_grant_owner=False)
            log.info("register_converged_governance", extra={"table": id, "location": registered})
        raise

    claims = table_claims.claim_store(settings)
    canonical = fga.canonical_object_id(segments)
    # The location as the backend RESOLVED it, the spelling every other door keys the claim by: a `%` in
    # the body is stored re-encoded, so the body's spelling can decode to another key.
    stated_location = f"{root.rstrip('/')}/{body.location}"
    claimed_location = await run_in_threadpool(absolute_table_location, ns, segments, stated_location) or stated_location

    async def _detach() -> None:
        # DEREGISTER, never drop. Register ATTACHES bytes that already existed and are not ours to
        # destroy — the whole point of the door. Deregister is the exact inverse: it removes the
        # catalog object this request created and leaves the data exactly where it was found. The
        # location's claim goes with it; one another table holds is not this registration's to free.
        await run_in_threadpool(native.call, ns, "deregister_table", DeregisterTableRequest(id=segments))
        await run_in_threadpool(location_claims.release, claims, claimed_location, canonical)

    # AFTER THE REGISTER, because only the backend knows which root a relative location resolves
    # against and the overlap check must see this registration's own row; BEFORE THE SEED, so a refused
    # dataset never gains an owner, a record entry, a lineage node or an event. The location's claim
    # first ([[LH-204]]): a rename holding it, or a table a recoverable drop is keeping it for, refuses
    # this registration even when the manifest shows no other row at the location. The claim records the
    # uuid minted for this table ([[LH-519]]), a new one even where an earlier table's claim was released.
    try:
        try:
            minted = await run_in_threadpool(table_claims.claim_new_table, ns, claims, claimed_location, table=canonical, segments=segments)
        except location_claims.LocationHeldError as held:
            log.warning("register_refused_held_location", extra={"table": id, "location": held.claim.location, "holder": held.claim.table})
            raise table_bases.occupied_location_refusal(body.location) from None
        verdict = await run_in_threadpool(table_bases.judge_registered_table, ns, so, segments, body.location, context)
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
        raise
    location = verdict.location
    table_uuid = minted.uuid
    if table_uuid is None:
        raise InternalError(f"the claim taken for {id} records no uuid")

    async def _undo_register() -> None:
        # Detach first: while the table is attached its record must stay, because its manifest still
        # declares those bases. Then the entries this registration claimed go with it.
        await _detach()
        if verdict.claim is not None:
            await run_in_threadpool(base_registry.release_claim, context.registry, verdict.claim)
        await run_in_threadpool(base_registry.release_data_directories, context.registry, verdict.directories, context.holder)

    await fga_deps.seed_ownership_or_compensate(client, settings, token, resolver, resource="table", segments=segments, uuid=table_uuid, undo=_undo_register)
    # RESOLVED, not echoed — `response.location` is the caller's own relative path, which names nothing a
    # reader of the feed can open; the flag judgement above already resolved it.
    # Versionless + source_uri=the attached location (register_table ∈ _CREATE_OPS, so the feed records it as a
    # create). The registered table already holds data at some version, which this marker does not name.
    await emit_write_event(
        emitter,
        segments,
        author=token.subject if token is not None else None,
        version=None,
        operation=REGISTER_TABLE,
        authorization=authorization,
        uuids=uuids,
        source_uri=location,
    )
    await emit_control(
        control,
        action="table_registered",
        object_type="table",
        object_id=fga.table_object(table_uuid),
        object_name=canonical,
        actor=f"user:{token.subject}" if token is not None else None,
        extra={"location": location},
    )
    await converge.remember(200, response)
    return response


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

    `expires_at` is when the object becomes PURGE-ELIGIBLE, not when recovery stops (diff2 F10 item
    5). Undrop does not check the clock — it checks whether the record is still there — so a passed
    deadline is a warning, not a verdict, and `expired` says which state you are in. Reporting it as
    finality would push an owner to give up on data that is still on storage and still recoverable.

    One entry per dropped table that held the name and that the caller may see, each with its uuid, which an undrop
    names when there are several ([[LH-525]]).
    """
    records = await resolver.admitted_trash("table", parse_identifier(id))
    entries: list[TrashEntry] = []
    for record in records:
        fields = {k: str(record.get(k, "")) for k in ("id", "uuid", "location", "dropped_by", "dropped_at", "expires_at")}
        entries.append(TrashEntry(**fields, expired=_is_expired(fields["expires_at"])))
    return entries


@management_router.post("/{id}/undrop", response_model_exclude_none=True)
async def undrop_table(
    id: str,
    ns: NamespaceDep,
    root: NamespaceRootDep,
    settings: SettingsDep,
    token: CurrentToken,
    control: ControlEmitterDep,
    emitter: LineageEmitterDep,
    uuids: ObjectUuidsDep,
    resolver: ResolverDep,
    authorization: Annotated[str | None, Header()] = None,
    uuid: TrashedUuidQuery = None,
) -> RegisterTableResponse:
    """Recover a dropped table from the trash (#75) — re-register its still-present bytes at its old
    id and clear the record. Owner-gated (``undrop`` maps to the drop rung: restoring an object into
    the namespace is the same authority as removing it).

    404 when there is no trash record — a never-trashed or already-PURGED drop is genuinely
    unrecoverable, and saying so plainly beats a 200 that recovers nothing.

    THE CLOCK DOES NOT GATE THIS, deliberately (diff2 F10 item 5). A record whose `expires_at` has
    passed but which the purge has not yet collected still undrops, because the bytes are still on
    storage and refusing would destroy recoverable data on a timestamp alone. The estate already
    reasons this way one service over — maintenance's own config says "a record that survives is a
    recovery that still works; a purged one is not" — and this door simply never said so out loud.
    What was wrong was the DESCRIPTION: the previous docstring called an expired drop "genuinely
    unrecoverable" alongside a never-trashed one, and `/tasks` reported the deadline as finality.
    Both now say purge-eligible, which is what it is.

    An expired undrop IS logged: it means the purge is behind, or somebody recovered at the very
    edge of the window, and an operator should be able to see either.

    THE NAME IS FREE ONCE ITS TABLE IS DROPPED ([[LH-525]], ADR 0173 §9), so the door restores exactly the trashed table
    its caller names: the one dropped under the name, or, when several were, the one ``?uuid=`` names (409 listing each
    one's uuid and deadline otherwise). It restores that table's uuid, and so its grants and records. While another
    table holds the name the undrop answers 409: two tables cannot share it.
    """
    segments = parse_identifier(id)
    canonical = fga.canonical_object_id(segments)
    so = settings.storage_options()
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
    if _is_expired(str(record.get("expires_at", ""))):
        log.warning("undrop_past_expiry", extra={"table": canonical, "uuid": record_uuid, "expires_at": record.get("expires_at")})
    standing = await resolver.table(segments, ns=ns)
    if standing.location is not None and standing.uuid != record_uuid:
        raise TableAlreadyExistsError(f"cannot undrop {canonical}: another table holds the name now; rename or drop it first")
    trash_doors.require_its_own_parent(record, standing=await resolver.namespace_uuid(segments[:-1]), name=canonical)
    # `register_table` REFUSES an absolute URI ("Location must be a relative path within the root directory"), and keeps
    # a relative one as the literal key it is given. The record keeps the URL `describe_table` answered, which
    # percent-encodes a registered table's name, so the table re-attaches at the key that URL decodes to, under the
    # connection's root, where its bytes are ([[LH-517]]). The absolute URL stays on the record for an operator.
    location = str(record["location"])
    body = RegisterTableRequest(id=segments, location=dataplane.relative_location(location, root=root))
    # The drop kept the location's claim for this id ([[LH-204]]), so this take is the retry of an
    # existing hold; another holder means another table now resolves to these bytes, and re-attaching
    # this id would put two live ids on one dataset.
    try:
        kept = await run_in_threadpool(table_claims.take, ns, table_claims.claim_store(settings), location, canonical, segments)
    except location_claims.LocationHeldError as held:
        log.warning("undrop_refused_held_location", extra={"table": canonical, "location": held.claim.location, "holder": held.claim.table})
        raise InvalidTableStateError(f"cannot undrop {canonical}: its location is now held by another table") from None
    if kept.uuid != record_uuid:
        raise InternalError(f"the claim on {canonical}'s location records another uuid than its trash record, so its grants cannot be found")
    try:
        response: RegisterTableResponse = await run_in_threadpool(native.call, ns, "register_table", body)
    except TableAlreadyExistsError:
        # ALREADY RECOVERED — converge instead of refusing. The namespace undrop has tolerated this
        # since #96 (`undrop_table_already_registered`); the table door did not, and that asymmetry
        # became a trap the moment the maintenance sweep started honouring trash records (F6(d)): a
        # crash between this register and the `trash.clear` below leaves a record on a LIVE table, so
        # the table is silently excluded from compaction and version GC — and the obvious retry used
        # to 409 here, leaving the record in place forever. Now the retry falls through to the clear
        # and the table rejoins the sweep.
        log.info("undrop_table_already_registered", extra={"table": canonical})
        response = RegisterTableResponse(location=location)
    await run_in_threadpool(warn_if_mixed_file_versions, location, so, table=canonical)

    # NO SEED — undrop is a PURE RESTORE (owner ruling, diff2 F10 item 4b).
    #
    # This used to call `seed_ownership_or_compensate`, which made the CALLER an owner. The recoverable
    # drop deliberately keeps the table's tuples (#75 — the owner is the one person who needs to undrop
    # it), so the original owner's grants are already live when we get here: the seed never restored
    # anything, it only ADDED whoever pressed the button. Anna drops a table, Bob undrops it, and the
    # estate silently gained a second owner with no grant ever having been made.
    #
    # The deliberate trade: a table that was ALREADY ownerless before the drop stays ownerless after
    # the undrop. The seed used to paper over that by granting the rescuer, which hid a pre-existing
    # stranding behind a privilege escalation. Surfacing stranded objects is the reconciler's job
    # (diff2 F3.3), not this door's.
    #
    # With the seed gone, nothing between the register and the clear can fail in a way that needs
    # compensating — the register is idempotent above, and a failed clear converges on retry — so the
    # `_undo_undrop` deregister went with it.
    # THE TABLE EXISTS AGAIN, and lineage has to hear it: the drop stamped the dataset DROP, and without a
    # CREATE fact the reconcile would skip a live governed table for good ([[LH-144]]). The same
    # versionless marker the register door sends, at the absolute location the record kept. Before the
    # clear, so a failed clear that is retried re-announces nothing it has not already announced.
    await emit_write_event(
        emitter,
        segments,
        author=token.subject if token is not None else None,
        version=None,
        operation=REGISTER_TABLE,
        authorization=authorization,
        uuids=uuids,
        source_uri=location,
    )
    await run_in_threadpool(trash.clear, settings.registry_root, so, record_uuid)
    await emit_control(
        control,
        action="table_undropped",
        object_type="table",
        object_id=fga.table_object(record_uuid),
        object_name=canonical,
        actor=f"user:{token.subject}" if token is not None else None,
        extra={},
    )
    return response


@management_router.post("/{id}/protection", response_model_exclude_none=True)
async def set_table_protection(
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


async def _hand_over_data_directories(settings: Settings, location: str, *, source: list[str], destination: list[str]) -> None:
    """Hand the renamed table's claims on its data directories to its new name ([[LH-252]]).

    Left with the old name, a directory's holder no longer resolves and the next registration declaring it is admitted
    once the claim's lease has run. Logged, never raised: the native rename has committed, and a failure here must not
    strand the caller after it; what failed to move stays under the old name, where an operator reading the log finds it.
    """
    old = fga.canonical_object_id(source)
    new = fga.canonical_object_id(destination)
    registry = base_registry.BaseRegistry(control_root=settings.registry_root, storage_options=settings.storage_options())
    try:
        await run_in_threadpool(partial(base_registry.hand_over_data_directories, registry, location, previous=old, table=new, segments=destination))
    except Exception as exc:  # noqa: BLE001 — the rename has committed; what did not move is logged, not raised
        log.error("rename_data_directories_not_handed_over", extra={"source": old, "destination": new, "error": str(exc)[:300]})


@router.post("/{id}/rename", response_model_exclude_none=True)
async def rename_table(
    id: str,
    body: RenameTableRequest,
    ns: NamespaceDep,
    table: ResolvedTableDep,
    so: StorageOptionsDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    emitter: LineageEmitterDep,
    uuids: ObjectUuidsDep,
    control: ControlEmitterDep,
    resolver: ResolverDep,
    root: NamespaceRootDep,
    authorization: Annotated[str | None, Header()] = None,
    force: RaskFlag = False,
    idempotency_key: idem.IdempotencyKeyHeader = None,
) -> RenameTableResponse:
    """Rename the table at ``id`` by moving its POINTER, and emit lineage; its grants stay on its uuid.

    Lance has no format-level rename (table rename is a namespace-layer remap, not a data-plane commit) and
    the ``dir`` backend's ``rename_table`` is a hard 501, which is also off-spec — the Namespace REST spec
    makes rename a first-class Metadata op. It is served here as what the spec describes: the destination id
    is registered at the SOURCE's existing location and the source id is deregistered. **No data byte is
    read, written or deleted**, so the cost is O(1) rather than the dataset's size — see
    ``dataplane.rename_table`` for the lance-ns V2 naming rule that makes this correct and for the one
    shape (a V1 root-namespace table) it refuses.

    The caller must hold ``can_drop`` on the source (owner tier, gated by the router ``authorize``) AND
    ``can_create_table`` on the DESTINATION parent — else a source owner could plant their table into a
    namespace/tenant they lack create rights on. The table keeps its uuid, so every record and every grant kept for it
    stays where it is ([[LH-523]], [[LH-524]]): into another namespace only its parent edge moves
    (``fga_deps.move_parent_edge``), and its data directories' claims move to its new name
    (``_hand_over_data_directories``). A versionless REGISTER marker records the attachment at the new id, with the source as its input, so the
    destination's provenance continues from the source's. Source missing → 404
    ``TableNotFound``; destination name taken → 409 ``TableAlreadyExists``; the location held by a
    concurrent rename or registration → 409 ``ConcurrentModification`` (code 14, [[LH-204]])."""
    # SAME AMPLIFIER AS create (see `catalog.api.idempotency`): a replay re-enters a door that
    # retires the source id and re-registers the destination. OPTIONAL, because the spec defines no such header and a stock client must keep working.
    converge = await idem.begin(settings, so, token, idempotency_key, endpoint="POST /v1/table/{id}/rename")
    if converge.replay is not None:
        return RenameTableResponse.model_validate(converge.replay.body)

    segments = parse_identifier(id)
    body.id = reconcile_body_id(segments, body.id)  # a contradictory body id is a 400, like every {id} route
    # #73: a rename RETIRES the source id, so the source's protection gates it exactly like drop even
    # though no byte moves: every reader and pipeline addressing the table by its id loses it. `force` rides
    # the query string as on the sibling doors, and the protection travels with the table to its new name.
    # Checked FIRST — before the parent/create gates — so a protected source refuses identically regardless
    # of destination.
    await fga_deps.require_table_unprotected(settings, table, force=force)
    # Rename mints a new table identifier: ``new_namespace_id`` (else the source's own namespace) + ``new_table_name``.
    new_segments = rename_destination(segments, body.new_namespace_id, body.new_table_name)
    # A rename can MINT an orphan as surely as a create, so its destination's namespace must exist. Same rule, same
    # door — checked before the native call, so a refused rename leaves the source exactly where it was.
    await fga_deps.require_parent_exists(ns, "table", new_segments)
    # Renaming INTO a namespace is a create in that namespace: authorize can_create_table on the DESTINATION
    # parent BEFORE the (destructive, relocating) rename — else a source-table owner could plant their table
    # into a namespace/tenant they have no create rights on. (authorize already gated can_drop on the source.)
    await fga_deps.require_create_on_parent(client, settings, token, resolver, resource="table", segments=new_segments)
    # The grants stay on the table's uuid and so follow it, so each is judged as a grant on the destination before
    # anything moves.
    renamed = fga.table_object(table.require_uuid())
    await fga_deps.require_grants_may_follow(client, settings, obj=renamed, source=segments, destination=new_segments)
    # Where the table inherits from, read before the rename moves it: a rename into another namespace moves its
    # parent edge, and that is all it writes.
    parents = (
        (await fga_deps.parent_object_of(resolver, settings, "table", segments), await fga_deps.parent_object_of(resolver, settings, "table", new_segments))
        if new_segments[:-1] != segments[:-1] and settings.fga_enabled
        else None
    )
    new_segments, location = await run_in_threadpool(
        partial(
            dataplane.rename_table,
            ns,
            table,
            body.new_table_name,
            body.new_namespace_id,
            so=so,
            claims=table_claims.claim_store(settings),
            root=root,
        )
    )
    await _hand_over_data_directories(settings, location, source=segments, destination=new_segments)
    # A rename keeps the table's uuid (ADR 0173 §2): both markers and the control event name the one the door resolved.
    source_ids = await uuids.table(segments, uuid=table.uuid)
    destination_ids = await uuids.table(new_segments, uuid=table.uuid)
    # The SOURCE's terminal marker, then the destination's. Both name the one uuid the door resolved, whose grants a
    # rename leaves where they are, so lineage authorizes both on the same object.
    #
    # DEREGISTER, NOT DROP, and the distinction is the whole change: the bytes survive under the new id.
    # Lineage reads both as this id leaving the catalog — `dropped_at()` fires and the reconcile stops
    # expecting the location under THIS id ([[LH-144]]) — and only DEREGISTER says the bytes were kept.
    # The destination's REGISTER marker below completes the pair: the same location, still governed,
    # now expected under the new id.
    await emit_write_event(
        emitter,
        segments,
        author=token.subject if token is not None else None,
        version=None,
        operation=DEREGISTER_TABLE,
        authorization=authorization,
        uuids=uuids,
        known=source_ids,
    )
    # A rename moves no grant ([[LH-524]]): every grant is held by the table's uuid, which the rename keeps. Into
    # another namespace, the parent edge moves with it.
    if parents is not None and parents[0] is not None and parents[1] is not None:
        await fga_deps.move_parent_edge(client, settings, token, obj=renamed, source=parents[0], destination=parents[1])
    # …then the DESTINATION's (re)attachment at its new location. Versionless: the relocate preserved
    # the on-disk version along with the whole version history, and the marker names none of it.
    await emit_write_event(
        emitter,
        new_segments,
        author=token.subject if token is not None else None,
        version=None,
        operation=REGISTER_TABLE,
        authorization=authorization,
        uuids=uuids,
        known=destination_ids,
        source_uri=location,
        # The destination is the renamed SOURCE, so record the source table as this event's input — otherwise
        # the rename severs the provenance chain and a follower of the feed or a forwarded graph backend sees the
        # renamed table as an orphan with no history (audit 2026-07-14). The source's DROP marker above ends its
        # own line; this input stitches the destination onto it.
        inputs=[InputPin(segments=segments, uuid=table.uuid)],
    )
    await emit_control(
        control,
        action="table_renamed",
        object_type="table",
        object_id=renamed,
        object_name=fga.canonical_object_id(new_segments),
        object_uuid=destination_ids.table_uuid,
        namespace_uuid=destination_ids.namespace_uuid,
        actor=f"user:{token.subject}" if token is not None else None,
        extra={
            "from": fga.canonical_object_id(segments),
            "to": fga.canonical_object_id(new_segments),
        },
    )
    response = RenameTableResponse()
    await converge.remember(200, response)
    return response


@router.post("/{id}/restore", response_model_exclude_none=True)
async def restore_table(
    id: str,
    body: RestoreTableRequest,
    table: DoorTableDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    uuids: ObjectUuidsDep,
    authorization: Annotated[str | None, Header()] = None,
    idempotency_key: idem.IdempotencyKeyHeader = None,
) -> RestoreTableResponse:
    """Restore the table at ``id`` to a prior version via ``restore_table``; emits a RESTORE_TABLE event at
    the NEW current version (restore mints a fresh version pointing at the restored data)."""
    # SAME AMPLIFIER AS create (see `catalog.api.idempotency`): a replay re-enters a door that
    # REWINDS the table to an older version. OPTIONAL, because the spec defines no such header and a stock client must keep working.
    converge = await idem.begin(settings, so, token, idempotency_key, endpoint="POST /v1/table/{id}/restore")
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
    response, committed = await run_in_threadpool(dataplane.restore, table, so, body)
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
    await converge.remember(200, response)
    return response


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
