"""Namespace metadata endpoints."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from functools import partial

from fastapi import APIRouter, Response
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    CreateNamespaceRequest,
    CreateNamespaceResponse,
    DeregisterTableRequest,
    DescribeNamespaceRequest,
    DescribeNamespaceResponse,
    DescribeTableRequest,
    DescribeTableResponse,
    DropNamespaceRequest,
    DropNamespaceResponse,
    InternalError,
    InvalidInputError,
    InvalidTableStateError,
    LanceNamespace,
    ListNamespacesRequest,
    ListNamespacesResponse,
    ListTablesRequest,
    ListTablesResponse,
    NamespaceAlreadyExistsError,
    NamespaceExistsRequest,
    NamespaceNotEmptyError,
    NamespaceNotFoundError,
    RegisterTableRequest,
    TableAlreadyExistsError,
    TableNotFoundError,
)
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel, ConfigDict, SkipValidation

from catalog.api import idempotency as idem
from catalog.api import listings
from catalog.api.announcing import Announcer, AnnouncerDep, Filing
from catalog.api.authz import decisions, door_gates, hierarchy, object_grants, objects, visible_pages
from catalog.api.dependencies import (
    ControlEmitterDep,
    FgaClientDep,
    NamespaceDep,
    NamespaceRootDep,
    ObjectUuids,
    ObjectUuidsDep,
    Resolver,
    ResolverDep,
    SettingsDep,
    StorageOptionsDep,
    TrashedUuidQuery,
)
from catalog.api.pagination import ListLimit
from catalog.api.rask_params import RaskFlag
from catalog.api.security import Caller, CurrentToken
from catalog.core import native, table_objects, write_judge
from catalog.core.config import CatalogStore, Settings
from catalog.core.formats import reject_unsupported_properties
from catalog.core.identifiers import MAX_NAMESPACE_DEPTH, UUID_PROPERTY, parse_identifier, reconcile_body_id, with_minted_uuid
from catalog.core.lineage_emit import DROP_TABLE, REGISTER_TABLE

# `MAX_NAMESPACE_DEPTH` is IMPORTED, not redeclared: the point of F10 item 10 is that two walkers
# over the same tree disagreed about how deep it may go, and a second copy of the number would let
# them drift apart again the moment one is tuned.
from catalog.core.modes import CreateMode, DropBehavior, DropMode
from catalog.core.namespace import warn_if_mixed_file_versions
from catalog.core.pagination import paginate
from catalog.schemas import ProtectionResponse, SetProtectionRequest, TrashEntry
from catalog.services import clone_endings, object_uuids, protections, table_claims, trash_doors, warehouses
from catalog.services.announcement_intents import Announcement, ControlMessage, NameResolves, NamespaceResolves, Unregistered
from catalog.services.clone_ending_records import CloneEnding
from catalog.services.tables import destroy
from lakehouse.events.control_emit import emit_control
from lakehouse.events.control_events import CatalogControlEvent, ControlUuids
from lakehouse.events.lineage_events import DatasetUuids
from lakehouse.governed import fga_ids
from lakehouse.naming import CATALOG_DELIMITER, uuid_of
from lakehouse.records import base_registry, clone_sources, location_claims, protection, trash
from lakehouse.records.protection import ProtectionRecord
from lakehouse.records.trash import TrashRecord
from lakehouse.storage.options import StorageOptions


log = logging.getLogger(__name__)

spec_router = APIRouter(prefix="/v1/namespace", tags=["namespace"])

#: THE MANAGEMENT SURFACE ([[LH-021]]). rask-only governance mounts here, not on the spec prefix: a
#: spec client meeting `undrop` or `protection` on `/v1/table` has no way to tell a rask verb from a
#: Lance one. Deletion protection and the trash are the clearest case — the spec has `drop` and no
#: notion of a drop that can be taken back. It inherits the same three rungs as the
#: spec router (`api/v1/router.py`), so moving a route does not move it out of its gate.
management_router = APIRouter(prefix="/management/v1/namespace", tags=["namespace-management"])

# Safety ceiling on the pagination loops in _collect_descendants — a runaway/looping backend token can't
# spin forever. Far above any real namespace fan-out.
_MAX_LIST_PAGES = 1000


def _collect_descendants(ns: LanceNamespace, segments: list[str]) -> list[tuple[str, list[str]]]:
    """Every ``(resource, segments)`` under ``segments`` — child tables AND nested namespaces, recursively.

    Enumerated BEFORE a Cascade drop removes them (afterwards they can't be listed), so the caller can
    revoke their FGA tuples once the drop commits. ``include_declared`` catches declared-only tables (they
    hold an owner grant too). Blocking native list calls → the caller runs this in a threadpool.

    DEPTH-CAPPED at the same bound the listing walk uses (diff2 F10 item 10). This recursed without one
    while `tables.py::_collect_tables` capped at ``MAX_NAMESPACE_DEPTH``, so the identical pathological
    tree was a truncated listing in one walker and a Python stack overflow in the other — and this is the
    walker that runs on the DESTRUCTIVE path, where crashing mid-enumeration means the cascade proceeds
    against a partial descendant list. Hitting the cap is reported the same way page truncation already
    is, because it has the same consequence: an incomplete revoke leaves orphan grants.
    """
    if len(segments) >= MAX_NAMESPACE_DEPTH:
        log.warning("namespace_depth_capped", extra={"namespace": segments, "cap": MAX_NAMESPACE_DEPTH})
        return []
    found: list[tuple[str, list[str]]] = []
    token: str | None = None
    for _ in range(_MAX_LIST_PAGES):
        tables: ListTablesResponse = native.call(ns, "list_tables", ListTablesRequest(id=segments, include_declared=True, page_token=token))
        found.extend(("table", [*segments, name]) for name in tables.tables or [])
        token = tables.page_token or None
        if not token:
            break
    else:
        # Hit the ceiling with a token still outstanding → a PARTIAL enumeration. Surface it (a partial
        # descendant list makes the cascade revoke silently incomplete → orphan grants), mirroring
        # fga_reads.read_object_tuples' openfga_read_truncated warning. In practice unreachable at this ceiling.
        log.warning("namespace_list_truncated", extra={"namespace": segments, "kind": "tables"})
    token = None
    for _ in range(_MAX_LIST_PAGES):
        children: ListNamespacesResponse = native.call(ns, "list_namespaces", ListNamespacesRequest(id=segments, page_token=token))
        for name in children.namespaces or []:
            child = [*segments, name]
            found.append(("namespace", child))
            found.extend(_collect_descendants(ns, child))  # recurse into the nested namespace
        token = children.page_token or None
        if not token:
            break
    else:
        log.warning("namespace_list_truncated", extra={"namespace": segments, "kind": "namespaces"})
    return found


@spec_router.post("/{id}/create", response_model_exclude_none=True)
async def create_namespace(
    *,
    id: str,
    keyed: idem.KeyedDoorDep,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
    resolver: ResolverDep,
    body: CreateNamespaceRequest | None = None,
) -> CreateNamespaceResponse:
    """Create a namespace via ``create_namespace``, owned by its creator and joined to its parent before it is recorded."""
    # LANCE-ONLY (2026-08-15 ruling). A namespace carries no bytes, but its `properties` map is
    # where a client sets a DEFAULT format for the tables created under it — so accepting one here
    # would let the ruling be bypassed one level up from the door that enforces it.
    reject_unsupported_properties(body.properties if body else None)
    declared = write_judge.declares_requires_run(body.properties if body else None)
    # WHAT `mode` MEANS HERE, from spec.yaml's CreateNamespaceRequest: "Create: the operation fails
    # with 409. ExistOk: the operation succeeds and the existing namespace is kept. Overwrite: the
    # existing namespace is dropped and a new empty namespace with this name is created." The backend
    # honours none of them (it answers NamespaceAlreadyExists for every mode), so this door supplies all
    # three. A value that is none of the three is refused by the parse itself, as InvalidInput naming
    # it — the closed-vocabulary rule `modes.py` states for every mode this catalog reads.
    create_mode = CreateMode.parse(body.mode if body else None)
    segments = parse_identifier(id)
    # And it must not nest deeper than the authz model can resolve. Both read walkers have capped depth
    # for a while; nothing capped CREATION, so the estate could be driven into a shape where
    # Check(can_get_metadata, warehouse:X) errors instead of answering — taking browsing down for the
    # whole bucket, its owners included. Shape rung, so 400, and before the native call.
    hierarchy.require_namespace_depth(segments)
    # As the table doors (`catalog.api.idempotency`): a replay under the caller's key answers the first attempt and re-enters nothing.
    converge = await keyed.begin(target=fga_ids.canonical_object_id(segments), takeover="rerun")
    if converge.replay is not None:
        return CreateNamespaceResponse.model_validate(converge.replay.body)
    req = body or CreateNamespaceRequest()
    req.id = reconcile_body_id(segments, req.id)
    # [[LH-519]] A uuid for the namespace this request creates (ADR 0173 §2, §6). An ExistOk that keeps one answers
    # the kept namespace's own, and an Overwrite's replacement is a new namespace with a new one.
    req.properties = with_minted_uuid(req.properties)
    minted = req.properties[UUID_PROPERTY]
    await _judge_run_rule_declaration(ns, settings, token, client=client, segments=segments, declared=declared, overwrite=create_mode is CreateMode.OVERWRITE)
    # The namespace's owner and its `parent` edge are written under the uuid minted above BEFORE the backend records it
    # ([[LH-585]], `object_grants.seed_namespace_before_write`), so a seed that fails has nothing to undo and no namespace
    # stands without them. They name the minted uuid, so they reach no namespace the request only finds standing: an
    # ExistOk that keeps one, or a create the backend refuses, takes them back.
    await object_grants.seed_namespace_before_write(client, settings, token, resolver=resolver, segments=segments, uuid=minted)
    create = _NamespaceCreate(ns=ns, settings=settings, token=token, client=client, resolver=resolver, segments=segments, minted=minted)
    created = await _create_in_its_mode(create, req, create_mode)
    response = created.response
    if created.kept_existing:
        # NOT ON THE `ExistOk` KEEP PATH, for the grants and for the ANNOUNCEMENT alike: the namespace kept is another
        # caller's, under its own uuid and grants, and `namespace_created` would announce a creation that did not
        # happen to every subscriber of the control stream.
        await object_grants.take_back_seed(client, settings, token, segments=segments, uuid=minted, reason="not_kept", resource="namespace")
        if declared and (response.properties or {}).get(write_judge.REQUIRES_RUN_PROPERTY) != write_judge.REQUIRED:
            # ExistOk keeps the namespace as it is, so a rule it does not already declare would not land. The rule is
            # declared once (ADR 0211): saying so beats a 200 that quietly ignored the declaration.
            raise NamespaceAlreadyExistsError(
                f"namespace {id!r} already exists without {write_judge.REQUIRES_RUN_PROPERTY}; the rule is declared once, at create"
            )
        await converge.remember(200, response)
        return response

    async def _undo_create() -> None:
        await run_in_threadpool(native.call, ns, "drop_namespace", DropNamespaceRequest(id=segments))

    await _require_parent_or_drop(create, undo=_undo_create)
    await object_grants.name_namespace_child(client, settings, token, resolver=resolver, segments=segments, uuid=minted)
    await emit_control(
        control,
        action="namespace_created",
        object_type="namespace",
        object_id=fga_ids.namespace_object(minted),
        object_name=fga_ids.canonical_object_id(segments),
        actor=f"user:{token.subject}" if token else None,
        extra={"mode": req.mode, "properties": req.properties},
    )
    await converge.remember(200, response)
    return response


class _NamespaceCreate(BaseModel):
    """What the steps of one namespace create share: the namespace it names, the uuid it minted, and who asks."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # The connection, the authorization client and the resolver are opaque collaborators, not values pydantic can judge.
    ns: SkipValidation[LanceNamespace]
    # The service's settings were validated once, where they were built: validating them again re-runs their
    # environment-bound checks on every request.
    settings: SkipValidation[Settings]
    token: Caller | None
    client: SkipValidation[OpenFgaClient | None]
    resolver: SkipValidation[Resolver]
    segments: list[str]
    #: The uuid this request minted for the namespace, which its grants are seeded under.
    minted: str


class _NamespaceCreated(BaseModel):
    """A namespace create's answer, and whether an ExistOk kept one already there."""

    model_config = ConfigDict(frozen=True)

    response: CreateNamespaceResponse
    kept_existing: bool


async def _create_in_its_mode(create: _NamespaceCreate, req: CreateNamespaceRequest, create_mode: CreateMode) -> _NamespaceCreated:
    """Create the namespace as ``create_mode`` says: the answer, and whether an ExistOk kept one already there."""
    # A create that fails takes back the grants seeded under ``minted`` unless the namespace stands.
    # RACE-FREE BY CATCHING RATHER THAN PRE-CHECKING. An `exists?` read followed by a create leaves a
    # window in which another caller creates the namespace; the backend's own refusal is the only
    # answer that cannot be stale.
    try:
        if create_mode is CreateMode.EXIST_OK:
            response, kept_existing = await create_or_keep_namespace(create.ns, create.segments, req)
        elif create_mode is CreateMode.OVERWRITE:
            response, kept_existing = (
                await _create_or_replace_empty_namespace(
                    create.ns,
                    settings=create.settings,
                    token=create.token,
                    client=create.client,
                    resolver=create.resolver,
                    segments=create.segments,
                    req=req,
                ),
                False,
            )
        else:
            response, kept_existing = await run_in_threadpool(native.call, create.ns, "create_namespace", req), False
    except Exception:
        await object_grants.take_back_unless_namespace_stands(
            create.ns, create.client, create.settings, token=create.token, segments=create.segments, uuid=create.minted, reason="not_written"
        )
        raise
    return _NamespaceCreated(response=response, kept_existing=kept_existing)


async def _require_parent_or_drop(create: _NamespaceCreate, *, undo: Callable[[], Awaitable[None]]) -> None:
    """Refuse a created namespace whose parent is not the one it was authorized under, undoing the create first."""
    try:
        await object_grants.require_parent_as_authorized(
            create.ns, create.settings, create.client, resolver=create.resolver, resource="namespace", segments=create.segments
        )
    except Exception:
        # A Restrict drop, which removes the namespace only while it holds nothing, so the undo destroys nobody's data.
        await object_grants.undo_a_create_whose_parent_is_unread(
            create.client,
            create.settings,
            create.token,
            resource="namespace",
            segments=create.segments,
            uuid=create.minted,
            undo=undo,
            remedy="drop it before retrying",
        )
        raise


async def _drain_tables(ns: LanceNamespace, segments: list[str]) -> tuple[ListTablesResponse, list[str]]:
    """Every table under ``segments``, declared ones included, following the backend's cursor to exhaustion.

    The twin of `_drain_namespaces` and for the identical reason: this route filters per item and then
    applies its own cursor, so reading the backend's first page alone answers a short list whose
    missing rows are indistinguishable from rows the caller may not read.
    """
    seen: list[str] = []
    token: str | None = None
    response = ListTablesResponse(tables=[])
    for _ in range(_MAX_LIST_PAGES):
        response = await run_in_threadpool(native.call, ns, "list_tables", ListTablesRequest(id=segments, include_declared=True, page_token=token))
        seen.extend(response.tables or [])
        token = response.page_token or None
        if not token:
            break
    else:
        log.warning("namespace_list_truncated", extra={"namespace": segments, "kind": "tables"})
    return response, sorted(set(seen))


async def _drain_namespaces(ns: LanceNamespace, segments: list[str]) -> tuple[ListNamespacesResponse, list[str]]:
    """Every child namespace under ``segments``, following the backend's own cursor to exhaustion.

    THE BACKEND'S PAGINATION IS NOT THE CALLER'S. This route applies its own keyset cursor AFTER the
    authorization filter — correct and deliberate, because a backend `limit` truncates BEFORE the
    filter, so a page could answer short and hand out a cursor that skips everything the filter
    removed. But reading only the backend's first page has the same failure in a different place:
    against a paginating backend the route answers a short list with a cursor that looks complete, and
    the dropped rows are indistinguishable from rows the caller may not read.

    `_collect_descendants` in this module already drains correctly; the two walkers over one tree
    disagreed, and the DESTRUCTIVE one was the careful one.
    """
    seen: list[str] = []
    token: str | None = None
    response = ListNamespacesResponse(namespaces=[])
    for _ in range(_MAX_LIST_PAGES):
        response = await run_in_threadpool(native.call, ns, "list_namespaces", ListNamespacesRequest(id=segments, page_token=token))
        seen.extend(response.namespaces or [])
        token = response.page_token or None
        if not token:
            break
    else:
        # A token still outstanding at the ceiling is a PARTIAL enumeration, and a partial listing that
        # answers 200 is exactly the silence this drain exists to remove. Logged like its sibling.
        log.warning("namespace_list_truncated", extra={"namespace": segments, "kind": "namespaces"})
    return response, sorted(set(seen))


async def create_or_keep_namespace(ns: LanceNamespace, segments: list[str], req: CreateNamespaceRequest) -> tuple[CreateNamespaceResponse, bool]:
    """Create the namespace, or KEEP the one already there. Returns ``(response, kept_existing)``.

    ``ExistOk`` SEMANTICS SUPPLIED HERE BECAUSE THE BACKEND DOES NOT. Driven 2026-09-13 against a real
    `dir` namespace: `create_namespace` raises `NamespaceAlreadyExistsError` for every mode — default,
    `ExistOk`, `exist_ok` and `Overwrite` alike — so any caller wanting those semantics has to apply
    them itself, and two callers here want them for different reasons.

    CATCHING RATHER THAN PRE-CHECKING. An ``exists?`` read followed by a create leaves a window another
    caller can create the namespace in, and the loser of that race would answer for a namespace it did not
    make. The backend's own refusal is the only answer that cannot be stale.

    The kept namespace is DESCRIBED rather than echoed, so a caller receives what the namespace
    actually is instead of a reflection of its own request.
    """
    try:
        return await run_in_threadpool(native.call, ns, "create_namespace", req), False
    except NamespaceAlreadyExistsError:
        described = await run_in_threadpool(native.call, ns, "describe_namespace", DescribeNamespaceRequest(id=segments))
        return CreateNamespaceResponse(properties=getattr(described, "properties", None)), True


async def _forget_namespace(settings: Settings, client: OpenFgaClient | None, token: Caller | None, *, namespace_uuid: str | None) -> None:
    """Remove what a dropped namespace leaves behind: the records kept under its uuid when the caller read one before
    the drop, and its FGA tuples, with those of every child an earlier attempt removed, which its `child` edges still
    name (``object_grants.end_removed_subtree``).

    Each step is idempotent — an absent record or tuple is already the post-condition — so a retry over a
    half-finished drop converges. A grant or record left behind governs nothing, since no other namespace holds the
    uuid it is keyed by ([[LH-524]]).
    """
    async for _ in object_grants.end_removed_subtree(client, settings, root_uuid=namespace_uuid, removed=[], token=token):
        pass


async def _judge_run_rule_declaration(
    ns: LanceNamespace,
    settings: Settings,
    token: Caller | None,
    *,
    client: OpenFgaClient | None,
    segments: list[str],
    declared: bool,
    overwrite: bool,
) -> None:
    """Refuse a create that declares the run rule, or an Overwrite that would drop one, unless an admin asks (ADR 0211).

    The rule is declared once: no Lance operation alters a namespace's properties, so the catalog's own Overwrite is the
    only way to drop it, and it needs the same admin as the declaration. A namespace beneath one that declares it is
    bound by it whatever it declares itself (:func:`write_judge.requires_run`). An Overwrite reads the namespace's own
    declaration first, to know whether it drops one; a caller who may create beside it already reads it as a reader of
    the parent.

    Raises:
        PermissionDeniedError: The caller is not the namespace's project admin (or the estate admin).
    """
    replaced = await write_judge.declared_on(ns, segments) if overwrite else False
    if not declared and not replaced:
        return
    project = await run_in_threadpool(warehouses.project_of, settings.registry_root, settings.storage_options(), segments[0])
    await door_gates.require_run_rule_authority(client, settings, token, project=project)


async def _create_or_replace_empty_namespace(
    ns: LanceNamespace,
    *,
    settings: Settings,
    token: Caller | None,
    client: OpenFgaClient | None,
    resolver: Resolver,
    segments: list[str],
    req: CreateNamespaceRequest,
) -> CreateNamespaceResponse:
    """``mode=Overwrite``: create the namespace, or drop the EMPTY one at the id and create it afresh.

    Spec (CreateNamespaceRequest.mode): "the existing namespace is dropped and a new empty namespace with
    this name is created". The drop is a RESTRICT drop, never a cascade: a create request is not where a
    caller decides to destroy a subtree, and the cascade's trash and descendant grants have no place on a
    create door. A namespace holding anything is refused 409 code 3 (NamespaceNotEmpty), naming what it
    holds, so the caller can empty it or drop it with ``behavior=Cascade`` deliberately.

    The drop clears the bar ``POST /v1/namespace/{id}/drop`` does: ``can_delete`` on the namespace (the
    router already checked create-on-parent) and its deletion protection. The old namespace's tuples,
    protection and policy go with it; the creator's grants name the replacement's own uuid.

    Catching rather than pre-checking, as in :func:`create_or_keep_namespace`: a create that wins needs no
    drop, and a concurrent create between the drop and the re-create answers the ordinary 409.
    """
    try:
        return await run_in_threadpool(native.call, ns, "create_namespace", req)
    except NamespaceAlreadyExistsError:
        pass
    canonical = fga_ids.canonical_object_id(segments)
    replaced = await objects.identify_namespace(resolver, ns, segments)
    await door_gates.require_relation(client, settings, token, relation="can_delete", obj=replaced.fga_object(), label=f"namespace:{canonical}")
    await hierarchy.require_namespace_unprotected(settings, replaced, force=False)
    _, tables = await _drain_tables(ns, segments)
    _, children = await _drain_namespaces(ns, segments)
    if tables or children:
        raise NamespaceNotEmptyError(
            f"cannot overwrite namespace '{canonical}': Overwrite drops only an empty namespace, and it holds "
            f"tables {tables} and namespaces {children}. Drop or move them first, or drop the namespace with behavior=Cascade."
        )
    await hierarchy.require_namespace_as_identified(ns, replaced)
    # A table landing between the listing and this drop is refused by the backend's own Restrict check.
    await run_in_threadpool(native.call, ns, "drop_namespace", DropNamespaceRequest(id=segments))
    await _forget_namespace(settings, client, token, namespace_uuid=replaced.uuid)
    return await run_in_threadpool(native.call, ns, "create_namespace", req)


@spec_router.get("/{id}/list", response_model_exclude_none=True)
async def list_namespaces(
    *,
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    resolver: ResolverDep,
    limit: ListLimit,
    page_token: str | None = None,
) -> ListNamespacesResponse:
    """List the child namespaces under ``id``; when FGA is on, filtered to what the caller can see.

    **THE SAME SPLIT ``list_tables`` BELOW APPLIES, and this route is why it is a split rather than a
    one-off.** ``authorize`` resolves this route's ``list`` action to ``can_get_metadata``, which C1
    redefined on a container as ``reader or can_get_metadata from child`` — correct for what it was
    for, since without it a grantee cannot resolve the breadcrumb to their own table. The consequence
    is that holding ``reader`` on ONE deep leaf opens this route on every ancestor, and the body
    answered with every sibling child-namespace name. Measured against the live store 2026-08-31: a
    holder of one leaf table checked ``can_get_metadata`` True on the parent and False on its sibling,
    and the sibling's NAME came back regardless.

    A namespace name is not a harmless header: a listing names data someone may not know exists, and a
    namespace name additionally leaks how a tenant organises data.

    So: the ROUTE still opens (narrowing it to 403 would restore the broken breadcrumb C1 fixed) and
    each ITEM is checked on the object itself, on ``can_get_metadata`` — the relation that governs
    seeing a namespace, where the table listing filters on ``can_read_data`` because reading a table's
    NAME and reading its ROWS are the same disclosure there.

    Pagination is applied AFTER the filter for the reason ``list_tables`` records: a backend ``limit``
    truncates first, so a page could answer short and hand out a cursor that skips everything the
    filter removed.
    """
    segments = parse_identifier(id)
    # THE ROOT'S CHILDREN ARE THE WAREHOUSES ([[LH-593]]): the estate's own and each registered one. Each is judged
    # below like any other item, on `can_get_metadata`, so a caller is shown exactly the warehouses they may see.
    if segments:
        response, names = await _drain_namespaces(ns, segments)
    else:
        response = ListNamespacesResponse(namespaces=[])
        names = await run_in_threadpool(partial(warehouses.served_ids, settings.registry_root, settings.storage_options(), estate=settings.estate_warehouse))

    if (authz := decisions.authorization(settings, client, token)) is not None:
        response.namespaces, response.page_token = await visible_pages.authorized_page(
            authz,
            resolver,
            relation="can_get_metadata",
            kind="namespace",
            candidates=[(name, [*segments, name]) for name in names],
            page_token=page_token,
            limit=limit,
        )
        return response
    response.namespaces, response.page_token = paginate(names, page_token, limit)
    return response


@spec_router.post("/{id}/describe", response_model_exclude_none=True)
def describe_namespace(id: str, ns: NamespaceDep, body: DescribeNamespaceRequest | None = None) -> DescribeNamespaceResponse:
    """Return the metadata/properties of namespace ``id`` via ``describe_namespace``."""
    segments = reconcile_body_id(parse_identifier(id), body.id if body else None)
    req = DescribeNamespaceRequest(id=segments)
    return native.call(ns, "describe_namespace", req)


async def _trash_subtree(
    ns: LanceNamespace,
    settings: CatalogStore,
    token: Caller | None,
    *,
    segments: list[str],
    descendants: list[objects.GovernedObject],
    own: ControlUuids,
    grace_days: float,
) -> list[objects.GovernedObject]:
    """DETACH the subtree instead of destroying it — the recoverable half of a cascade (#96); the tables it detached.

    A trash record pointing at bytes the native cascade already deleted would be a LIE: undrop
    re-registers still-present bytes, and `drop_namespace(cascade)` destroys its children inside one
    native call. So with a grace period the cascade never reaches that call. Instead, exactly what
    the single-table drop door does, per child: every descendant table is DEREGISTERED (pointer
    detached, bytes untouched) and filed in the trash (:func:`_trash_listed_table`); every namespace — children
    deepest-first, then the cascaded root — is then empty and dropped plainly, filed as a ``kind="namespace"``
    record so undrop knows which manifest rows to rebuild. All records share one drop-time stamp
    rule: ``expires_at`` is data on the record, never policy read at expiry.

    Not atomic, deliberately: a mid-loop failure leaves every already-detached child RECOVERABLE
    (its record is filed and its parent namespace still exists), which is strictly safer than the
    all-or-nothing native destroy it replaces.

    Each record is filed under its object's uuid, names its parent namespace's uuid, and carries the uuid of the
    namespace the cascade dropped (``cascade``), so the namespace undrop restores exactly this cascade's records, never
    another object dropped under one of their names ([[LH-525]]). ``descendants`` and ``own`` are what the door read
    while the subtree stood, and ``grace_days`` the window its warehouse's delete profile gives (ADR 0146). A listed
    table that left before its turn is detached by no one and is not among the tables answered.

    Raises:
        InternalError: The dropped namespace keeps no uuid, which no door leaves.
    """
    so = settings.storage_options()
    dropped_by = f"user:{token.subject}" if token is not None else None
    if own.object_uuid is None:
        raise InternalError(f"namespace {fga_ids.canonical_object_id(segments)!r} keeps no uuid, so its trash cannot be filed")
    cascade = own.object_uuid
    uuid_at = {tuple(item.segments): item.uuid for item in descendants} | {tuple(segments): cascade}

    def parent_of(child: list[str]) -> str | None:
        return uuid_at.get(tuple(child[:-1]), own.namespace_uuid)

    # Deepest-first, so every namespace is empty by the time its own drop runs.
    child_namespaces = sorted((item.segments for item in descendants if item.kind == "namespace"), key=len, reverse=True)
    tables = [item for item in descendants if item.kind == "table"]
    # EVERY NAME RESOLVED FIRST, CONCURRENTLY (catalog-api-11): independent reads whose results are only collected, so a
    # subtree of N tables pays no N sequential round trips before its first record is filed. Each table is then
    # detached in turn under the claim its resolution read, so one that changed since answers 409 at its fence.
    claims = table_claims.claim_store(settings)
    resolved_all = await asyncio.gather(*(run_in_threadpool(table_claims.resolve, ns, claims, item.segments) for item in tables))
    detached: list[objects.GovernedObject] = []
    for item, resolved in zip(tables, resolved_all, strict=True):
        trashed = await _trash_listed_table(
            ns,
            settings,
            item,
            resolved=resolved,
            claims=claims,
            namespace_uuid=parent_of(item.segments),
            cascade=cascade,
            dropped_by=dropped_by,
            grace_days=grace_days,
        )
        if trashed:
            detached.append(item)
    for child in [*child_namespaces, segments]:
        # Read before the drop removes the `__manifest` row that is their only copy.
        described = await run_in_threadpool(native.call, ns, "describe_namespace", DescribeNamespaceRequest(id=child))
        # The backend drops by name, so the namespace the walk listed is asked for in what it describes, the last read
        # before the drop: one created under the name since would be dropped, and filed under the listed one's uuid.
        hierarchy.require_standing_as_identified(child, identified=uuid_at[tuple(child)], standing=uuid_of(described.properties))
        await run_in_threadpool(native.call, ns, "drop_namespace", DropNamespaceRequest(id=child))
        record = trash.make_record(
            fga_ids.canonical_object_id(child),
            uuid=uuid_at[tuple(child)],
            location="",
            dropped_by=dropped_by,
            grace_days=grace_days,
            kind="namespace",
            namespace_uuid=parent_of(child),
            cascade=cascade,
            properties=described.properties,
        )
        await run_in_threadpool(trash.put, settings.registry_root, so, record)
    return detached


async def _trash_listed_table(
    ns: LanceNamespace,
    settings: CatalogStore,
    item: objects.GovernedObject,
    *,
    resolved: table_claims.ResolvedTable,
    claims: location_claims.ClaimStore,
    namespace_uuid: str | None,
    cascade: str,
    dropped_by: str | None,
    grace_days: float,
) -> bool:
    """Detach one table a recoverable cascade listed and file it in the trash, under the fence the drop door holds its
    table by; whether this cascade detached it.

    ``resolved`` is what the table's name resolved to once the cascade had listed it, and its claim is fenced under
    that reading (:func:`table_claims.fence_listed`, [[LH-586]]), so a rename landing while the table goes answers 409
    rather than leaving a trash record on a table that lives on under another name. ``False`` for a table that left
    the namespace since the listing: no record is filed for it.

    RECORD FIRST, DETACH SECOND, as at the table door (diff2 F6 leg a): a crash between the two leaves a record on a
    live table, which the purge refuses ``STILL_REGISTERED`` and a retry overwrites, where the other order left bytes
    no undrop, purge or retry could reach. The trashed table keeps its location's claim until its undrop or the purge
    ([[LH-204]]); only the fence is lifted, whether the detach landed or failed.

    Raises:
        ConcurrentModificationError: The table changed, or another request is acting on it; nothing of it changed here.
        TableNotFoundError: The row went between the fence and the detach, by a hand the fence does not bind. Unlike the
            destroying cascade, this one has already filed its record, so it answers rather than skip the table: the
            record stays, and the call issued again lists only what still stands.
    """
    fenced = await run_in_threadpool(partial(table_claims.fence_listed, ns, claims, resolved, listed_uuid=item.uuid, verb="drop"))
    if fenced is None:
        return False
    record = trash.make_record(
        fga_ids.canonical_object_id(item.segments),
        uuid=item.uuid,
        location=fenced.location,
        dropped_by=dropped_by,
        grace_days=grace_days,
        namespace_uuid=namespace_uuid,
        cascade=cascade,
    )
    try:
        await run_in_threadpool(trash.put, settings.registry_root, settings.storage_options(), record)
        await run_in_threadpool(native.call, ns, "deregister_table", DeregisterTableRequest(id=item.segments))
    finally:
        await run_in_threadpool(table_claims.lift_fence, claims, fenced)
    return True


async def _destroy_subtree(
    ns: LanceNamespace,
    segments: list[str],
    descendants: list[objects.GovernedObject],
    *,
    root_uuid: str,
    registry: base_registry.BaseRegistry,
    claims: location_claims.ClaimStore,
    storage_options: StorageOptions,
    estate_roots: tuple[str, ...],
    dropped_by: str | None = None,
) -> AsyncIterator[list[str]]:
    """Destroy the subtree BOTTOM-UP, ourselves (#117), yielding each table as it goes.

    `drop_namespace(behavior=Cascade)` is not implemented by the `dir` backend the chart runs — it
    answers `NamespaceNotEmpty` for EVERY casing (probed directly against the library). So the
    destructive cascade this door has always documented never happened: with the shipped default
    (`LANCE_TRASH_GRACE_DAYS=0`) a non-empty namespace could not be dropped at all, forced or not,
    and `purge=true` — the documented destroy-now opt-out — was equally dead. Three guarded loops
    below it (the force-path protection clear, the descendant revoke, the descendant protection
    sweep) were unreachable code on that backend.

    The fix is the same shape as `_trash_subtree`'s, minus the trash records: tables first (each one's bytes are
    deleted at the location it is described at, by `tables.destroy.destroy_table`, [[LH-532]] — this is the destructive
    path by definition), then namespaces
    deepest-first so each is empty when its own drop runs, then the cascaded root. Not atomic, and
    deliberately so: a mid-loop failure leaves a SMALLER subtree that the same call can finish on a
    retry, which is strictly better than the all-or-nothing native call it replaces (that one simply
    refused). An already-absent child is tolerated: drift, not an error.

    Each destroyed table's base record goes with its bytes ([[LH-279]]): left behind, it would sanction its bases for
    the next table registered there. Its location claim goes too ([[LH-204]]), or the location stays held for the
    claim's lease.

    Each table is yielded once this cascade has removed it, so the caller announces it before the next one goes
    ([[LH-495]]): the retry lists only what still stands, so a table the first attempt destroyed is announced then or
    not at all. A listed table that left before its turn (:func:`_destroy_listed_table`) is yielded by no one: its own
    drop announced and ended it, or a rename carried it out alive, and the caller ends only what was yielded.

    A table's residue record, filed while its bytes are deleted, names its uuid and its parent namespace's: the root's,
    ``root_uuid``, or the descendant namespace's it sits in.
    """
    uuid_at = {tuple(item.segments): item.uuid for item in descendants} | {tuple(segments): root_uuid}
    tables = [item for item in descendants if item.kind == "table"]
    child_namespaces = sorted((item.segments for item in descendants if item.kind == "namespace"), key=len, reverse=True)
    # SEQUENTIAL, DELIBERATELY (catalog-api-11 asked about this loop and the answer is no). Every call
    # here DESTROYS bytes, and the ordered partial progress is the property the docstring above trades
    # for: a mid-loop failure must leave a strictly SMALLER subtree that the same call finishes on a
    # retry, and the namespace drops must run deepest-first so each is empty when its own drop lands.
    # Concurrency buys latency on a path nobody is waiting on and costs both of those.
    #
    # [[LH-204]] BEFORE ANYTHING IS DESTROYED, every table's location is checked against its claim: a
    # location another live table holds refuses the whole cascade, as a protected descendant does. So is every table a
    # registered clone outside the subtree reads (ADR 0198 §1), and the clones inside it go before their sources.
    located: dict[tuple[str, ...], str] = {}
    for item in tables:
        with suppress(TableNotFoundError):
            doomed: DescribeTableResponse = await run_in_threadpool(native.call, ns, "describe_table", DescribeTableRequest(id=item.segments))
            if doomed.location:
                await run_in_threadpool(table_claims.require_no_other_holder, ns, claims, doomed.location, table=fga_ids.canonical_object_id(item.segments))
                located[tuple(item.segments)] = doomed.location
    carried = {
        key: await run_in_threadpool(partial(destroy.clone_readers, location, storage_options, table=fga_ids.canonical_object_id(list(key))))
        for key, location in located.items()
    }
    try:
        order = clone_sources.destroy_order({key: clone_sources.tag_of(location) for key, location in located.items()}, carried)
    except clone_sources.ReadFromOutsideError as exc:
        log.warning(
            "cascade_refused_clone_sources",
            extra={"namespace": segments, "read": {fga_ids.canonical_object_id(list(k)): sorted(v) for k, v in exc.read.items()}},
        )
        raise InvalidTableStateError(
            f"namespace {fga_ids.canonical_object_id(segments)} cannot be destroyed: {len(exc.read)} table(s) in it are read by registered clones outside "
            "it; drop or deregister those clones first, or drop the namespace recoverably"
        ) from exc
    tables = sorted(tables, key=lambda item: order.index(tuple(item.segments)) if tuple(item.segments) in located else len(order))
    for item in tables:
        removed = await _destroy_listed_table(
            ns,
            item,
            namespace_uuid=uuid_at.get(tuple(item.segments[:-1])),
            registry=registry,
            claims=claims,
            storage_options=storage_options,
            estate_roots=estate_roots,
            dropped_by=dropped_by,
        )
        if removed:
            yield item.segments
    for child in child_namespaces:
        with suppress(NamespaceNotFoundError):
            await run_in_threadpool(native.call, ns, "drop_namespace", DropNamespaceRequest(id=child))
    # The root by name, once the name is asked to hold it still: the walk took the time a drop and a create need. A root
    # already gone is the drift the drops above tolerate; one another namespace replaced is refused.
    standing = await run_in_threadpool(object_uuids.resolve_namespace_uuid, ns, segments)
    if standing is not None:
        hierarchy.require_standing_as_identified(segments, identified=root_uuid, standing=standing)
    with suppress(NamespaceNotFoundError):
        await run_in_threadpool(native.call, ns, "drop_namespace", DropNamespaceRequest(id=segments))


async def _destroy_listed_table(
    ns: LanceNamespace,
    item: objects.GovernedObject,
    *,
    namespace_uuid: str | None,
    registry: base_registry.BaseRegistry,
    claims: location_claims.ClaimStore,
    storage_options: StorageOptions,
    estate_roots: tuple[str, ...],
    dropped_by: str | None,
) -> bool:
    """Destroy one table a cascade listed, under the fence the drop door holds its table by; whether this cascade removed it.

    The table's name is resolved again as its turn comes and its claim fenced (:func:`table_claims.fence_listed`,
    [[LH-586]]), so the bytes deleted are those of the row removed ([[LH-532]]) and a rename landing while the table
    goes answers 409 rather than carrying it out of a cascade that then ends its grants. ``False`` for a table that
    left the namespace since the listing: it is neither destroyed, announced nor ended here.

    A clone's ending is filed once every judgement has passed, right before its row goes (ADR 0198 §1), so a refusal
    files none: a tag release failing afterwards is finished by maintenance's audit, where a re-issued cascade would no
    longer list the clone it belongs to. One filed before a failure stays, for the finish to judge.

    Raises:
        ConcurrentModificationError: The table changed, or another request is acting on it; nothing of it changed here.
        TableBytesLeftError: Its row is gone and bytes remain, so its claim stays held and fenced, as at the drop door.
    """
    child = item.segments
    canonical = fga_ids.canonical_object_id(child)
    resolved = await run_in_threadpool(table_claims.resolve, ns, claims, child)
    fenced = await run_in_threadpool(partial(table_claims.fence_listed, ns, claims, resolved, listed_uuid=item.uuid, verb="drop"))
    if fenced is None:
        return False
    filed: list[CloneEnding] = []

    def _file_the_ending() -> None:
        found = clone_endings.file_ending(registry, claims, fenced.location, table=canonical, forget_record=True, door="namespace_cascade", filing=fenced.token)
        if found is not None:
            filed.append(found)

    try:
        await run_in_threadpool(
            partial(
                destroy.destroy_table,
                ns,
                storage_options,
                child,
                location=fenced.location,
                estate_roots=estate_roots,
                table_uuid=item.uuid,
                namespace_uuid=namespace_uuid,
                control=claims,
                dropped_by=dropped_by,
                before_removal=_file_the_ending,
            )
        )
    except destroy.TableBytesLeftError:
        raise
    except TableNotFoundError:
        # The row went by a hand the fence does not bind, so the table is not this cascade's to announce or end. An
        # ending filed before it stays, for the finish to judge; the fence is lifted, as the drop door lifts its own.
        await run_in_threadpool(table_claims.lift_fence, claims, fenced)
        return False
    except Exception:
        # The row stands, so the fence is lifted for the re-issue that finishes the cascade.
        await run_in_threadpool(table_claims.lift_fence, claims, fenced)
        raise
    # The base record goes with the bytes ([[LH-279]]) and the location's claim with the row ([[LH-204]]); the clone's
    # ending last. The claim is released under the uuid this cascade listed the table by, as well as under its fence: a
    # table's bytes can take longer to go than a fence stands, and the same name registered at the location once the
    # fence has lapsed holds the claim under a uuid of its own.
    await run_in_threadpool(partial(base_registry.forget_base_record, registry, fenced.location, uuid=item.uuid))
    await run_in_threadpool(partial(location_claims.release, claims, fenced.location, canonical, uuid=item.uuid))
    if filed:
        await run_in_threadpool(clone_endings.clear_ending, claims, filed[0])
    return True


async def _identify(ns: LanceNamespace, settings: CatalogStore, descendants: list[tuple[str, list[str]]]) -> list[objects.GovernedObject]:
    """Every descendant a cascade is about to remove, with its uuid, read while it still stands.

    CONCURRENT (catalog-api-11): one independent read per descendant on the pre-flight of a cascade, so its latency
    does not grow with the subtree. Enumeration order is kept, so a refusal names the FIRST protected descendant.
    """
    return list(
        await asyncio.gather(
            *(objects.standing(ns, settings, "table" if resource == "table" else "namespace", segments=child) for resource, child in descendants)
        )
    )


async def _require_descendants_unprotected(settings: CatalogStore, descendants: list[objects.GovernedObject], *, force: bool) -> None:
    """Refuse a cascade that would destroy a PROTECTED descendant, naming the first one found.

    The guard at the named rung only sees the id in the URL; a cascade's unit of destruction is the
    whole subtree, and those children are destroyed inside one native call that never reaches a door.
    So the check belongs here, where the destroyed set is actually enumerated.
    """
    if force or not descendants:
        return
    records = await asyncio.gather(*(run_in_threadpool(protections.read, settings, item.kind, item.uuid) for item in descendants))
    for item, record in zip(descendants, records, strict=True):
        hierarchy.require_not_protected(record is not None and record.protected, kind=item.kind, obj_id=fga_ids.canonical_object_id(item.segments), force=False)


@spec_router.post("/{id}/drop", response_model_exclude_none=True)
async def drop_namespace(
    *,
    id: str,
    keyed: idem.KeyedDoorDep,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    announcer: AnnouncerDep,
    uuids: ObjectUuidsDep,
    resolver: ResolverDep,
    body: DropNamespaceRequest | None = None,
    force: RaskFlag = False,
    purge: RaskFlag = False,
) -> DropNamespaceResponse:
    """Drop namespace ``id`` (``drop_namespace``); revoke its FGA tuples — and, for a Cascade drop, every
    dropped child's — so a reused id can't inherit stale grants.

    Deletion protection (#73): a ``protected`` control-root record refuses 409 unless ``force=true``.
    A CASCADE is checked against the whole SUBTREE before the native call — its children are destroyed
    inside that one call and never reach this door, so a protected table under a cascaded namespace
    would otherwise die silently. ``force`` turns the protection lock only; the FGA gate ran first.

    With a grace period configured (#96), a CASCADE becomes RECOVERABLE: the subtree is detached —
    tables deregistered, namespaces emptied then dropped — with a trash record per destroyed object,
    and ``POST /management/v1/namespace/{id}/undrop`` rebuilds the whole subtree. ``purge=true`` is the same
    explicit opt-out the table door has. A plain RESTRICT drop stays destructive-but-cheap: the
    namespace it removes is empty by definition, and re-creating an empty namespace needs no trash."""
    segments = parse_identifier(id)
    if not segments:
        # Refused before anything is enumerated: a cascade of the root is every default-root table.
        raise InvalidInputError("the root namespace cannot be dropped")
    req = body or DropNamespaceRequest()
    # TWO ORTHOGONAL FIELDS, and this door read only one. `behavior` (below) decides what happens to
    # the CONTENTS; `mode` decides what happens when the namespace IS NOT THERE — "Fail (default):
    # the server must return 400 … Skip: the server must return 204 indicating the drop operation has
    # succeeded". Measured 2026-09-13 against a real `dir` backend: it raises NamespaceNotFound for
    # EVERY mode, `Skip` included, so the semantics have to be supplied here.
    #
    # `Skip` is the IDEMPOTENCY lever rather than a status-code nicety: it is how a client makes a
    # drop safe to retry, so a namespace a previous attempt already removed counts as success.
    #
    # BOTH ARE PARSED BEFORE THE PROTECTION READ, because both are closed sets and a value outside
    # either is refused as InvalidInput by the parse. That refusal is a shape answer: it costs no round
    # trip, and an outage of the control root must not answer 503 for a request malformed on its face.
    drop_mode = DropMode.parse(req.mode)
    # A Cascade drop (behavior=Cascade; case-insensitive per the lance spec) removes all child tables +
    # nested namespaces from storage. Their FGA grants must be revoked too, or a later object reusing a
    # child id would inherit the stale owner/reader/writer tuples (privilege bleed). Enumerate the
    # descendants BEFORE the drop (afterwards they can't be listed); only when FGA is on (else the revoke
    # loop is a no-op and the listing is wasted work). Restrict (the dir-backend default) errors on a
    # non-empty namespace, so there are never extra tuples to revoke on that path.
    cascade = DropBehavior.parse(req.behavior) is DropBehavior.CASCADE
    req.id = reconcile_body_id(segments, req.id)
    # As the table doors (`catalog.api.idempotency`): a replay under the caller's key answers the first attempt and re-enters
    # nothing. Its claim captures no namespace, so a second run cannot tell the namespace from one created under the name
    # since, which a cascade would remove: a resend taking over a lost drop is told its outcome is unknown.
    converge = await keyed.begin(target=fga_ids.canonical_object_id(segments), takeover="unknown")
    if converge.replay is not None:
        return DropNamespaceResponse.model_validate(converge.replay.body)
    identified = await _identify_the_drop(
        ns,
        _DropTarget(segments=segments, cascade=cascade, drop_mode=drop_mode, purge=purge, force=force),
        resolver=resolver,
        settings=settings,
        converge=converge,
    )
    if isinstance(identified, DropNamespaceResponse):
        return identified
    plan = await _plan_the_drop(identified, uuids=uuids, settings=settings, announcer=announcer, token=token)
    async with announcer.announcing(
        door="drop_namespace", target=identified.dropped.fga_object(), announcements=[*plan.dropped_tables.values(), plan.dropped_namespace]
    ) as filing:
        if plan.recoverable:
            removal = await _trash_the_cascade(ns, plan, settings=settings, token=token)
        elif cascade:
            # The dir backend cannot cascade (#117) — we enumerate and destroy bottom-up ourselves, announcing each table as
            # it goes ([[LH-495]]): the retry after a failure part-way lists only what still stands.
            removal = await _destroy_announcing_each_table(ns, plan, settings=settings, token=token, filing=filing)
        else:
            restricted = await _drop_restrict(ns, req, drop_mode=drop_mode, converge=converge)
            if restricted.skipped:
                return restricted.response
            # A Restrict drop removes no table itself.
            removal = _CascadeRemoval(response=restricted.response, removed_tables=frozenset())
        removed = [item for item in identified.descendants if item.kind == "namespace" or tuple(item.segments) in removal.removed_tables]
        if plan.recoverable:
            await _settle_a_recoverable_drop(plan, removed, settings=settings, filing=filing)
        else:
            await _end_what_a_destroying_drop_removed(plan, removed, settings=settings, client=client, token=token)
        # Sent with the count of what this drop removed, under the id the planned event was filed with.
        await filing.send(
            plan.dropped_namespace.model_copy(
                update={
                    "message": ControlMessage(
                        event=plan.planned.model_copy(update={"extra": {**plan.planned.extra, "descendants_revoked": 0 if plan.recoverable else len(removed)}})
                    )
                }
            )
        )
    await converge.remember(200, removal.response)
    return removal.response


class _DropTarget(BaseModel):
    """What a drop request asks of the namespace it names."""

    model_config = ConfigDict(frozen=True)

    segments: list[str]
    cascade: bool
    drop_mode: DropMode
    purge: bool
    force: bool


class _IdentifiedDrop(BaseModel):
    """A drop's namespace and the subtree a cascade removes, as the door identified them."""

    model_config = ConfigDict(frozen=True)

    target: _DropTarget
    dropped: objects.GovernedObject
    descendants: list[objects.GovernedObject]


class _DropPlan(BaseModel):
    """What an identified drop announces, and whether the trash keeps what it removes."""

    model_config = ConfigDict(frozen=True)

    drop: _IdentifiedDrop
    own: ControlUuids
    grace: float
    recoverable: bool
    dropped_tables: dict[tuple[str, ...], Announcement]
    planned: CatalogControlEvent
    dropped_namespace: Announcement


async def _identify_the_drop(
    ns: LanceNamespace, target: _DropTarget, *, resolver: Resolver, settings: Settings, converge: idem.Converge
) -> _IdentifiedDrop | DropNamespaceResponse:
    """The namespace and subtree ``target`` drops, judged unprotected; a ``Skip`` of a namespace not there is answered."""
    # The namespace the request was authorized on, with the uuid its records are kept under (ADR 0173 §8). A `Skip` of a
    # namespace that is not there answers here, before anything is enumerated.
    try:
        dropped = await objects.identify_namespace(resolver, ns, target.segments)
    except NamespaceNotFoundError:
        if target.drop_mode is not DropMode.SKIP:
            raise
        return await _skipped(converge)
    await hierarchy.require_namespace_unprotected(settings, dropped, force=target.force)
    descendants: list[objects.GovernedObject] = []
    # Enumerated whenever a CASCADE is asked for — not only when FGA is on, as it used to be. A cascade
    # destroys children INSIDE the single native call, so they never re-enter this door: without this
    # list, a protected table under a cascade-dropped namespace died silently while the docstring
    # claimed protection covered it. The tuple-revoke below consumes the same list when FGA is on.
    if target.cascade:
        # A CASCADE TOUCHES EXISTENCE EARLIER than the drop itself — it enumerates the subtree first —
        # so a `Skip` guarding only the drop below would still raise for the caller most likely to be
        # retrying. Narrow on purpose: this covers the NAMED namespace being absent, never a descendant
        # vanishing mid-destroy, which is a real error and must not read as a skip.
        try:
            descendants = await _identify(ns, settings, await run_in_threadpool(_collect_descendants, ns, target.segments))
        except NamespaceNotFoundError:
            if target.drop_mode is not DropMode.SKIP:
                raise
            return await _skipped(converge)
        # Protection is a property of the SUBTREE, so it is checked against every id about to die, and
        # the refusal NAMES the protected descendant — "something in here is protected" is not an
        # answer anyone can act on. `force` turns this lock exactly as it does at the named rung.
        await _require_descendants_unprotected(settings, descendants, force=target.force)
    # The backend drops by name, so the name is asked once more, as late as the drop allows, to hold that namespace.
    await hierarchy.require_namespace_as_identified(ns, dropped)
    return _IdentifiedDrop(target=target, dropped=dropped, descendants=descendants)


async def _skipped(converge: idem.Converge) -> DropNamespaceResponse:
    """What a ``Skip`` drop of a namespace that is not there answers, remembered under the caller's key."""
    answer = DropNamespaceResponse()
    await converge.remember(200, answer)
    return answer


async def _plan_the_drop(drop: _IdentifiedDrop, *, uuids: ObjectUuids, settings: Settings, announcer: Announcer, token: Caller | None) -> _DropPlan:
    """The grace an identified drop is kept under and the announcements it files before removing anything."""
    # What every announcement below names the objects by, read while they still stand (ADR 0173 §10).
    own = await uuids.namespace(drop.target.segments)
    known = {tuple(item.segments): await uuids.table(item.segments, uuid=item.uuid) for item in drop.descendants if item.kind == "table"}
    # #96: with a grace period, a cascade DETACHES the subtree (trash record per child) instead of
    # destroying it inside the one native call — see _trash_subtree for why the native cascade and a
    # trash record cannot coexist. `purge=true` is the explicit destroy-now, as at the table door.
    grace = 0.0 if drop.target.purge or not drop.target.cascade else await trash_doors.drop_grace_days(settings, drop.target.segments)
    recoverable = grace > 0
    # The tables this cascade removes itself. A listed table that left before its turn ([[LH-586]]: a drop of its own,
    # or a rename that carried it out alive) is not among them, so no step below announces, tracks or ends it.
    # What the drop announces is filed before it removes anything, so a drop that fails after removing something still
    # announces it ([[LH-576]]): a drop marker per table it means to remove, each landed once that table is registered
    # under no name and the trash keeps it as this drop keeps it (`Unregistered`), and its own `namespace_dropped`, landed
    # once the namespace's name names it no longer. Its limits:
    # the store cannot tell which actor retired a table, so another's deregister or destroying drop after this door died
    # satisfies it too, and this door's event goes out as well, under its own ids.
    # And a recoverable cascade files a table's trash record before its detach (`_trash_listed_table`) and has no stamp
    # to tell the two apart, so one failing between them leaves a record on a live table.
    dropped_tables = {
        tuple(item.segments): Announcement(
            evidence=Unregistered(segments=item.segments, uuid=item.uuid, location=item.location, trashed=recoverable),
            message=announcer.marker(
                item.segments,
                author=token.subject if token is not None else None,
                operation=DROP_TABLE,
                uuids=known.get(tuple(item.segments), DatasetUuids()),
            ),
        )
        for item in drop.descendants
        if item.kind == "table"
    }
    planned = CatalogControlEvent(
        action="namespace_dropped",
        object_type="namespace",
        object_id=drop.dropped.fga_object(),
        object_name=fga_ids.canonical_object_id(drop.target.segments),
        object_uuid=own.object_uuid,
        namespace_uuid=own.namespace_uuid,
        actor=f"user:{token.subject}" if token else None,
        extra={"cascade": drop.target.cascade, "recoverable": recoverable, "descendants_revoked": 0 if recoverable else len(drop.descendants)},
    )
    dropped_namespace = Announcement(
        evidence=NamespaceResolves(segments=drop.target.segments, uuid=drop.dropped.uuid, held=False), message=ControlMessage(event=planned)
    )
    return _DropPlan(
        drop=drop,
        own=own,
        grace=grace,
        recoverable=recoverable,
        dropped_tables=dropped_tables,
        planned=planned,
        dropped_namespace=dropped_namespace,
    )


class _CascadeRemoval(BaseModel):
    """What a cascade answered, and the tables it removed itself."""

    model_config = ConfigDict(frozen=True)

    response: DropNamespaceResponse
    removed_tables: frozenset[tuple[str, ...]]


async def _trash_the_cascade(ns: LanceNamespace, plan: _DropPlan, *, settings: Settings, token: Caller | None) -> _CascadeRemoval:
    """Detach a recoverable cascade's subtree into the trash: the answer, and the tables it removed."""
    drop = plan.drop
    # The cascade's records are keyed by the uuid the door identified the namespace by, not the best-effort read its
    # announcements name it by, which answers ``None`` on a failed describe.
    detached = await _trash_subtree(
        ns,
        settings,
        token,
        segments=drop.target.segments,
        descendants=drop.descendants,
        own=ControlUuids(object_uuid=drop.dropped.uuid, namespace_uuid=plan.own.namespace_uuid),
        grace_days=plan.grace,
    )
    removed_tables = frozenset(tuple(item.segments) for item in detached)
    # Long running, as the spec reads a `transaction_id`: the subtree's bytes are deleted later, by the purge, and
    # each table's deletion is tracked through DescribeTransaction as `<table id>$<its uuid>`, as at the table door.
    response = DropNamespaceResponse(transaction_id=[fga_ids.canonical_object_id([*item.segments, item.uuid]) for item in detached] or None)
    return _CascadeRemoval(response=response, removed_tables=removed_tables)


async def _destroy_announcing_each_table(ns: LanceNamespace, plan: _DropPlan, *, settings: Settings, token: Caller | None, filing: Filing) -> _CascadeRemoval:
    """Destroy a cascade's subtree bottom-up, sending each table's drop marker as it goes: the answer, and the tables removed."""
    drop = plan.drop
    removed_tables: set[tuple[str, ...]] = set()
    registry = base_registry.BaseRegistry(control_root=settings.registry_root, storage_options=settings.storage_options())
    async for table in _destroy_subtree(
        ns,
        drop.target.segments,
        drop.descendants,
        root_uuid=drop.dropped.uuid,
        registry=registry,
        claims=table_claims.claim_store(settings),
        storage_options=settings.storage_options(),
        estate_roots=(settings.registry_root, settings.root),
        dropped_by=f"user:{token.subject}" if token is not None else None,
    ):
        removed_tables.add(tuple(table))
        await filing.send(plan.dropped_tables[tuple(table)])
    response = DropNamespaceResponse()
    return _CascadeRemoval(response=response, removed_tables=frozenset(removed_tables))


class _RestrictDrop(BaseModel):
    """What a Restrict drop answered, and whether that answer is a ``Skip`` of a namespace not there."""

    model_config = ConfigDict(frozen=True)

    response: DropNamespaceResponse
    skipped: bool


async def _drop_restrict(ns: LanceNamespace, req: DropNamespaceRequest, *, drop_mode: DropMode, converge: idem.Converge) -> _RestrictDrop:
    """A Restrict drop of the namespace ``req`` names, and whether it answered a ``Skip`` of one that is not there."""
    try:
        response = await run_in_threadpool(native.call, ns, "drop_namespace", req)
    except NamespaceNotFoundError:
        if drop_mode is not DropMode.SKIP:
            raise
        return _RestrictDrop(response=await _skipped(converge), skipped=True)
    return _RestrictDrop(response=response, skipped=False)


async def _settle_a_recoverable_drop(plan: _DropPlan, removed: list[objects.GovernedObject], *, settings: Settings, filing: Filing) -> None:
    """Clear the protection a recoverable cascade ends and send each detached table's drop marker."""
    # The record dies with the object — a reused id must not inherit protection nobody set on it. A recoverable cascade
    # clears its namespace's here, and its DESCENDANTS' only under force=true (an unforced cascade already proved none
    # exist); every other drop ends them with the rest of what each removed object holds.
    await run_in_threadpool(protection.clear_protection, settings.registry_root, settings.storage_options(), "namespace", uuid=plan.drop.dropped.uuid)
    if plan.drop.target.force:
        for item in removed:
            await run_in_threadpool(protection.clear_protection, settings.registry_root, settings.storage_options(), item.kind, uuid=item.uuid)
    # RECORD THE DELETION OF EVERY TABLE THIS CASCADE DETACHED, exactly as the single-table door does (a `drop_table`
    # event per table); a destroyed subtree announced each table as it went. A follower of the feed learns a table
    # is gone only from these events ([[LH-144]]), so a table nothing records stays indistinguishable from a live
    # one. Emitted for a recoverable drop too, matching that door — the undrop emits a `register_table` marker per
    # table, which a follower reads as the table existing again.
    for item in removed:
        if item.kind == "table":
            await filing.send(plan.dropped_tables[tuple(item.segments)])


async def _end_what_a_destroying_drop_removed(
    plan: _DropPlan, removed: list[objects.GovernedObject], *, settings: Settings, client: OpenFgaClient | None, token: Caller | None
) -> None:
    """End what each object a destroying drop removed holds, the descendants first, then the namespace's own."""
    # End what each removed object holds AFTER the drop commits, so a failed or Restrict-refused drop leaves all of it in
    # place: the descendants first, found by the catalog's listing and by the `child` edges a failed earlier attempt
    # left, each one's protection and maintenance policy before its grants, then the namespace's own. NOT on a
    # recoverable cascade (#96, the #75 rule): undrop rebuilds the subtree, and each object must come back configured
    # as it was and reachable by the owner, the one person who needs to undrop it. Those die with the bytes — at
    # purge, or when the sweep's expiry reclaims the trash.
    async for _ in object_grants.end_removed_subtree(client, settings, root_uuid=plan.drop.dropped.uuid, removed=removed, token=token):
        pass


@management_router.get("/{id}/tasks", response_model_exclude_none=True)
async def namespace_tasks(
    id: str,
    resolver: ResolverDep,
    uuid: TrashedUuidQuery = None,
) -> list[TrashEntry]:
    """What is queued for THIS namespace — a pending trash expiry after a recoverable cascade (#96).
    Same contract as the table door: an undrop deadline the owner cannot see is not a safety
    feature. Reader-gated by the router (``tasks`` is a metadata read). One entry per dropped
    namespace that held the name and that the caller may see, each with its uuid ([[LH-525]])."""
    return [trash_doors.entry(record) for record in await resolver.admitted_trash("namespace", parse_identifier(id))]


@management_router.post("/{id}/undrop", response_model_exclude_none=True)
async def undrop_namespace(
    *,
    id: str,
    keyed: idem.KeyedDoorDep,
    ns: NamespaceDep,
    root: NamespaceRootDep,
    settings: SettingsDep,
    token: CurrentToken,
    announcer: AnnouncerDep,
    resolver: ResolverDep,
    uuid: TrashedUuidQuery = None,
) -> CreateNamespaceResponse:
    """Recover a cascade-dropped SUBTREE from the trash (#96) — the plural undrop.

    The unit of a cascade is the subtree, so the unit of its recovery is too: every trashed
    namespace under (and including) ``id`` is re-created shallowest-first, then every trashed table
    is re-registered at its old id from its still-present bytes, and each record is cleared as its
    object recovers. Owner-gated like the table door (``undrop`` maps to the delete rung).

    Resumable, not atomic: namespace creates run ``exist_ok`` and an already-registered table is
    treated as recovered, so a rerun after a mid-recovery failure finishes the job instead of
    409-ing on what the first attempt already rebuilt. 404 when there is no trash record: an expired
    or never-trashed drop is genuinely unrecoverable, and saying so beats a 200 that recovers
    nothing. A declared-only table (empty recorded location) is skipped with a warning — there were
    no bytes to lose.

    THE UNDROP RESTORES ONE CASCADE ([[LH-525]], ADR 0173 §9). A name is free once its namespace is dropped, so the door
    restores the namespace its caller names (``?uuid=`` when several held the name, 409 listing each one's uuid and
    deadline otherwise) and what the cascade that dropped it filed under it, each object under its own uuid. While
    another namespace holds the name, or the parent's name is held by a namespace other than the one it was dropped
    from, it answers 409."""
    segments = parse_identifier(id)
    canonical = fga_ids.canonical_object_id(segments)
    # As the table doors (`catalog.api.idempotency`): a replay under the caller's key answers the first attempt and re-enters nothing.
    converge = await keyed.begin(target=canonical, takeover="rerun")
    if converge.replay is not None:
        return CreateNamespaceResponse.model_validate(converge.replay.body)
    so = settings.storage_options()
    restoring = await _the_drop_to_restore(resolver, segments=segments, canonical=canonical)
    record, root_uuid = restoring.record, restoring.root_uuid
    # Read whole, as the name's own records were (``trash.named``): a record that cannot be read may be one this cascade
    # filed, which the undrop would leave for the purge under a subtree its owner was told is restored ([[LH-572]]).
    everything = await run_in_threadpool(trash.every_record, settings.registry_root, so)
    # The records the cascade that dropped this namespace filed, at its name and under it: undropping a namespace that
    # cascade dropped below its own root restores that branch of it, once its parent stands again.
    cascade, prefix = record.cascade, canonical + CATALOG_DELIMITER
    subtree = [r for r in everything if r.cascade == cascade and (r.id == canonical or r.id.startswith(prefix))]
    # Shallowest first — a parent manifest row must exist before its children's create/register.
    namespace_records = sorted((r for r in subtree if r.kind == "namespace"), key=lambda r: r.id.count(CATALOG_DELIMITER))
    table_records = [r for r in subtree if r.kind == "table"]
    # Before anything is rebuilt: a residue record's table was destroyed, not trashed ([[LH-532]]).
    for t_rec in table_records:
        trash_doors.require_restorable(t_rec, name=t_rec.id)
    # What the undrop announces is filed before it rebuilds anything, as the table undrop files its own ([[LH-576]]): a
    # marker per table it re-registers, landed once the table's name names it again, and its own `namespace_undropped`,
    # landed once the namespace's name does. A restored table lineage never hears of is skipped by its feed for good
    # ([[LH-144]]), so an undrop that stops midway leaves each one to the finish.
    restored_tables = _restored_table_markers(table_records, announcer, author=token.subject if token is not None else None)
    declared_only = sum(1 for t_rec in table_records if not t_rec.location)
    restored_namespace = Announcement(
        evidence=NamespaceResolves(segments=segments, uuid=root_uuid, held=True),
        message=ControlMessage(
            event=CatalogControlEvent(
                action="namespace_undropped",
                object_type="namespace",
                object_id=fga_ids.namespace_object(root_uuid),
                object_name=canonical,
                object_uuid=root_uuid,
                namespace_uuid=record.namespace_uuid,
                actor=f"user:{token.subject}" if token is not None else None,
                extra={"namespaces": len(namespace_records), "tables": len(table_records) - declared_only, "declared_only_skipped": declared_only},
            )
        ),
    )
    async with announcer.announcing(
        door="undrop_namespace", target=fga_ids.namespace_object(root_uuid), announcements=[*restored_tables.values(), restored_namespace]
    ) as filing:
        response = await _rebuild_namespaces(ns, settings, namespace_records, canonical=canonical)
        async for restored in _reregister_tables(ns, settings, table_records, root=root):
            await filing.send(restored_tables[restored])
        # NO SEED: an undrop is a pure restore (owner ruling, diff2 F10 item 4b), on this door for the same reason as
        # the table door. The recoverable cascade kept every tuple, so the descendants' owners are intact, and a seed
        # would only add whoever pressed the button on the root of a subtree they may have had no claim to.
        await filing.send(restored_namespace)
    await converge.remember(200, response)
    return response


class _DropToRestore(BaseModel):
    """The trash record an undrop restores, and the uuid the restored namespace keeps."""

    model_config = ConfigDict(frozen=True)

    record: TrashRecord
    root_uuid: str


async def _the_drop_to_restore(resolver: Resolver, *, segments: list[str], canonical: str) -> _DropToRestore:
    """The trash record an undrop of ``segments`` restores and the uuid it keeps, once nothing refuses restoring it."""
    trashed = await resolver.admitted_trash("namespace", segments)
    if not trashed:
        raise NamespaceNotFoundError(f"no recoverable drop for namespace: {canonical}. The grace period may have expired, or the drop purged its subtree.")
    record = trash_doors.the_one_to_restore(trashed, kind="namespace", name=canonical)
    trash_doors.require_restorable(record, name=canonical)
    root_uuid = record.uuid
    if root_uuid is None:
        raise InternalError(f"the trash record of namespace {canonical!r} keeps no uuid, so its grants cannot be found")
    standing = await resolver.namespace_uuid(segments)
    if standing is not None and standing != root_uuid:
        raise NamespaceAlreadyExistsError(f"cannot undrop namespace {canonical!r}: another namespace holds the name now; drop it first")
    if len(segments) > 1:
        trash_doors.require_its_own_parent(record, standing=await resolver.namespace_uuid(segments[:-1]), name=canonical)
    return _DropToRestore(record=record, root_uuid=root_uuid)


def _restored_table_markers(table_records: list[TrashRecord], announcer: Announcer, *, author: str | None) -> dict[str, Announcement]:
    """The ``register_table`` marker an undrop files for each table it re-registers, by the table's id."""
    restored_tables: dict[str, Announcement] = {}
    for t_rec in table_records:
        # A declared-only table kept no bytes, so it is not re-registered and gets no marker.
        if not t_rec.location:
            continue
        if t_rec.uuid is None:
            raise InternalError(f"the trash record of table {t_rec.id!r} keeps no uuid, so its restore cannot be announced")
        t_segments = parse_identifier(t_rec.id)
        restored_tables[t_rec.id] = Announcement(
            evidence=NameResolves(segments=t_segments, uuid=t_rec.uuid),
            message=announcer.marker(
                t_segments,
                author=author,
                operation=REGISTER_TABLE,
                # The record's own, stamped by the cascade: the parent is still dropped here, so no describe names it.
                uuids=DatasetUuids(table_uuid=t_rec.uuid, namespace_uuid=t_rec.namespace_uuid),
                source_uri=t_rec.location,
            ),
        )
    return restored_tables


async def _rebuild_namespaces(ns: LanceNamespace, settings: CatalogStore, namespace_records: list[TrashRecord], *, canonical: str) -> CreateNamespaceResponse:
    """Re-create a cascade's namespaces shallowest first, clearing each record once its namespace stands again; the
    response of the namespace ``canonical`` names."""
    so = settings.storage_options()
    response = CreateNamespaceResponse()
    for n_rec in namespace_records:
        n_id = n_rec.id
        # THROUGH THE SHARED SEAM, because the `mode="exist_ok"` below is not honoured by the backend
        # and this loop's whole resumability rests on it: a rerun after a mid-recovery failure would
        # raise on the first namespace the previous attempt already rebuilt, abandoning the rest of the
        # subtree in the trash.
        #
        # THE TABLE LOOP BELOW ALREADY GETS THIS RIGHT — it catches `TableAlreadyExistsError` and logs
        # `undrop_table_already_registered` — so the docstring's "rerun finishes the job" was true of
        # tables and false of namespaces. And the false half gated the true one: namespaces are rebuilt
        # shallowest-FIRST, so a rerun aborted before it ever reached the tolerant loop.
        n_segments = parse_identifier(n_id)
        rebuilt = CreateNamespaceRequest(id=n_segments, mode="exist_ok", properties=n_rec.properties)
        created, _kept = await create_or_keep_namespace(ns, n_segments, rebuilt)
        if n_id == canonical:
            response = created
        # Clear only AFTER the create commits — a failed rebuild must leave the subtree recoverable.
        await run_in_threadpool(trash.clear, settings.registry_root, so, str(n_rec.uuid), kind="namespace")
    return response


async def _reregister_tables(ns: LanceNamespace, settings: CatalogStore, table_records: list[TrashRecord], *, root: str) -> AsyncIterator[str]:
    """Re-register each of a cascade's tables from its bytes and yield its id once it is registered, for its door to
    announce; its record is cleared after that. A table only declared held no bytes and is cleared unannounced."""
    so = settings.storage_options()
    for t_rec in table_records:
        t_id = t_rec.id
        location = t_rec.location
        if location:
            # The RELATIVE key register_table accepts, the one the record's URL decodes to under the root, as at the
            # table undrop ([[LH-517]]).
            t_segments = parse_identifier(t_id)
            body = RegisterTableRequest(id=t_segments, location=table_objects.relative_location(location, root=root))
            # The cascade kept each location's claim for its trashed id; another holder means another
            # table now resolves to these bytes, as at the table door ([[LH-204]]).
            try:
                kept = await run_in_threadpool(table_claims.take, ns, table_claims.claim_store(settings), location, table=t_id, segments=t_segments)
            except location_claims.LocationHeldError as held:
                log.warning("undrop_refused_held_location", extra={"table": t_id, "location": held.claim.location, "holder": held.claim.table})
                raise InvalidTableStateError(f"cannot undrop table {t_id}: its location is now held by another table") from None
            if kept.uuid != t_rec.uuid:
                raise InternalError(f"the claim on {t_id}'s location records another uuid than its trash record, so its grants cannot be found")
            try:
                await run_in_threadpool(native.call, ns, "register_table", body)
            except TableAlreadyExistsError:
                log.info("undrop_table_already_registered", extra={"table": t_id})
            await run_in_threadpool(warn_if_mixed_file_versions, location, so, table=t_id)
            yield t_id
        else:
            log.warning("undrop_skipped_declared_only_table", extra={"table": t_id})
        await run_in_threadpool(trash.clear, settings.registry_root, so, str(t_rec.uuid))


@management_router.get("/{id}/protection", response_model_exclude_none=True)
async def get_namespace_protection(*, id: str, ns: NamespaceDep, settings: SettingsDep, resolver: ResolverDep) -> ProtectionResponse:
    """Read the namespace's deletion-protection flag (#123) — the table door's read, one rung up."""
    segments = parse_identifier(id)
    canonical = fga_ids.canonical_object_id(segments)
    namespace = await objects.identify_namespace(resolver, ns, segments)
    record = await run_in_threadpool(protections.read, settings, "namespace", namespace.uuid)
    return ProtectionResponse(id=canonical, protected=record is not None and record.protected, set_by=record.set_by if record is not None else None)


@management_router.post("/{id}/protection", response_model_exclude_none=True)
async def set_namespace_protection(
    *,
    id: str,
    body: SetProtectionRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    control: ControlEmitterDep,
    resolver: ResolverDep,
) -> ProtectionResponse:
    """Set or clear deletion protection on namespace ``id`` (#73). Owner-gated by the router
    (``protection`` maps to ``can_set_protection``, held by the namespace's owner, so a writer cannot arm or disarm the
    safety on what it writes). Same control-root record contract as the table door."""
    segments = parse_identifier(id)
    canonical = fga_ids.canonical_object_id(segments)
    namespace = await objects.identify_namespace(resolver, ns, segments)
    await hierarchy.require_namespace_as_identified(ns, namespace)
    so = settings.storage_options()
    if body.protected:
        record = ProtectionRecord(
            kind="namespace", id=canonical, uuid=namespace.uuid, protected=True, set_by=f"user:{token.subject}" if token is not None else "anonymous"
        )
        await run_in_threadpool(protection.set_protection, settings.registry_root, so, record)
    else:
        await run_in_threadpool(protection.clear_protection, settings.registry_root, so, "namespace", uuid=namespace.uuid)
    await emit_control(
        control,
        action="namespace_protected" if body.protected else "namespace_unprotected",
        object_type="namespace",
        object_id=namespace.fga_object(),
        object_name=canonical,
        actor=f"user:{token.subject}" if token is not None else None,
        extra={},
    )
    return ProtectionResponse(id=canonical, protected=body.protected)


#: The spec gives `exists` a 200 with NO `content:` block — *"Success, no content"* — so a conforming
#: server sends an empty body. A handler annotated `-> None` sends `application/json` with the four
#: bytes `null`, which a strict client reads as a JSON document where the contract promised none.
#: `response_class=Response` is what makes FastAPI emit nothing; `status_code=200` alone does not.
@spec_router.post("/{id}/exists", status_code=200, response_class=Response)
def namespace_exists(id: str, ns: NamespaceDep, body: NamespaceExistsRequest | None = None) -> Response:
    """Check that namespace ``id`` exists via ``namespace_exists`` — 200 on success (spec 0.9), else error."""
    req = NamespaceExistsRequest(id=reconcile_body_id(parse_identifier(id), body.id if body else None))
    native.call(ns, "namespace_exists", req)
    return Response(status_code=200)


@spec_router.get("/{id}/table/list", response_model_exclude_none=True)
async def list_tables(
    *,
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    client: FgaClientDep,
    resolver: ResolverDep,
    limit: ListLimit,
    page_token: str | None = None,
    include_declared: bool = True,
) -> ListTablesResponse:
    """List the tables under namespace ``id``; when FGA is on, filtered to what the caller can
    ``can_read_data``. ``include_declared=false`` lists only the tables that hold a committed version, each judged by
    its own storage (:func:`catalog.api.listings.holding_a_version`, [[LH-528]]).

    **WHY THIS FILTERS AT ALL — the route's gate stopped implying its contents.** The router mounts
    every endpoint here under ``authorize``, which resolves this route's ``list`` action to
    ``can_get_metadata``; C1 (upward visibility) then redefined that on a namespace as
    ``reader or can_get_metadata from child``. The widening is correct for what it was for — without
    it a grantee could not resolve the breadcrumb to their own table and every list above it 403'd —
    but it means holding ``reader`` on ONE table opens this route, and the route used to answer with
    every SIBLING table's name. Measured against both compiled models: before ``14a84022`` the check
    was ``false`` (403); after it, ``true`` (200, full listing). A table list is not a harmless header:
    it names data someone may not know exists.

    So the split ``open_notifications.md`` §3.1 already settled applies verbatim: the ROUTE may open on
    ``can_get_metadata``, each ITEM is checked on the object itself. ``list_all_tables`` (``tables.py``)
    has always done this; that this route did not was the inconsistency, not the policy.

    **Pagination moved off the native call, for the reason it moved there too (#141).** A backend
    ``limit`` truncates BEFORE the filter, so ``limit=10`` could answer 2 and hand out a cursor that
    skips everything the filter removed — pages that silently drop tables the caller CAN read. The
    native call is therefore unpaginated and the keyset cursor is applied to the filtered list. The
    list is sorted and deduped first because the cursor is the last NAME of the previous page and is
    stable only over an ordered one.
    """
    segments = parse_identifier(id)
    response, names = await _drain_tables(ns, segments)
    candidates = [(name, [*segments, name]) for name in names]
    if not include_declared:
        candidates = await listings.holding_a_version(resolver, so, candidates)
    # With FGA on, keep only the tables the caller may actually read, each judged on the object its name resolves to
    # ([[LH-223]], [[LH-524]]).
    if (authz := decisions.authorization(settings, client, token)) is not None:
        response.tables, response.page_token = await visible_pages.authorized_page(
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
