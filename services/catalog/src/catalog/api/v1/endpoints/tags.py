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
    InvalidInputError,
    ListTableTagsRequest,
    ListTableTagsResponse,
    UpdateTableTagRequest,
    UpdateTableTagResponse,
)

from catalog.api.announcing import AnnouncerDep
from catalog.api.dependencies import ControlEmitterDep, DoorTableDep, SettingsDep, StorageOptionsDep
from catalog.api.pagination import ListLimit
from catalog.api.ref_notices import putting_ref
from catalog.api.security import CurrentToken
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.services import dataplane
from lakehouse import clone_sources
from lakehouse.control_emit import emit_control
from lakehouse.governed import fga


router = APIRouter(prefix="/v1/table", tags=["tag"])


def _refuse_a_catalog_tag(tag: str) -> None:
    """Refuse a tag the catalog writes itself, on every door that writes a tag: the tag a registered clone holds on the
    source version it reads (`lakehouse.clone_sources`), which the clone's own end releases. Every other name is the
    caller's, `published` and `stable` included: marking a version ready is a pipeline's own tag (ADR 0212).

    Raises:
        InvalidInputError: ``tag`` is under `rask.clone.`.
    """
    if tag.startswith(clone_sources.TAG_PREFIX):
        raise InvalidInputError(
            f"tag {tag!r} is the catalog's own: a tag under {clone_sources.TAG_PREFIX!r} keeps the version a registered clone reads, "
            "and is released when that clone ends"
        )


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
def list_table_tags(id: str, table: DoorTableDep, so: StorageOptionsDep, limit: ListLimit, page_token: str | None = None) -> ListTableTagsResponse:
    """List the table's tags, a page at a time — wraps lance_namespace ListTableTags."""
    req = ListTableTagsRequest(id=parse_identifier(id), page_token=page_token, limit=limit)
    return dataplane.list_tags(table, so, req)


@router.post("/{id}/tags/create", response_model_exclude_none=True)
async def create_table_tag(
    id: str,
    body: CreateTableTagRequest,
    table: DoorTableDep,
    so: StorageOptionsDep,
    control: ControlEmitterDep,
    settings: SettingsDep,
    announcer: AnnouncerDep,
    token: CurrentToken = None,
) -> CreateTableTagResponse:
    """Tag the given table version with a name — wraps lance_namespace CreateTableTag.

    ANNOUNCED ON THE CONTROL LANE ([[LH-056]]) and UNTARGETED, by the rule the estate codified: a
    control event is targeted when it changes what a specific PERSON may do or must do, not when it
    changes an object. A tag is a ref, so this joins the members that name nobody.

    A TAG IS HOW A PIPELINE MARKS A VERSION READY (`lance_docs/lance/guide/tags_and_branches.md`), under a name it
    chooses, so each of the three ref mutations is announced: a reader holding "tag -> version" learns it moved.

    A TAG AN ERASURE'S RELEASE WILL TAKE IS TOLD OF TO ITS CALLER (ADR 0233 §9): when a release scheduled on the table takes
    the tag (its version holds the erased subject, or it is on a branch the release recreates), the caller receives a
    targeted `erasure_release_scheduled` naming the tag and the release's deadline, filed before the tag is written.
    """
    segments = parse_identifier(id)
    body.id = reconcile_body_id(segments, body.id)
    _refuse_a_catalog_tag(body.tag)
    response = await putting_ref(
        lambda before_ref: dataplane.create_tag(table, so, body, before_ref=before_ref), table=table, settings=settings, announcer=announcer, token=token
    )
    # AFTER the data-plane call — a change that did not happen is never announced.
    await emit_control(
        control,
        action="table_tag_created",
        object_type="table",
        object_id=fga.table_object(table.resolved.require_uuid()),
        object_name=fga.canonical_object_id(segments),
        actor=f"user:{token.subject}" if token is not None else None,
        extra={"tag": body.tag, "version": body.version, **await run_in_threadpool(dataplane.tag_event_ref, table.location, so, segments, body.branch)},
    )
    return response


@router.post("/{id}/tags/version", response_model_exclude_none=True)
def get_table_tag_version(id: str, body: GetTableTagVersionRequest, table: DoorTableDep, so: StorageOptionsDep) -> GetTableTagVersionResponse:
    """Resolve which table version a tag points to — wraps lance_namespace GetTableTagVersion."""
    body.id = reconcile_body_id(parse_identifier(id), body.id)
    return dataplane.get_tag_version(table, so, body)


@router.post("/{id}/tags/update", response_model_exclude_none=True)
async def update_table_tag(
    id: str,
    body: UpdateTableTagRequest,
    table: DoorTableDep,
    so: StorageOptionsDep,
    control: ControlEmitterDep,
    settings: SettingsDep,
    announcer: AnnouncerDep,
    token: CurrentToken = None,
) -> UpdateTableTagResponse:
    """Move an existing tag to a new table version — wraps lance_namespace UpdateTableTag.

    A MOVE IS NOT A CREATE. The name survives and the version under it changes, so a consumer holding
    "tag -> version" has stale state with no failing read to discover it by; `version` rides in `extra`
    so it can be corrected without a re-read. A tag moved onto a version an erasure's release will take is told of to the
    caller as a created one is.
    """
    segments = parse_identifier(id)
    body.id = reconcile_body_id(segments, body.id)
    _refuse_a_catalog_tag(body.tag)
    response = await putting_ref(
        lambda before_ref: dataplane.update_tag(table, so, body, before_ref=before_ref), table=table, settings=settings, announcer=announcer, token=token
    )
    # AFTER the data-plane call — a change that did not happen is never announced.
    await emit_control(
        control,
        action="table_tag_updated",
        object_type="table",
        object_id=fga.table_object(table.resolved.require_uuid()),
        object_name=fga.canonical_object_id(segments),
        actor=f"user:{token.subject}" if token is not None else None,
        extra={"tag": body.tag, "version": body.version, **await run_in_threadpool(dataplane.tag_event_ref, table.location, so, segments, body.branch)},
    )
    return response


@router.post("/{id}/tags/delete", response_model_exclude_none=True)
async def delete_table_tag(
    id: str,
    body: DeleteTableTagRequest,
    table: DoorTableDep,
    so: StorageOptionsDep,
    control: ControlEmitterDep,
    token: CurrentToken = None,
) -> DeleteTableTagResponse:
    """Delete a tag from the table — wraps lance_namespace DeleteTableTag.

    Authorization alone governs it (``can_drop``): a tag pins the version it names against cleanup, so deleting
    one makes that version reclaimable, which is history reclaim, and deletion protection guards dropping the
    table, not its history (ADR 0150). The catalog's own clone tags are refused here.
    """
    segments = parse_identifier(id)
    body.id = reconcile_body_id(segments, body.id)
    _refuse_a_catalog_tag(body.tag)
    # The ref the tag pinned, read while the tag still exists: after the delete nothing records it.
    listed = await run_in_threadpool(dataplane.list_tags, table, so, ListTableTagsRequest(id=segments))
    pinned = (listed.tags or {}).get(body.tag)
    response = await run_in_threadpool(dataplane.delete_tag, table, so, body)
    # AFTER the data-plane call — a change that did not happen is never announced.
    await emit_control(
        control,
        action="table_tag_deleted",
        object_type="table",
        object_id=fga.table_object(table.resolved.require_uuid()),
        object_name=fga.canonical_object_id(segments),
        actor=f"user:{token.subject}" if token is not None else None,
        extra={
            "tag": body.tag,
            **await run_in_threadpool(dataplane.tag_event_ref, table.location, so, segments, pinned.branch if pinned is not None else None),
        },
    )
    return response
