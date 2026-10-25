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

from catalog.api import fga_deps
from catalog.api.dependencies import ControlEmitterDep, DoorTableDep, SettingsDep, StorageOptionsDep
from catalog.api.pagination import ListLimit
from catalog.api.rask_params import RaskFlag
from catalog.api.security import CurrentToken
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.services import dataplane, publication
from service_kit.control_emit import emit_control
from service_kit.governed import fga


router = APIRouter(prefix="/v1/table", tags=["tag"])


def _refuse_a_catalog_tag(tag: str) -> None:
    """Refuse a tag the catalog moves itself (`publication.is_catalog_tag`), on every door that writes a tag.

    Raises:
        InvalidInputError: ``tag`` is `published`, `publishing` or under `rask.`.
    """
    if publication.is_catalog_tag(tag):
        raise InvalidInputError(
            f"tag {tag!r} is the catalog's own: `published` moves only through POST /management/v1/table/{{id}}/publish and a "
            "can_promote holder's approval of a held version, and the other catalog tags pin versions for those doors"
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
def list_table_tags(id: str, table: DoorTableDep, so: StorageOptionsDep, page_token: str | None = None, limit: ListLimit = None) -> ListTableTagsResponse:
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
    segments = parse_identifier(id)
    body.id = reconcile_body_id(segments, body.id)
    _refuse_a_catalog_tag(body.tag)
    response = dataplane.create_tag(table, so, body)
    # AFTER the data-plane call — a change that did not happen is never announced.
    await emit_control(
        control,
        action="table_tag_created",
        object_type="table",
        object_id=fga.table_object(table.resolved.require_uuid()),
        object_name=fga.canonical_object_id(segments),
        actor=f"user:{token.subject}" if token is not None else None,
        extra={"tag": body.tag, "version": body.version, **await run_in_threadpool(_ref_of, table.location, so, segments, body.branch)},
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
    token: CurrentToken = None,
) -> UpdateTableTagResponse:
    """Move an existing tag to a new table version — wraps lance_namespace UpdateTableTag.

    A MOVE IS NOT A CREATE. The name survives and the version under it changes, so a consumer holding
    "tag -> version" has stale state with no failing read to discover it by; `version` rides in `extra`
    so it can be corrected without a re-read.
    """
    segments = parse_identifier(id)
    body.id = reconcile_body_id(segments, body.id)
    _refuse_a_catalog_tag(body.tag)
    response = dataplane.update_tag(table, so, body)
    # AFTER the data-plane call — a change that did not happen is never announced.
    await emit_control(
        control,
        action="table_tag_updated",
        object_type="table",
        object_id=fga.table_object(table.resolved.require_uuid()),
        object_name=fga.canonical_object_id(segments),
        actor=f"user:{token.subject}" if token is not None else None,
        extra={"tag": body.tag, "version": body.version, **await run_in_threadpool(_ref_of, table.location, so, segments, body.branch)},
    )
    return response


@router.post("/{id}/tags/delete", response_model_exclude_none=True)
async def delete_table_tag(
    id: str,
    body: DeleteTableTagRequest,
    table: DoorTableDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    control: ControlEmitterDep,
    force: RaskFlag = False,
    token: CurrentToken = None,
) -> DeleteTableTagResponse:
    """Delete a tag from the table — wraps lance_namespace DeleteTableTag.

    PROTECTION-GATED ([[LH-056]]). A tag pins the version it names against cleanup, so a protected table
    refuses this on the same record its drop consults, and ``force`` turns that lock only. The catalog's own
    tags, ``published`` among them, are refused here whatever the table's protection says.
    """
    segments = parse_identifier(id)
    body.id = reconcile_body_id(segments, body.id)
    _refuse_a_catalog_tag(body.tag)
    await fga_deps.require_table_unprotected(settings, table.resolved, force=force)
    # The ref the tag pinned, read while the tag still exists: after the delete nothing records it.
    listed = await run_in_threadpool(dataplane.list_tags, table, so, ListTableTagsRequest(id=segments))
    pinned = (listed.tags or {}).get(body.tag)
    response = dataplane.delete_tag(table, so, body)
    # AFTER the data-plane call — a change that did not happen is never announced.
    await emit_control(
        control,
        action="table_tag_deleted",
        object_type="table",
        object_id=fga.table_object(table.resolved.require_uuid()),
        object_name=fga.canonical_object_id(segments),
        actor=f"user:{token.subject}" if token is not None else None,
        extra={"tag": body.tag, **await run_in_threadpool(_ref_of, table.location, so, segments, pinned.branch if pinned is not None else None)},
    )
    return response


def _ref_of(location: str, so: dict[str, str], segments: list[str], branch: str | None) -> dict[str, str]:
    """The ref a tag pins on the table at ``location``, for a tag event's ``extra``: nothing for main, else the branch and its identifier.

    A tag names a version WITHIN its branch's history, and branch histories are numbered independently,
    so ``version`` alone names a different snapshot on main. The identifier says which incarnation of the
    branch: a recreated branch restarts its numbering. Absent for main, as the lineage event's ``ref`` is.
    """
    ref = dataplane.recorded_branch(branch)
    if ref is None:
        return {}
    identifier = dataplane.branch_identifier(location, so, segments, ref)
    return {"branch": ref, **({dataplane.BRANCH_IDENTIFIER_KEY: identifier} if identifier is not None else {})}
