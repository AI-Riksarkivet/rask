"""Tag endpoints (implemented via the pylance data plane)."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    CreateTableTagRequest,
    CreateTableTagResponse,
    DeleteTableTagRequest,
    DeleteTableTagResponse,
    GetTableTagVersionRequest,
    GetTableTagVersionResponse,
    ListTableTagsRequest,
    ListTableTagsResponse,
    UpdateTableTagRequest,
    UpdateTableTagResponse,
)

from catalog.api import fga_deps
from catalog.api.dependencies import ControlEmitterDep, NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.api.rask_params import RaskFlag
from catalog.api.security import CurrentToken
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.services import dataplane
from service_kit.control_emit import emit_control
from service_kit.governed import fga
from service_kit.lakehouse import protection


router = APIRouter(prefix="/v1/table", tags=["tag"])


# DUAL-MOUNTED, and the reason is upstream disagreeing with itself. The spec says POST at every
# tag from v0.9.0 to v0.12.0, and lance-namespace's own generated reqwest client sends POST — but
# the REST client pylance BUNDLES from the lance repo (`rust/lance-namespace-impls/src/rest.rs`)
# calls `get_json` for this op, and the reference server it bundles mounts it as GET. Since
# `lance_namespace.connect("rest", …)` resolves to that class, a POST-only route answered every
# Python user of the "rest" alias with FastAPI's default 405, which carries no `code` and so
# surfaces as `InternalError 18`. Serving both is the local fix; the upstream fix is one line in
# lance and is worth filing.
# TWO DECORATORS, NOT `api_route(methods=[...])`, and the difference is not style. One `api_route`
# with both methods emits ONE operationId for both, and FastAPI derives its suffix from whichever
# method it happened to register last — so the generated OpenAPI flipped between `_get` and
# `_post` between runs, which is invalid (operationIds must be unique) and made the contract gate
# flip-flop. Explicit ids keep the spec's POST canonical and name the GET for what it is.
@router.post("/{id}/tags/list", response_model_exclude_none=True, operation_id="list_table_tags")
@router.get("/{id}/tags/list", response_model_exclude_none=True, operation_id="list_table_tags_compat_get")
def list_table_tags(id: str, ns: NamespaceDep, settings: SettingsDep, so: StorageOptionsDep) -> ListTableTagsResponse:
    """List every tag on the table — wraps lance_namespace ListTableTags."""
    req = ListTableTagsRequest(id=parse_identifier(id, settings.delimiter))
    return dataplane.list_tags(ns, so, req)


@router.post("/{id}/tags/create", response_model_exclude_none=True)
async def create_table_tag(
    id: str,
    body: CreateTableTagRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    control: ControlEmitterDep,
    token: CurrentToken = None,
) -> CreateTableTagResponse:
    """Tag the given table version with a name — wraps lance_namespace CreateTableTag.

    ANNOUNCED ON THE CONTROL LANE ([[LH-056]]) and UNTARGETED, by the rule the estate codified: a
    control event is targeted when it changes what a specific PERSON may do or must do, not when it
    changes an object. A tag is a ref, so this joins the members that name nobody.

    THE REF PLANE IS WHERE `published` LIVES, which is why these three matter more than their size
    suggests: `table_published` already announces the publication tag moving, and until now the OTHER
    ref mutations moved in silence — a console could not tell a tag was gone.
    """
    segments = parse_identifier(id, settings.delimiter)
    body.id = reconcile_body_id(segments, body.id)
    response = dataplane.create_tag(ns, so, body)
    # AFTER the data-plane call — a change that did not happen is never announced.
    await emit_control(
        control,
        action="table_tag_created",
        object_type="table",
        object_id=f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}",
        actor=f"user:{token.sub}" if token is not None else None,
        extra={"tag": body.tag, "version": body.version},
    )
    return response


@router.post("/{id}/tags/version", response_model_exclude_none=True)
def get_table_tag_version(
    id: str, body: GetTableTagVersionRequest, ns: NamespaceDep, settings: SettingsDep, so: StorageOptionsDep
) -> GetTableTagVersionResponse:
    """Resolve which table version a tag points to — wraps lance_namespace GetTableTagVersion."""
    body.id = reconcile_body_id(parse_identifier(id, settings.delimiter), body.id)
    return dataplane.get_tag_version(ns, so, body)


@router.post("/{id}/tags/update", response_model_exclude_none=True)
async def update_table_tag(
    id: str,
    body: UpdateTableTagRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    control: ControlEmitterDep,
    token: CurrentToken = None,
) -> UpdateTableTagResponse:
    """Move an existing tag to a new table version — wraps lance_namespace UpdateTableTag.

    A MOVE IS NOT A CREATE. The name survives and the version under it changes, so a consumer holding
    "tag -> version" has stale state with no failing read to discover it by; `version` rides in `extra`
    so it can be corrected without a re-read.
    """
    segments = parse_identifier(id, settings.delimiter)
    body.id = reconcile_body_id(segments, body.id)
    response = dataplane.update_tag(ns, so, body)
    # AFTER the data-plane call — a change that did not happen is never announced.
    await emit_control(
        control,
        action="table_tag_updated",
        object_type="table",
        object_id=f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}",
        actor=f"user:{token.sub}" if token is not None else None,
        extra={"tag": body.tag, "version": body.version},
    )
    return response


@router.post("/{id}/tags/delete", response_model_exclude_none=True)
async def delete_table_tag(
    id: str,
    body: DeleteTableTagRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    control: ControlEmitterDep,
    force: RaskFlag = False,
    token: CurrentToken = None,
) -> DeleteTableTagResponse:
    """Delete a tag from the table — wraps lance_namespace DeleteTableTag.

    PROTECTION-GATED ([[LH-056]]). A tag is the pin ``published`` is made of, and the publication
    door's rollback guard rests on it: publication refuses to move ``published`` BACKWARDS and has
    nothing to say about republishing after the tag is gone. So a protected table refuses this on the
    same record its drop consults, and ``force`` turns that lock only.
    """
    segments = parse_identifier(id, settings.delimiter)
    body.id = reconcile_body_id(segments, body.id)
    canonical = fga.canonical_object_id(segments, delimiter=settings.delimiter)
    guard = await run_in_threadpool(protection.get_protection, settings.registry_root, settings.storage_options(), "table", canonical)
    fga_deps.require_not_protected(guard or {}, kind="table", obj_id=canonical, force=force)
    response = dataplane.delete_tag(ns, so, body)
    # AFTER the data-plane call — a change that did not happen is never announced.
    await emit_control(
        control,
        action="table_tag_deleted",
        object_type="table",
        object_id=f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}",
        actor=f"user:{token.sub}" if token is not None else None,
        extra={"tag": body.tag},
    )
    return response
