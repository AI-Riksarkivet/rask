"""Namespace metadata endpoints."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import suppress
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Header, Request, Response
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
    DropTableResponse,
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
    PermissionDeniedError,
    RegisterTableRequest,
    ServiceUnavailableError,
    TableAlreadyExistsError,
    TableNotFoundError,
)
from openfga_sdk import OpenFgaClient

from catalog.api import fga_deps, listings
from catalog.api.dependencies import (
    ControlEmitterDep,
    FgaClientDep,
    LineageEmitterDep,
    NamespaceDep,
    NamespaceRootDep,
    ObjectUuidsDep,
    ResolverDep,
    SettingsDep,
    StorageOptionsDep,
    TrashedUuidQuery,
    namespace_for_root,
)
from catalog.api.pagination import ListLimit, paginate
from catalog.api.rask_params import RaskFlag
from catalog.api.security import Caller, CurrentToken
from catalog.core import tier_policy
from catalog.core.config import Settings
from catalog.core.formats import reject_unsupported_properties
from catalog.core.identifiers import MAX_NAMESPACE_DEPTH, UUID_PROPERTY, parse_identifier, reconcile_body_id, with_minted_uuid
from catalog.core.lineage_emit import DROP_TABLE, REGISTER_TABLE, LineageEmitter, UuidReader, emit_write_event

# `MAX_NAMESPACE_DEPTH` is IMPORTED, not redeclared: the point of F10 item 10 is that two walkers
# over the same tree disagreed about how deep it may go, and a second copy of the number would let
# them drift apart again the moment one is tuned.
from catalog.core.modes import CreateMode, DropBehavior, DropMode
from catalog.core.namespace import warn_if_mixed_file_versions
from catalog.core.store_endpoint import require_estate_store
from catalog.schemas import ProtectionResponse, SetProtectionRequest, TrashEntry
from catalog.services import dataplane, native, table_claims, trash_doors, warehouses
from service_kit.control_emit import emit_control
from service_kit.control_events import ControlUuids
from service_kit.governed import fga
from service_kit.lakehouse import base_registry, location_claims, protection, trash
from service_kit.lakehouse.naming import CATALOG_DELIMITER
from service_kit.lakehouse.objectfs import StorageOptions
from service_kit.openlineage import DatasetUuids


log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/namespace", tags=["namespace"])

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
        # fga.read_object_tuples' openfga_read_truncated warning. In practice unreachable at this ceiling.
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


@router.post("/{id}/create", response_model_exclude_none=True)
async def create_namespace(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
    resolver: ResolverDep,
    body: CreateNamespaceRequest | None = None,
) -> CreateNamespaceResponse:
    """Create a namespace via ``create_namespace``, then seed its FGA owner + parent edge."""
    # LANCE-ONLY (2026-08-15 ruling). A namespace carries no bytes, but its `properties` map is
    # where a client sets a DEFAULT format for the tables created under it — so accepting one here
    # would let the ruling be bypassed one level up from the door that enforces it.
    reject_unsupported_properties(body.properties if body else None)
    declared = tier_policy.declared_tier(body.properties if body else None)
    # WHAT `mode` MEANS HERE, from spec.yaml's CreateNamespaceRequest: "Create: the operation fails
    # with 409. ExistOk: the operation succeeds and the existing namespace is kept. Overwrite: the
    # existing namespace is dropped and a new empty namespace with this name is created." The backend
    # honours none of them (it answers NamespaceAlreadyExists for every mode), so this door supplies all
    # three. A value that is none of the three is refused by the parse itself, as InvalidInput naming
    # it — the closed-vocabulary rule `modes.py` states for every mode this catalog reads.
    create_mode = CreateMode.parse(body.mode if body else None)
    segments = parse_identifier(id)
    # A top-level namespace needs a warehouse to live in. This door cannot name one, so a NEW top-level
    # namespace is refused here and the caller is sent to the warehouse-scoped route; checked BEFORE the
    # native create, so a refusal leaves nothing half-made. Nested namespaces inherit their parent's
    # binding and pass. An Overwrite or an ExistOk of a top-level id that already has a binding passes too
    # ([[LH-416]]): the namespace it keeps or creates is where the binding routes it (`NamespaceDep` resolves
    # through it) and hangs off that binding's warehouse, so it belongs to a warehouse as the bound one does.
    binding: dict[str, str] | None = None
    if create_mode in (CreateMode.OVERWRITE, CreateMode.EXIST_OK) and settings.warehouses_enabled and len(segments) == 1:
        binding = await run_in_threadpool(warehouses.binding_for_namespace, settings.registry_root, settings.storage_options(), segments[0])
    if binding is None:
        fga_deps.require_warehouse_scoped(segments, warehouses_enabled=settings.warehouses_enabled)
    # And it must not nest deeper than the authz model can resolve. Both read walkers have capped depth
    # for a while; nothing capped CREATION, so the estate could be driven into a shape where
    # Check(can_get_metadata, warehouse:X) errors instead of answering — taking browsing down for the
    # whole bucket, its owners included. Shape rung, so 400, and before the native call.
    fga_deps.require_namespace_depth(segments)
    req = body or CreateNamespaceRequest()
    req.id = reconcile_body_id(segments, req.id)
    # [[LH-519]] A uuid for the namespace this request creates (ADR 0173 §2, §6). An ExistOk that keeps one answers
    # the kept namespace's own, and an Overwrite's replacement is a new namespace with a new one.
    req.properties = with_minted_uuid(req.properties)
    minted = req.properties[UUID_PROPERTY]
    await _judge_tier_declaration(ns, settings, token, client, segments, declared=declared, overwrite=create_mode is CreateMode.OVERWRITE)
    # RACE-FREE BY CATCHING RATHER THAN PRE-CHECKING. An `exists?` read followed by a create leaves a
    # window in which another caller creates the namespace, and on THIS door the loser of that race
    # would go on to seed ownership over somebody else's object. The backend's own refusal is the only
    # answer that cannot be stale.
    if create_mode is CreateMode.EXIST_OK:
        response, kept_existing = await create_or_keep_namespace(ns, segments, req)
        if kept_existing and declared is not None and (kept := (response.properties or {}).get(tier_policy.TIER_PROPERTY)) != declared:
            # ExistOk keeps the namespace as it is, so a tier it does not already have would not land. A tier
            # is declared once (ADR 0160): saying so beats a 200 that quietly ignored the declaration.
            raise NamespaceAlreadyExistsError(f"namespace {id!r} already exists with {tier_policy.TIER_PROPERTY}={kept!r}; a tier is declared once, at create")
    elif create_mode is CreateMode.OVERWRITE:
        response = await _create_or_replace_empty_namespace(ns, settings, token, client, segments, req)
        kept_existing = False
    else:
        response = await run_in_threadpool(native.call, ns, "create_namespace", req)
        kept_existing = False

    async def _undo_create() -> None:
        await run_in_threadpool(native.call, ns, "drop_namespace", DropNamespaceRequest(id=segments))

    # Owner + parent edge (parent namespace if nested, else the catalog root) so the
    # concentric cascade reaches the namespace and its tables — stops a nested-namespace
    # lockout and lets a layer-level grant (medallion bronze/silver/gold) reach children.
    #
    # Compensated (diff2 F3): the namespace is EMPTY at this instant — it was created three lines up
    # and nothing can have been put in it yet — so the undo cannot destroy anyone's data. A failed
    # seed used to leave a namespace its creator could neither see nor drop, while native
    # `NamespaceAlreadyExists` refused every retry, permanently reserving the name.
    # NOT ON THE `ExistOk` KEEP PATH — for the seed and for the ANNOUNCEMENT alike. The namespace
    # already exists and already has an owner, so seeding would hand somebody else's namespace to any
    # caller who may create one (the same reason `table_create.py` carries `existok_kept_existing`),
    # and `namespace_created` would announce a creation that did not happen to every subscriber of the
    # control stream. Nothing changed, so the honest control event is none. A namespace this call
    # really created still gets both, which is why the condition is the flag rather than the mode.
    if not kept_existing:
        await fga_deps.seed_ownership_or_compensate(
            client,
            settings,
            token,
            resolver,
            resource="namespace",
            segments=segments,
            uuid=minted,
            undo=_undo_create,
            # A bound top-level namespace hangs off its warehouse, as the warehouse door seeds it.
            parent_object=f"warehouse:{binding['warehouse_id']}" if binding is not None else None,
        )
        await emit_control(
            control,
            action="namespace_created",
            object_type="namespace",
            object_id=fga.namespace_object(minted),
            object_name=fga.canonical_object_id(segments),
            actor=f"user:{token.subject}" if token else None,
            extra={"mode": req.mode, "properties": req.properties},
        )
    return response


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
    caller can create the namespace in, and for the create door the loser of that race would then seed
    ownership over somebody else's object. The backend's own refusal is the only answer that cannot be
    stale.

    The kept namespace is DESCRIBED rather than echoed, so a caller receives what the namespace
    actually is instead of a reflection of its own request.
    """
    try:
        return await run_in_threadpool(native.call, ns, "create_namespace", req), False
    except NamespaceAlreadyExistsError:
        described = await run_in_threadpool(native.call, ns, "describe_namespace", DescribeNamespaceRequest(id=segments))
        return CreateNamespaceResponse(properties=getattr(described, "properties", None)), True


async def _forget_namespace(
    settings: Settings, client: OpenFgaClient | None, token: Caller | None, segments: list[str], *, namespace_uuid: str | None, unbind: bool
) -> None:
    """Remove what a dropped namespace leaves behind: the warehouse binding (``unbind``, top-level only), the records
    kept under its uuid when the caller read one before the drop, and its FGA tuples, with those of every child an
    earlier attempt removed, which its `child` edges still name (``fga_deps.end_removed_subtree``).

    Each step is idempotent — an absent record or tuple is already the post-condition — so a retry over a
    half-finished drop converges. A grant or record left behind governs nothing, since no other namespace holds the
    uuid it is keyed by ([[LH-524]]).
    """
    if unbind and len(segments) == 1:
        await run_in_threadpool(warehouses.unbind_namespace, settings.registry_root, settings.storage_options(), segments[0])
    async for _ in fga_deps.end_removed_subtree(client, settings, root_uuid=namespace_uuid, removed=[], token=token):
        pass


async def _judge_tier_declaration(
    ns: LanceNamespace,
    settings: Settings,
    token: Caller | None,
    client: OpenFgaClient | None,
    segments: list[str],
    *,
    declared: str | None,
    overwrite: bool,
) -> None:
    """Refuse a create that declares a tier, or an Overwrite that would replace one, unless an admin asks (ADR 0160).

    A tier is declared once: no Lance operation alters a namespace's properties, so the catalog's own
    Overwrite is the only way to change one, and it needs the same admin as the declaration. A deeper
    namespace may repeat its ancestors' tier and nothing else, which is what lets the judge stop at the
    shallowest declaration. The ancestors are read only after the admin check, so a refused caller learns
    nothing about them. An Overwrite reads the namespace's own declaration first, to know whether it
    replaces one; a caller who may create beside it already reads it as a reader of the parent.

    Raises:
        PermissionDeniedError: The caller is not the namespace's project admin (or the estate admin).
        InvalidInputError: ``declared`` differs from the tier an ancestor declares.
    """
    replaced = await tier_policy.declared_on(ns, segments) if overwrite else None
    if declared is None and replaced is None:
        return
    project = (
        await run_in_threadpool(warehouses.project_for_namespace, settings.registry_root, settings.storage_options(), segments[0])
        if settings.warehouses_enabled
        else None
    )
    await fga_deps.require_tier_authority(client, settings, token, project=project)
    inherited = await tier_policy.tier_of(ns, segments[:-1])
    if declared is not None and inherited is not None and inherited != declared:
        raise InvalidInputError(f"namespace {segments!r} lies under a {inherited} namespace, so it cannot declare {tier_policy.TIER_PROPERTY}={declared!r}")


async def _create_or_replace_empty_namespace(
    ns: LanceNamespace,
    settings: Settings,
    token: Caller | None,
    client: OpenFgaClient | None,
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
    protection and policy go with it, so the creator is seeded on a clean id; its binding stays, because
    the new namespace lives where the old one did.

    Catching rather than pre-checking, as in :func:`create_or_keep_namespace`: a create that wins needs no
    drop, and a concurrent create between the drop and the re-create answers the ordinary 409.
    """
    try:
        return await run_in_threadpool(native.call, ns, "create_namespace", req)
    except NamespaceAlreadyExistsError:
        pass
    canonical = fga.canonical_object_id(segments)
    replaced = await fga_deps.identify(ns, settings, "namespace", segments)
    await fga_deps.require_relation(client, settings, token, relation="can_delete", obj=replaced.fga_object(), label=f"namespace:{canonical}")
    await fga_deps.require_namespace_unprotected(settings, replaced, force=False)
    _, tables = await _drain_tables(ns, segments)
    _, children = await _drain_namespaces(ns, segments)
    if tables or children:
        raise NamespaceNotEmptyError(
            f"cannot overwrite namespace '{canonical}': Overwrite drops only an empty namespace, and it holds "
            f"tables {tables} and namespaces {children}. Drop or move them first, or drop the namespace with behavior=Cascade."
        )
    # A table landing between the listing and this drop is refused by the backend's own Restrict check.
    await run_in_threadpool(native.call, ns, "drop_namespace", DropNamespaceRequest(id=segments))
    await _forget_namespace(settings, client, token, segments, namespace_uuid=replaced.uuid, unbind=False)
    return await run_in_threadpool(native.call, ns, "create_namespace", req)


def _merge_bound_top_namespaces(names: list[str], bound: Iterable[Mapping[str, str]]) -> list[str]:
    """A ROOT listing's names plus the warehouse-bound top-level namespaces the default backend cannot see.

    SORTED AND DEDUPED because the caller's cursor is keyset over this list: an unsorted merge would make
    ``page_token`` skip or repeat rows, and a namespace reachable both ways — bound AND present in the
    default root — must be one row, not two.

    A record with no ``top_ns`` is skipped rather than trusted: the registry is object-store JSON written
    by another service, and an empty name in a listing is one a client will try to resolve.
    """
    return sorted({*names, *(top for record in bound if (top := record.get("top_ns")))})


async def _bound_top_namespaces(settings: Settings) -> list[Mapping[str, str]]:
    """The bindings registry, or NOTHING when it cannot be read.

    Tolerant for the reason ``GET /v1/table`` is tolerant per seed: the registry lives in object storage
    and an unreachable bucket must narrow this listing to the default root's answer, never turn a
    discovery call into a 500. The narrowing is the same one every caller already gets today.
    """
    try:
        return list(await run_in_threadpool(warehouses.list_bindings, settings.registry_root, settings.storage_options()))
    except Exception:
        log.warning("namespace_root_bindings_unreadable", extra={"registry_root": settings.registry_root}, exc_info=True)
        return []


@router.get("/{id}/list", response_model_exclude_none=True)
async def list_namespaces(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    resolver: ResolverDep,
    page_token: str | None = None,
    limit: ListLimit = None,
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

    A namespace name is not a harmless header. ``viewer.api.security`` states the estate's position —
    "A corpus LIST is itself sensitive: it names data someone may not know exists" — and a namespace
    name additionally leaks how a tenant organises data.

    So: the ROUTE still opens (narrowing it to 403 would restore the broken breadcrumb C1 fixed) and
    each ITEM is checked on the object itself, on ``can_get_metadata`` — the relation that governs
    seeing a namespace, where the table listing filters on ``can_read_data`` because reading a table's
    NAME and reading its ROWS are the same disclosure there.

    Pagination is applied AFTER the filter for the reason ``list_tables`` records: a backend ``limit``
    truncates first, so a page could answer short and hand out a cursor that skips everything the
    filter removed.
    """
    segments = parse_identifier(id)
    response, names = await _drain_namespaces(ns, segments)
    # THE ROOT ONLY, and the condition is the whole point. The root id has no top segment to route by,
    # so `get_namespace` hands this route the DEFAULT namespace and the drain above asks that one
    # backend — while a warehouse-enabled estate keeps each tenant's namespaces in that tenant's own
    # bucket. The bindings registry already names exactly the missing children, because the root's
    # children ARE top-level namespaces and a binding names one.
    #
    # It is also the source that keeps this answer in step with the routes that serve it: `get_namespace`
    # routes an id by its binding, so a namespace sitting in a warehouse bucket with no binding is
    # reachable by no id route, and naming it here would advertise a listing the estate cannot serve.
    #
    # A binding merged below is NOT exempt from the per-item filter — it goes into `names` before it,
    # because a bound namespace's name is as disclosive as a sibling's.
    if not segments and settings.warehouses_enabled:
        names = _merge_bound_top_namespaces(names, await _bound_top_namespaces(settings))

    if (authz := fga_deps.authorization(settings, client, token)) is not None:
        response.namespaces, response.page_token = await fga_deps.authorized_page(
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


@router.post("/{id}/describe", response_model_exclude_none=True)
def describe_namespace(id: str, ns: NamespaceDep, body: DescribeNamespaceRequest | None = None) -> DescribeNamespaceResponse:
    """Return the metadata/properties of namespace ``id`` via ``describe_namespace``."""
    segments = reconcile_body_id(parse_identifier(id), body.id if body else None)
    req = DescribeNamespaceRequest(id=segments)
    return native.call(ns, "describe_namespace", req)


async def _trash_subtree(
    ns: LanceNamespace,
    settings: Settings,
    token: Caller | None,
    segments: list[str],
    descendants: list[fga_deps.GovernedObject],
    *,
    own: ControlUuids,
    grace_days: float,
) -> None:
    """DETACH the subtree instead of destroying it — the recoverable half of a cascade (#96).

    A trash record pointing at bytes the native cascade already deleted would be a LIE: undrop
    re-registers still-present bytes, and `drop_namespace(cascade)` destroys its children inside one
    native call. So with a grace period the cascade never reaches that call. Instead, exactly what
    the single-table drop door does, per child: every descendant table is DEREGISTERED (pointer
    detached, bytes untouched) and filed in the trash; every namespace — children deepest-first,
    then the cascaded root — is then empty and dropped plainly, filed as a ``kind="namespace"``
    record so undrop knows which manifest rows to rebuild. All records share one drop-time stamp
    rule: ``expires_at`` is data on the record, never policy read at expiry.

    Not atomic, deliberately: a mid-loop failure leaves every already-detached child RECOVERABLE
    (its record is filed and its parent namespace still exists), which is strictly safer than the
    all-or-nothing native destroy it replaces.

    Each record is filed under its object's uuid, names its parent namespace's uuid, and carries the uuid of the
    namespace the cascade dropped (``cascade``), so the namespace undrop restores exactly this cascade's records, never
    another object dropped under one of their names ([[LH-525]]). ``descendants`` and ``own`` are what the door read
    while the subtree stood, and ``grace_days`` the window its warehouse's delete profile gives (ADR 0146).

    Raises:
        InternalError: The dropped namespace keeps no uuid, which no door leaves.
    """
    so = settings.storage_options()
    dropped_by = f"user:{token.subject}" if token is not None else None
    if own.object_uuid is None:
        raise InternalError(f"namespace {fga.canonical_object_id(segments)!r} keeps no uuid, so its trash cannot be filed")
    cascade = own.object_uuid
    uuid_at = {tuple(item.segments): item.uuid for item in descendants} | {tuple(segments): cascade}

    def parent_of(child: list[str]) -> str | None:
        return uuid_at.get(tuple(child[:-1]), own.namespace_uuid)

    tables = [item.segments for item in descendants if item.kind == "table"]
    # Deepest-first, so every namespace is empty by the time its own drop runs.
    child_namespaces = sorted((item.segments for item in descendants if item.kind == "namespace"), key=len, reverse=True)
    # THE DESCRIBES FIRST, CONCURRENTLY (catalog-api-11): they are independent reads whose results are
    # only collected, and a subtree of N tables paid N sequential round trips before a single record
    # was filed. The WRITE pair below stays sequential and in order per child — see the ordering note.
    described_all: list[DescribeTableResponse] = list(
        await asyncio.gather(*(run_in_threadpool(native.call, ns, "describe_table", DescribeTableRequest(id=child)) for child in tables))
    )
    for child, described in zip(tables, described_all, strict=True):
        # A declared-only table has no location; its record carries "" and undrop skips it with a
        # warning — there were no bytes to lose, only a declaration the caller can redo.
        record = trash.make_record(
            fga.canonical_object_id(child),
            uuid=uuid_at[tuple(child)],
            location=described.location or "",
            dropped_by=dropped_by,
            grace_days=grace_days,
            namespace_uuid=parent_of(child),
            cascade=cascade,
        )
        # ORDER SPLIT ON WHETHER THE RECORD OWNS BYTES (diff2 F6 leg a), matching the single-table
        # door. A byte-owning record goes FIRST: a crash between the deregister and the write used to
        # leave the bytes unreachable by undrop, by the purge and by any retry (`describe_table` 404s
        # once detached), and filing first turns that into a record on a live table, which the purge
        # refuses `STILL_REGISTERED` and the retry overwrites.
        #
        # A byte-LESS record keeps the old order, and that is not laziness. The purge's estate test is
        # skipped for records with no location, so a record filed on a still-LIVE object inside a
        # deactivated warehouse would be revoked and cleared — trading a recoverable loss for an
        # unrecoverable one. Here the window costs only a declaration the caller can redo.
        if described.location:
            await run_in_threadpool(trash.put, settings.registry_root, so, record)
            # The trashed id keeps its location until undrop or purge, as at the table door ([[LH-204]]).
            await run_in_threadpool(table_claims.keep_for_trash, ns, table_claims.claim_store(settings), described.location, str(record["id"]), child)
            await run_in_threadpool(native.call, ns, "deregister_table", DeregisterTableRequest(id=child))
        else:
            await run_in_threadpool(native.call, ns, "deregister_table", DeregisterTableRequest(id=child))
            await run_in_threadpool(trash.put, settings.registry_root, so, record)
    # The ROOT's binding, read BEFORE anything is dropped — after the unbind below it is unreadable,
    # and after the purge it is gone for good. `None` for a nested namespace (bindings are keyed by
    # top-level segment) and for an unbound one (single-bucket estates have no binding at all).
    binding: dict[str, str] | None = None
    if len(segments) == 1:
        live = await run_in_threadpool(warehouses.binding_for_namespace, settings.registry_root, so, segments[0])
        if live:
            binding = {"warehouse_id": str(live["warehouse_id"]), "root_uri": str(live["root_uri"])}
    for child in [*child_namespaces, segments]:
        # Read before the drop removes the `__manifest` row that is their only copy.
        described = await run_in_threadpool(native.call, ns, "describe_namespace", DescribeNamespaceRequest(id=child))
        await run_in_threadpool(native.call, ns, "drop_namespace", DropNamespaceRequest(id=child))
        record = trash.make_record(
            fga.canonical_object_id(child),
            uuid=uuid_at[tuple(child)],
            location="",
            dropped_by=dropped_by,
            grace_days=grace_days,
            kind="namespace",
            namespace_uuid=parent_of(child),
            cascade=cascade,
            # Only the ROOT carries it — `child is segments` is identity on the same list object, and
            # the root is the last iteration by construction.
            binding=binding if child is segments else None,
            properties=described.properties,
        )
        await run_in_threadpool(trash.put, settings.registry_root, so, record)
    # UNBIND LAST, and only once the root's record — carrying the binding — is durably written
    # (diff2 F6 leg c). Order is the whole safety argument:
    #
    #   * before the loop, a mid-loop crash would leave the surviving children unroutable, because
    #     `NamespaceDep` resolves through this very binding;
    #   * after the record, a crash between the two leaves a binding whose namespace is gone — which
    #     is exactly the old leak, but now bounded by one retry instead of forever, and `undrop`
    #     re-binds to the same values so it converges either way.
    #
    # The alternative shape — keep the binding and have the PURGE remove it — was rejected: it puts a
    # registry write in the reclaimer (which owns no write path to it and may not import the
    # catalog's), and it needs a reconciler category that would fire on every legitimately-trashed
    # subtree for its whole grace window, blocking `report_is_clean` and with it the purge itself.
    if binding is not None:
        await run_in_threadpool(warehouses.unbind_namespace, settings.registry_root, so, segments[0])


async def _destroy_subtree(
    ns: LanceNamespace,
    segments: list[str],
    descendants: list[fga_deps.GovernedObject],
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
    deleted at the location it is described at, by `dataplane.destroy_table`, [[LH-532]] — this is the destructive
    path by definition), then namespaces
    deepest-first so each is empty when its own drop runs, then the cascaded root. Not atomic, and
    deliberately so: a mid-loop failure leaves a SMALLER subtree that the same call can finish on a
    retry, which is strictly better than the all-or-nothing native call it replaces (that one simply
    refused). An already-absent child is tolerated — drift, not an error, exactly as the warehouse
    cascade treats a binding that outlived its namespace.

    Each destroyed table's base record goes with its bytes ([[LH-279]]), off the location its drop answers:
    left behind, it would sanction its bases for the next table registered there. Its location claim goes
    too ([[LH-204]]), or the location stays held for the claim's lease.

    Each table is yielded once it is gone, or found gone already, so the caller announces it before the next one
    goes ([[LH-495]]): the retry lists only what still stands, so a table the first attempt destroyed is announced
    then or not at all.

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
    # location another live table holds refuses the whole cascade, as a protected descendant does.
    for item in tables:
        with suppress(TableNotFoundError):
            doomed: DescribeTableResponse = await run_in_threadpool(native.call, ns, "describe_table", DescribeTableRequest(id=item.segments))
            if doomed.location:
                await run_in_threadpool(table_claims.require_no_other_holder, ns, claims, doomed.location, fga.canonical_object_id(item.segments))
    for item in tables:
        child = item.segments
        with suppress(TableNotFoundError):
            # [[LH-532]] Described again at the moment it goes, so the bytes deleted are those of the row removed.
            doomed = await run_in_threadpool(native.call, ns, "describe_table", DescribeTableRequest(id=child))
            dropped: DropTableResponse = await run_in_threadpool(
                partial(
                    dataplane.destroy_table,
                    ns,
                    storage_options,
                    child,
                    str(doomed.location or ""),
                    estate_roots=estate_roots,
                    table_uuid=item.uuid,
                    namespace_uuid=uuid_at.get(tuple(child[:-1])),
                    control=claims,
                    dropped_by=dropped_by,
                )
            )
            if dropped.location:
                await run_in_threadpool(base_registry.forget_base_record, registry, dropped.location, table=fga.canonical_object_id(child))
                await run_in_threadpool(location_claims.release, claims, dropped.location, fga.canonical_object_id(child))
        yield child
    for child in [*child_namespaces, segments]:
        with suppress(NamespaceNotFoundError):
            await run_in_threadpool(native.call, ns, "drop_namespace", DropNamespaceRequest(id=child))


async def _announce_removed(
    emitter: LineageEmitter,
    segments: list[str],
    *,
    operation: str,
    token: Caller | None,
    authorization: str | None,
    uuids: UuidReader,
    known: DatasetUuids,
) -> None:
    """Tell lineage a table stopped existing, so its reconcile does not read it as live forever ([[LH-144]]).

    Before its grants are revoked: on the http transport the caller's bearer authorizes the ingest against grants
    still standing. Best-effort and logged: a missing lineage row must not stop the delete before its revoke, which
    would trade it for grants on nothing.
    """
    try:
        await emit_write_event(
            emitter,
            segments,
            author=token.subject if token is not None else None,
            version=None,
            operation=operation,
            authorization=authorization,
            uuids=uuids,
            known=known,
        )
    except Exception as exc:  # noqa: BLE001 — provenance must never fail the delete it records
        log.warning("cascade_drop_lineage_emit_failed", extra={"table": segments, "error": str(exc)})


async def _identify(ns: LanceNamespace, settings: Settings, descendants: list[tuple[str, list[str]]]) -> list[fga_deps.GovernedObject]:
    """Every descendant a cascade is about to remove, with its uuid, read while it still stands.

    CONCURRENT (catalog-api-11): one independent read per descendant on the pre-flight of a cascade, so its latency
    does not grow with the subtree. Enumeration order is kept, so a refusal names the FIRST protected descendant.
    """
    return list(
        await asyncio.gather(*(fga_deps.identify(ns, settings, "table" if resource == "table" else "namespace", child) for resource, child in descendants))
    )


async def _require_descendants_unprotected(settings: Settings, descendants: list[fga_deps.GovernedObject], *, force: bool) -> None:
    """Refuse a cascade that would destroy a PROTECTED descendant, naming the first one found.

    The guard at the named rung only sees the id in the URL; a cascade's unit of destruction is the
    whole subtree, and those children are destroyed inside one native call that never reaches a door.
    So the check belongs here, where the destroyed set is actually enumerated.
    """
    if force or not descendants:
        return
    so = settings.storage_options()
    records = await asyncio.gather(*(run_in_threadpool(protection.get_protection, settings.registry_root, so, item.kind, item.uuid) for item in descendants))
    for item, record in zip(descendants, records, strict=True):
        fga_deps.require_not_protected(record or {}, kind=item.kind, obj_id=fga.canonical_object_id(item.segments), force=False)


async def _skip_absent_namespace(settings: Settings, client: OpenFgaClient | None, token: Caller | None, segments: list[str]) -> DropNamespaceResponse:
    """``mode=Skip`` on a namespace that is not there: succeed, after finishing the trailer a previous drop left.

    Skip is the retry lever, and the retry it exists for is a drop that removed the namespace and failed
    before its trailer ran. Answering success without the trailer would leave that drop's binding on the id
    for the next namespace created there to route through, so the trailer runs here: every step is
    idempotent, and on an id that never existed each one finds nothing. The namespace's records and grants are
    keyed by a uuid nothing here can read any more, and they govern no other namespace.

    Not while the trash holds a namespace dropped under the name: its binding moved onto its record for the undrop (#96),
    and its grants are held by its uuid, so a namespace created at the name meanwhile inherits nothing ([[LH-525]]).

    The answer is 200 with a DropNamespaceResponse body, not the 204 the mode's prose names: spec.yaml's
    DropNamespace operation declares only 200, and pylance 12.0.0's RestNamespace refuses a 204 ("Failed
    to parse response: EOF while parsing a value", measured 2026-10-05). Nothing was dropped by this call,
    so nothing is announced on the control stream.
    """
    canonical = fga.canonical_object_id(segments)
    if not await run_in_threadpool(trash.named, settings.registry_root, settings.storage_options(), canonical, kind="namespace"):
        await _forget_namespace(settings, client, token, segments, namespace_uuid=None, unbind=True)
    return DropNamespaceResponse()


@router.post("/{id}/drop", response_model_exclude_none=True)
async def drop_namespace(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
    emitter: LineageEmitterDep,
    uuids: ObjectUuidsDep,
    body: DropNamespaceRequest | None = None,
    force: RaskFlag = False,
    purge: RaskFlag = False,
    authorization: Annotated[str | None, Header()] = None,
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
    # The namespace the name resolves to now, with the uuid its records are kept under (ADR 0173 §8). A `Skip` of a
    # namespace that is not there answers here, before anything is enumerated.
    try:
        dropped = await fga_deps.identify(ns, settings, "namespace", segments)
    except NamespaceNotFoundError:
        if drop_mode is not DropMode.SKIP:
            raise
        return await _skip_absent_namespace(settings, client, token, segments)
    await fga_deps.require_namespace_unprotected(settings, dropped, force=force)
    descendants: list[fga_deps.GovernedObject] = []
    # Enumerated whenever a CASCADE is asked for — not only when FGA is on, as it used to be. A cascade
    # destroys children INSIDE the single native call, so they never re-enter this door: without this
    # list, a protected table under a cascade-dropped namespace died silently while the docstring
    # claimed protection covered it. The tuple-revoke below consumes the same list when FGA is on.
    if cascade:
        # A CASCADE TOUCHES EXISTENCE EARLIER than the drop itself — it enumerates the subtree first —
        # so a `Skip` guarding only the drop below would still raise for the caller most likely to be
        # retrying. Narrow on purpose: this covers the NAMED namespace being absent, never a descendant
        # vanishing mid-destroy, which is a real error and must not read as a skip.
        try:
            descendants = await _identify(ns, settings, await run_in_threadpool(_collect_descendants, ns, segments))
        except NamespaceNotFoundError:
            if drop_mode is not DropMode.SKIP:
                raise
            return await _skip_absent_namespace(settings, client, token, segments)
        # Protection is a property of the SUBTREE, so it is checked against every id about to die, and
        # the refusal NAMES the protected descendant — "something in here is protected" is not an
        # answer anyone can act on. `force` turns this lock exactly as it does at the named rung.
        await _require_descendants_unprotected(settings, descendants, force=force)
    # What every announcement below names the objects by, read while they still stand (ADR 0173 §10).
    own = await uuids.namespace(segments)
    known = {tuple(item.segments): await uuids.table(item.segments, uuid=item.uuid) for item in descendants if item.kind == "table"}
    # #96: with a grace period, a cascade DETACHES the subtree (trash record per child) instead of
    # destroying it inside the one native call — see _trash_subtree for why the native cascade and a
    # trash record cannot coexist. `purge=true` is the explicit destroy-now, as at the table door.
    grace = 0.0 if purge or not cascade else await trash_doors.drop_grace_days(settings, segments)
    recoverable = grace > 0
    if recoverable:
        # The cascade's records are keyed by the uuid the door identified the namespace by, not the best-effort read its
        # announcements name it by, which answers ``None`` on a failed describe.
        await _trash_subtree(
            ns, settings, token, segments, descendants, own=ControlUuids(object_uuid=dropped.uuid, namespace_uuid=own.namespace_uuid), grace_days=grace
        )
        # Long running, as the spec reads a `transaction_id`: the subtree's bytes are deleted later, by the purge, and
        # each table's deletion is tracked through DescribeTransaction as `<table id>$<its uuid>`, as at the table door.
        detached = [fga.canonical_object_id([*item.segments, item.uuid]) for item in descendants if item.kind == "table"]
        response = DropNamespaceResponse(transaction_id=detached or None)
    elif cascade:
        # The dir backend cannot cascade (#117) — we enumerate and destroy bottom-up ourselves, announcing each table as
        # it goes ([[LH-495]]): the retry after a failure part-way lists only what still stands.
        registry = base_registry.BaseRegistry(control_root=settings.registry_root, storage_options=settings.storage_options())
        async for table in _destroy_subtree(
            ns,
            segments,
            descendants,
            root_uuid=dropped.uuid,
            registry=registry,
            claims=table_claims.claim_store(settings),
            storage_options=settings.storage_options(),
            estate_roots=(settings.registry_root, settings.root),
            dropped_by=f"user:{token.subject}" if token is not None else None,
        ):
            await _announce_removed(
                emitter,
                table,
                operation=DROP_TABLE,
                token=token,
                authorization=authorization,
                uuids=uuids,
                known=known.get(tuple(table), DatasetUuids()),
            )
        response = DropNamespaceResponse()
    else:
        try:
            response = await run_in_threadpool(native.call, ns, "drop_namespace", req)
        except NamespaceNotFoundError:
            if drop_mode is not DropMode.SKIP:
                raise
            return await _skip_absent_namespace(settings, client, token, segments)
    # The record dies with the object — a reused id must not inherit protection nobody set on it. A recoverable cascade
    # clears its namespace's here, and its DESCENDANTS' only under force=true (an unforced cascade already proved none
    # exist); every other drop ends them below, with the rest of what each removed object holds.
    if recoverable:
        await run_in_threadpool(protection.clear_protection, settings.registry_root, settings.storage_options(), "namespace", dropped.uuid)
    if recoverable and force:
        for item in descendants:
            await run_in_threadpool(protection.clear_protection, settings.registry_root, settings.storage_options(), item.kind, item.uuid)
    # The warehouse BINDING dies with a destroyed top-level namespace. `unbind_namespace` existed and
    # only the warehouse-delete door called it, so every direct drop of a top-level namespace leaked
    # its binding record — and the UI derives its namespace list from bindings, so the dropped
    # namespace kept LISTING forever, indistinguishable from a live empty one (seen on the deployed
    # estate: `casc9` survived its own successful cascade). Kept on a RECOVERABLE drop, exactly as the
    # grants are: undrop rebuilds the subtree and the binding must still be there to route it.
    if not recoverable and len(segments) == 1:
        await run_in_threadpool(warehouses.unbind_namespace, settings.registry_root, settings.storage_options(), segments[0])
    # RECORD THE DELETION OF EVERY TABLE THIS CASCADE DETACHED, exactly as the single-table door does (a `drop_table`
    # event per table); a destroyed subtree announced each table as it went. A follower of the feed learns a table
    # is gone only from these events ([[LH-144]]), so a table nothing records stays indistinguishable from a live
    # one. Emitted for a recoverable drop too, matching that door — the undrop emits a `register_table` marker per
    # table, which a follower reads as the table existing again.
    if recoverable:
        for item in descendants:
            if item.kind == "table":
                await _announce_removed(
                    emitter,
                    item.segments,
                    operation=DROP_TABLE,
                    token=token,
                    authorization=authorization,
                    uuids=uuids,
                    known=known.get(tuple(item.segments), DatasetUuids()),
                )
    # End what each removed object holds AFTER the drop commits, so a failed or Restrict-refused drop leaves all of it in
    # place: the descendants first, found by the catalog's listing and by the `child` edges a failed earlier attempt
    # left, each one's protection and maintenance policy before its grants, then the namespace's own. NOT on a
    # recoverable cascade (#96, the #75 rule): undrop rebuilds the subtree, and each object must come back configured
    # as it was and reachable by the owner, the one person who needs to undrop it. Those die with the bytes — at
    # purge, or when the sweep's expiry reclaims the trash.
    if not recoverable:
        async for _ in fga_deps.end_removed_subtree(client, settings, root_uuid=dropped.uuid, removed=descendants, token=token):
            pass
    await emit_control(
        control,
        action="namespace_dropped",
        object_type="namespace",
        object_id=dropped.fga_object(),
        object_name=fga.canonical_object_id(segments),
        object_uuid=own.object_uuid,
        namespace_uuid=own.namespace_uuid,
        actor=f"user:{token.subject}" if token else None,
        extra={"cascade": cascade, "recoverable": recoverable, "descendants_revoked": 0 if recoverable else len(descendants)},
    )
    return response


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
    records = await resolver.admitted_trash("namespace", parse_identifier(id))
    return [TrashEntry(**{k: str(record.get(k, "")) for k in ("id", "uuid", "location", "dropped_by", "dropped_at", "expires_at")}) for record in records]


@management_router.post("/{id}/undrop", response_model_exclude_none=True)
async def undrop_namespace(
    id: str,
    request: Request,
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
    canonical = fga.canonical_object_id(segments)
    so = settings.storage_options()
    trashed = await resolver.admitted_trash("namespace", segments)
    if not trashed:
        raise NamespaceNotFoundError(f"no recoverable drop for namespace: {canonical}. The grace period may have expired, or the drop purged its subtree.")
    record = trash_doors.the_one_to_restore(trashed, kind="namespace", name=canonical)
    trash_doors.require_restorable(record, name=canonical)
    root_uuid = trash.object_uuid(record)
    if root_uuid is None:
        raise InternalError(f"the trash record of namespace {canonical!r} keeps no uuid, so its grants cannot be found")
    standing = await resolver.namespace_uuid(segments)
    if standing is not None and standing != root_uuid:
        raise NamespaceAlreadyExistsError(f"cannot undrop namespace {canonical!r}: another namespace holds the name now; drop it first")
    if len(segments) > 1:
        trash_doors.require_its_own_parent(record, standing=await resolver.namespace_uuid(segments[:-1]), name=canonical)
    everything = await run_in_threadpool(trash.list_all, settings.registry_root, so)
    # The records the cascade that dropped this namespace filed, at its name and under it: undropping a namespace that
    # cascade dropped below its own root restores that branch of it, once its parent stands again.
    cascade, prefix = record.get("cascade"), canonical + CATALOG_DELIMITER
    subtree = [r for r in everything if r.get("cascade") == cascade and (r.get("id") == canonical or str(r.get("id") or "").startswith(prefix))]
    # Shallowest first — a parent manifest row must exist before its children's create/register.
    namespace_records = sorted((r for r in subtree if r.get("kind") == "namespace"), key=lambda r: str(r["id"]).count(CATALOG_DELIMITER))
    table_records = [r for r in subtree if r.get("kind") == "table"]
    # Before anything is rebuilt: a residue record's table was destroyed, not trashed ([[LH-532]]).
    for t_rec in table_records:
        trash_doors.require_restorable(t_rec, name=str(t_rec.get("id") or ""))
    # RE-BIND FIRST, and re-resolve the connection from the restored root (diff2 F6 leg c).
    #
    # The drop unbound this top-level namespace, so `NamespaceDep` — which resolves BEFORE this
    # handler body runs — has already fallen back to the estate's default root. Rebuilding through it
    # would re-create the whole subtree in the WRONG BUCKET: a silent tenant-isolation break, not an
    # error. So the binding is restored from the record and the connection re-derived from it.
    #
    # `bind_namespace` is write-once at the store: identical `{warehouse_id, root_uri}` is idempotent
    # (the retry path), a DIFFERENT binding raises `NamespaceAlreadyExistsError` (409). That refusal
    # is correct and deliberate — if the id was re-bound to another warehouse during the grace window,
    # routing this subtree there would hand one tenant another's bucket, and failing loudly is the
    # only safe answer. A record with no `binding` (a nested undrop, an unbound estate, or a record
    # written before this landed) keeps the dependency's connection untouched.
    bound = record.get("binding") or None
    if bound:
        warehouse_id = str(bound["warehouse_id"])
        # THE DEACTIVATION GATE, and it is not optional: every caller of `namespace_for_root` pairs it
        # with one, or a quarantined warehouse's bucket stays reachable through that caller.
        # `get_namespace` gates every ordinary request this way; re-deriving the connection here steps
        # around that dependency, so the check has to come with it.
        #
        # Deactivation is offboarding step one, so recovering a subtree INTO a quarantined warehouse is
        # precisely what it exists to stop. Fail-closed on both failure kinds, symmetric with
        # `dependencies.py`: an unreadable registry is 503, and a MISSING warehouse record (clean
        # `None`) is not-active → 403, never silently allowed.
        # THE WHOLE RECORD, for the reason `dependencies._resolve_warehouse_root` reads it: the undrop
        # also needs the warehouse's own `endpoint` ([[LH-067]]), and it is the same single GET. Opening
        # the estate's store instead would recover the subtree into a different object store from the
        # one its tables live in.
        try:
            warehouse = await run_in_threadpool(warehouses.get_warehouse, settings.registry_root, so, warehouse_id)
        except Exception as exc:
            log.warning("warehouse_status_lookup_failed", extra={"top_ns": segments[0], "warehouse_id": warehouse_id, "error": str(exc)})
            raise ServiceUnavailableError(f"warehouse status lookup failed for {warehouse_id!r}") from exc
        if warehouse is None or (warehouse.get("status") or "active") != "active":
            raise PermissionDeniedError(
                f"warehouse {warehouse_id!r} is deactivated (quarantined); namespace {segments[0]!r} cannot be recovered into it until it is reactivated"
            )
        # [[LH-205]] Before the binding is written, so a record naming another store refuses the undrop
        # whole instead of leaving a binding behind for the connection builder to refuse.
        require_estate_store(warehouse.get("endpoint"), estate=settings.s3_endpoint, subject=f"warehouse {warehouse_id!r}")
        await run_in_threadpool(
            warehouses.bind_namespace,
            settings.registry_root,
            so,
            segments[0],
            warehouse_id,
            str(bound["root_uri"]),
        )
        ns = await namespace_for_root(request, settings, str(bound["root_uri"]), endpoint=warehouse.get("endpoint") or None)
        root = str(bound["root_uri"])
    response = CreateNamespaceResponse()
    for n_rec in namespace_records:
        n_id = str(n_rec["id"])
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
        rebuilt = CreateNamespaceRequest(id=n_segments, mode="exist_ok", properties=n_rec.get("properties"))
        created, _kept = await create_or_keep_namespace(ns, n_segments, rebuilt)
        if n_id == canonical:
            response = created
        # Clear only AFTER the create commits — a failed rebuild must leave the subtree recoverable.
        await run_in_threadpool(trash.clear, settings.registry_root, so, str(n_rec["uuid"]), kind="namespace")
    skipped = 0
    for t_rec in table_records:
        t_id = str(t_rec["id"])
        location = str(t_rec.get("location") or "")
        if location:
            # The RELATIVE key register_table accepts, the one the record's URL decodes to under the root, as at the
            # table undrop ([[LH-517]]).
            t_segments = parse_identifier(t_id)
            body = RegisterTableRequest(id=t_segments, location=dataplane.relative_location(location, root=root))
            # The cascade kept each location's claim for its trashed id; another holder means another
            # table now resolves to these bytes, as at the table door ([[LH-204]]).
            try:
                kept = await run_in_threadpool(table_claims.take, ns, table_claims.claim_store(settings), location, t_id, t_segments)
            except location_claims.LocationHeldError as held:
                log.warning("undrop_refused_held_location", extra={"table": t_id, "location": held.claim.location, "holder": held.claim.table})
                raise InvalidTableStateError(f"cannot undrop table {t_id}: its location is now held by another table") from None
            if kept.uuid != t_rec.get("uuid"):
                raise InternalError(f"the claim on {t_id}'s location records another uuid than its trash record, so its grants cannot be found")
            try:
                await run_in_threadpool(native.call, ns, "register_table", body)
            except TableAlreadyExistsError:
                log.info("undrop_table_already_registered", extra={"table": t_id})
            await run_in_threadpool(warn_if_mixed_file_versions, location, so, table=t_id)
            # Each re-registered table exists again, and lineage has to hear it, for the reason the table
            # undrop states ([[LH-144]]). Guarded per table like the cascade's own emits: one table's
            # failed announcement must not strand the rest of the recovery.
            try:
                await emit_write_event(
                    emitter,
                    parse_identifier(t_id),
                    author=token.subject if token is not None else None,
                    version=None,
                    operation=REGISTER_TABLE,
                    authorization=authorization,
                    uuids=uuids,
                    source_uri=location,
                )
            except Exception:
                log.exception("undrop_lineage_emit_failed", extra={"table": t_id})
        else:
            skipped += 1
            log.warning("undrop_skipped_declared_only_table", extra={"table": t_id})
        await run_in_threadpool(trash.clear, settings.registry_root, so, str(t_rec["uuid"]))
    # NO SEED — undrop is a PURE RESTORE (owner ruling, diff2 F10 item 4b), on this door for the same
    # reason as the table door.
    #
    # The comment that stood here made the argument against itself: "the recoverable cascade kept every
    # tuple (#75's rule), so the descendants' owners are intact — seeding the ROOT records the undrop
    # actor". If every tuple survived, there is nothing to restore; the seed only ADDED whoever pressed
    # the button, on the root of a whole subtree they may have had no prior claim to.
    #
    # It also deletes this door's worst residual. The seed carried `undo=None` deliberately, because by
    # this line the loop above has re-registered N tables and cleared N records, and a compensating
    # `drop_namespace` would destroy the cascade the user just recovered in order to repair one tuple.
    # That was the right call given a seed had to happen at all — and with no seed, the door has no
    # uncompensatable step left.
    await emit_control(
        control,
        action="namespace_undropped",
        object_type="namespace",
        object_id=fga.namespace_object(root_uuid),
        object_name=canonical,
        actor=f"user:{token.subject}" if token is not None else None,
        extra={"namespaces": len(namespace_records), "tables": len(table_records) - skipped, "declared_only_skipped": skipped},
    )
    return response


@management_router.get("/{id}/protection", response_model_exclude_none=True)
async def get_namespace_protection(id: str, ns: NamespaceDep, settings: SettingsDep) -> ProtectionResponse:
    """Read the namespace's deletion-protection flag (#123) — the table door's read, one rung up."""
    segments = parse_identifier(id)
    canonical = fga.canonical_object_id(segments)
    namespace = await fga_deps.identify(ns, settings, "namespace", segments)
    record = await run_in_threadpool(protection.get_protection, settings.registry_root, settings.storage_options(), "namespace", namespace.uuid)
    return ProtectionResponse(id=canonical, protected=bool(record), set_by=(record or {}).get("set_by"))


@management_router.post("/{id}/protection", response_model_exclude_none=True)
async def set_namespace_protection(
    id: str,
    body: SetProtectionRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    control: ControlEmitterDep,
) -> ProtectionResponse:
    """Set or clear deletion protection on namespace ``id`` (#73). Owner-gated by the router
    (``protection`` maps to ``can_delete`` — whoever may delete the namespace decides whether
    deleting it needs a second thought). Same control-root record contract as the table door."""
    segments = parse_identifier(id)
    canonical = fga.canonical_object_id(segments)
    namespace = await fga_deps.identify(ns, settings, "namespace", segments)
    so = settings.storage_options()
    if body.protected:
        record = {
            "kind": "namespace",
            "id": canonical,
            "uuid": namespace.uuid,
            "protected": "true",
            "set_by": f"user:{token.subject}" if token is not None else "anonymous",
        }
        await run_in_threadpool(protection.set_protection, settings.registry_root, so, record)
    else:
        await run_in_threadpool(protection.clear_protection, settings.registry_root, so, "namespace", namespace.uuid)
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
@router.post("/{id}/exists", status_code=200, response_class=Response)
def namespace_exists(id: str, ns: NamespaceDep, body: NamespaceExistsRequest | None = None) -> Response:
    """Check that namespace ``id`` exists via ``namespace_exists`` — 200 on success (spec 0.9), else error."""
    req = NamespaceExistsRequest(id=reconcile_body_id(parse_identifier(id), body.id if body else None))
    native.call(ns, "namespace_exists", req)
    return Response(status_code=200)


@router.get("/{id}/table/list", response_model_exclude_none=True)
async def list_tables(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    client: FgaClientDep,
    resolver: ResolverDep,
    page_token: str | None = None,
    limit: ListLimit = None,
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
    was ``false`` (403); after it, ``true`` (200, full listing). A table list is not a harmless header;
    ``viewer.api.security`` states the estate's position outright — "A corpus LIST is itself sensitive:
    it names data someone may not know exists."

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
