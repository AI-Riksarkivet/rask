"""Namespace metadata endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    CreateNamespaceRequest,
    CreateNamespaceResponse,
    DescribeNamespaceRequest,
    DescribeNamespaceResponse,
    DropNamespaceRequest,
    DropNamespaceResponse,
    LanceNamespace,
    ListNamespacesRequest,
    ListNamespacesResponse,
    ListTablesRequest,
    ListTablesResponse,
    NamespaceExistsRequest,
)
from pydantic import BaseModel

from catalog.api import fga_deps
from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, NamespaceDep, SettingsDep
from catalog.api.security import CurrentToken
from catalog.core.control_emit import emit_control
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.services import native
from service_kit.governed import fga
from service_kit.lakehouse import protection


log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/namespace", tags=["namespace"])

# Safety ceiling on the pagination loops in _collect_descendants — a runaway/looping backend token can't
# spin forever. Far above any real namespace fan-out.
_MAX_LIST_PAGES = 1000


def _collect_descendants(ns: LanceNamespace, segments: list[str]) -> list[tuple[str, list[str]]]:
    """Every ``(resource, segments)`` under ``segments`` — child tables AND nested namespaces, recursively.

    Enumerated BEFORE a Cascade drop removes them (afterwards they can't be listed), so the caller can
    revoke their FGA tuples once the drop commits. ``include_declared`` catches declared-only tables (they
    hold an owner grant too). Blocking native list calls → the caller runs this in a threadpool.
    """
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
    body: CreateNamespaceRequest | None = None,
) -> CreateNamespaceResponse:
    """Create a namespace via ``create_namespace``, then seed its FGA owner + parent edge."""
    segments = parse_identifier(id, settings.delimiter)
    # A top-level namespace needs a warehouse to live in. This door cannot name one, so it is refused
    # here and the caller is sent to the warehouse-scoped route; checked BEFORE the native create, so a
    # refusal leaves nothing half-made. Nested namespaces inherit their parent's binding and pass.
    fga_deps.require_warehouse_scoped(segments, delimiter=settings.delimiter, warehouses_enabled=settings.warehouses_enabled)
    req = body or CreateNamespaceRequest()
    req.id = reconcile_body_id(segments, req.id)
    response: CreateNamespaceResponse = await run_in_threadpool(native.call, ns, "create_namespace", req)
    # Owner + parent edge (parent namespace if nested, else the catalog root) so the
    # concentric cascade reaches the namespace and its tables — stops a nested-namespace
    # lockout and lets a layer-level grant (medallion bronze/silver/gold) reach children.
    await fga_deps.seed_ownership(client, settings, token, resource="namespace", segments=segments)
    await emit_control(
        control,
        action="namespace_created",
        object_type="namespace",
        object_id=f"namespace:{id}",
        actor=f"user:{token.sub}" if token else None,
        extra={"mode": req.mode, "properties": req.properties},
    )
    return response


@router.get("/{id}/list", response_model_exclude_none=True)
def list_namespaces(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    page_token: str | None = None,
    limit: int | None = None,
) -> ListNamespacesResponse:
    """List the child namespaces under ``id`` via ``list_namespaces`` (page_token/limit paged)."""
    req = ListNamespacesRequest(id=parse_identifier(id, settings.delimiter), page_token=page_token, limit=limit)
    return native.call(ns, "list_namespaces", req)


@router.post("/{id}/describe", response_model_exclude_none=True)
def describe_namespace(id: str, ns: NamespaceDep, settings: SettingsDep) -> DescribeNamespaceResponse:
    """Return the metadata/properties of namespace ``id`` via ``describe_namespace``."""
    req = DescribeNamespaceRequest(id=parse_identifier(id, settings.delimiter))
    return native.call(ns, "describe_namespace", req)


@router.post("/{id}/drop", response_model_exclude_none=True)
async def drop_namespace(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
    body: DropNamespaceRequest | None = None,
    force: bool = False,
) -> DropNamespaceResponse:
    """Drop namespace ``id`` (``drop_namespace``); revoke its FGA tuples — and, for a Cascade drop, every
    dropped child's — so a reused id can't inherit stale grants.

    Deletion protection (#73): a ``protected`` control-root record refuses 409 unless ``force=true``
    — and it also stops a CASCADE from a parent taking this namespace with it, because the cascade
    arrives at this same door. ``force`` turns the protection lock only; the FGA gate ran first."""
    segments = parse_identifier(id, settings.delimiter)
    canonical = fga.canonical_object_id(segments, delimiter=settings.delimiter)
    guard = await run_in_threadpool(protection.get_protection, settings.registry_root, settings.storage_options(), "namespace", canonical)
    fga_deps.require_not_protected(guard or {}, kind="namespace", obj_id=canonical, force=force)
    req = body or DropNamespaceRequest()
    req.id = reconcile_body_id(segments, req.id)
    # A Cascade drop (behavior=Cascade; case-insensitive per the lance spec) removes all child tables +
    # nested namespaces from storage. Their FGA grants must be revoked too, or a later object reusing a
    # child id would inherit the stale owner/reader/writer tuples (privilege bleed). Enumerate the
    # descendants BEFORE the drop (afterwards they can't be listed); only when FGA is on (else the revoke
    # loop is a no-op and the listing is wasted work). Restrict (the dir-backend default) errors on a
    # non-empty namespace, so there are never extra tuples to revoke on that path.
    cascade = (req.behavior or "").lower() == "cascade"
    descendants: list[tuple[str, list[str]]] = []
    if cascade and settings.fga_enabled and client is not None:
        descendants = await run_in_threadpool(_collect_descendants, ns, segments)
    response: DropNamespaceResponse = await run_in_threadpool(native.call, ns, "drop_namespace", req)
    # The record dies with the object — a reused id must not inherit protection nobody set on it.
    await run_in_threadpool(protection.clear_protection, settings.registry_root, settings.storage_options(), "namespace", canonical)
    # Revoke AFTER the drop commits (so a failed/restricted drop leaves the still-valid grants in place):
    # the namespace's own tuples, then every cascaded descendant's.
    await fga_deps.revoke_ownership(client, settings, resource="namespace", segments=segments, token=token)
    for resource, child_segments in descendants:
        await fga_deps.revoke_ownership(client, settings, resource=resource, segments=child_segments, token=token)
    await emit_control(
        control,
        action="namespace_dropped",
        object_type="namespace",
        object_id=f"namespace:{id}",
        actor=f"user:{token.sub}" if token else None,
        extra={"cascade": cascade, "descendants_revoked": len(descendants)},
    )
    return response


class SetProtectionRequest(BaseModel):
    """The one field this door writes. Setting is idempotent; clearing removes the record."""

    protected: bool


class ProtectionResponse(BaseModel):
    id: str
    protected: bool


@router.post("/{id}/protection", response_model_exclude_none=True)
async def set_namespace_protection(
    id: str,
    body: SetProtectionRequest,
    settings: SettingsDep,
    token: CurrentToken,
    control: ControlEmitterDep,
) -> ProtectionResponse:
    """Set or clear deletion protection on namespace ``id`` (#73). Owner-gated by the router
    (``protection`` maps to ``can_delete`` — whoever may delete the namespace decides whether
    deleting it needs a second thought). Same control-root record contract as the table door."""
    segments = parse_identifier(id, settings.delimiter)
    canonical = fga.canonical_object_id(segments, delimiter=settings.delimiter)
    so = settings.storage_options()
    if body.protected:
        record = {
            "kind": "namespace",
            "id": canonical,
            "protected": "true",
            "set_by": f"user:{token.sub}" if token is not None else "anonymous",
        }
        await run_in_threadpool(protection.set_protection, settings.registry_root, so, record)
    else:
        await run_in_threadpool(protection.clear_protection, settings.registry_root, so, "namespace", canonical)
    await emit_control(
        control,
        action="namespace_protected" if body.protected else "namespace_unprotected",
        object_type="namespace",
        object_id=f"namespace:{canonical}",
        actor=f"user:{token.sub}" if token is not None else None,
        extra={},
    )
    return ProtectionResponse(id=canonical, protected=body.protected)


@router.post("/{id}/exists", status_code=200)
def namespace_exists(id: str, ns: NamespaceDep, settings: SettingsDep) -> None:
    """Check that namespace ``id`` exists via ``namespace_exists`` — 200 on success (spec 0.9), else error."""
    req = NamespaceExistsRequest(id=parse_identifier(id, settings.delimiter))
    native.call(ns, "namespace_exists", req)


@router.get("/{id}/table/list", response_model_exclude_none=True)
def list_tables(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    page_token: str | None = None,
    limit: int | None = None,
    include_declared: bool = True,
) -> ListTablesResponse:
    """List the tables under namespace ``id`` via ``list_tables`` (page_token/limit paged);
    ``include_declared=false`` drops declared-only tables (reserved, no storage yet)."""
    req = ListTablesRequest(
        id=parse_identifier(id, settings.delimiter),
        page_token=page_token,
        limit=limit,
        include_declared=include_declared,
    )
    return native.call(ns, "list_tables", req)
