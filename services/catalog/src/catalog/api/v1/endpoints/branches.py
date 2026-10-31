"""Table branch endpoints — backed in-process via the pylance data plane.

The native ``DirectoryNamespace`` 501s branch ops, but ``lance.LanceDataset`` implements Git-like branches
(``ds.branches`` / ``ds.create_branch``), so we back them in-process here exactly like tags — turning the
former spec-correct 501 into a real, working operation.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    CreateTableBranchRequest,
    CreateTableBranchResponse,
    DeleteTableBranchRequest,
    DeleteTableBranchResponse,
    ListTableBranchesRequest,
    ListTableBranchesResponse,
)

from catalog.api.dependencies import ControlEmitterDep, DoorTableDep, StorageOptionsDep
from catalog.api.pagination import ListLimit
from catalog.api.security import Caller, CurrentToken
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.services import dataplane
from catalog.services.table_claims import DoorTable
from service_kit.control_emit import ControlEmitter, emit_control
from service_kit.governed import fga
from service_kit.lakehouse.objectfs import StorageOptions


log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/table", tags=["branch"])


@router.post("/{id}/branches/list", response_model_exclude_none=True)
def list_table_branches(
    id: str,
    table: DoorTableDep,
    so: StorageOptionsDep,
    page_token: str | None = None,
    limit: ListLimit = None,
) -> ListTableBranchesResponse:
    """List a table's Git-like branches (paginated) — wraps the pylance ``list_branches`` data-plane op."""
    req = ListTableBranchesRequest(id=parse_identifier(id), page_token=page_token, limit=limit)
    return dataplane.list_branches(table, so, req)


@router.post("/{id}/branches/create", response_model_exclude_none=True)
async def create_table_branch(
    id: str,
    body: CreateTableBranchRequest,
    table: DoorTableDep,
    so: StorageOptionsDep,
    control: ControlEmitterDep,
    token: CurrentToken = None,
) -> CreateTableBranchResponse:
    """Create a branch from main (or a source branch/version) — wraps pylance ``create_branch``.

    ANNOUNCED ON THE CONTROL LANE ([[LH-056]]) and UNTARGETED. The rule the estate codified is that a
    control event is targeted when it changes what a specific PERSON may do or must do, not when it
    changes an object; a branch appearing changes an object, so it joins the 31 members that name
    nobody and stays out of notifications' `NAMED_ACTIONS`. A console invalidating a branch list is
    the consumer this serves.
    """
    segments = parse_identifier(id)
    body.id = reconcile_body_id(segments, body.id)
    response = await run_in_threadpool(dataplane.create_branch, table, so, body)
    # AFTER the data-plane call, never before — the rule `namespaces.py` states: a change that did not
    # happen is never announced, so a refused create emits nothing.
    await emit_control(
        control,
        action="table_branch_created",
        object_type="table",
        object_id=fga.table_object(table.resolved.require_uuid()),
        object_name=fga.canonical_object_id(segments),
        actor=f"user:{token.subject}" if token is not None else None,
        # `name`, not `branch` — that is the field `CreateTableBranchRequest` declares. "Branched from
        # what" is the question a consumer asks next, and it is answered from the DATASET rather than
        # from the request: a branch taken from main sends no `from_version` and records main's current
        # version, so announcing the request answered `null` for the case that happens most.
        extra={"branch": body.name, **_recorded_parent(table, so, body)},
    )
    return response


def _recorded_parent(table: DoorTable, so: StorageOptions, body: CreateTableBranchRequest) -> dict[str, Any]:
    """The parent the dataset recorded for a just-created branch, or nothing.

    ONE EXTRA READ PER CREATE, which is a rare operation and a permanent announcement: a console that
    stored the wrong ancestor has no way to learn otherwise without going back to the catalog, which is
    what the event exists to save it.

    NOTHING, never the request, when the read fails. The request's values are what this replaces and
    they are null exactly where the answer matters, so falling back to them would restore the defect
    silently on the one path where the authoritative read is unavailable. An absent field is a consumer
    that asks; a null one is a consumer that believes.

    Named for what they carry, matching `list_branches`. `from_version` already means something else in
    this estate — ingest's publish delta range — and `extra` has no schema to keep the two apart.
    """
    try:
        listed = dataplane.list_branches(table, so, ListTableBranchesRequest(id=body.id))
        entry = (listed.branches or {}).get(body.name)
    except Exception:  # noqa: BLE001 — an announcement must survive an unreadable parent
        log.warning("branch_parent_unreadable", extra={"branch": body.name})
        return {}
    if entry is None:
        return {}
    # ATTRIBUTES, not `.get` — `ListTableBranchesResponse.branches` holds `BranchContents` models and a
    # dict-shaped read raises. The data-plane builds them from plain dicts, which is what makes the
    # mistake easy and silent right up to the AttributeError.
    recorded: dict[str, Any] = {"parent_branch": entry.parent_branch, "parent_version": entry.parent_version}
    # WHICH INCARNATION was created: a branch deleted and created again under the same name restarts its
    # version numbering, so a consumer keyed on the name alone cannot tell the two apart.
    if (identifier := (entry.metadata or {}).get(dataplane.BRANCH_IDENTIFIER_KEY)) is not None:
        recorded[dataplane.BRANCH_IDENTIFIER_KEY] = identifier
    return recorded


@router.post("/{id}/branches/delete", response_model_exclude_none=True)
async def delete_table_branch(
    id: str,
    body: DeleteTableBranchRequest,
    table: DoorTableDep,
    so: StorageOptionsDep,
    control: ControlEmitterDep,
    token: CurrentToken = None,
) -> DeleteTableBranchResponse:
    """Delete a branch from the table — wraps the pylance ``delete_branch`` data-plane op.

    The disappearance is the half worth announcing: a console holding a branch list has no other way
    to learn the branch is gone, and a reader that polls discovers it by a failing read. So EVERY branch the
    delete removes is announced, each with the incarnation it was ([[LH-413]]): the branches nested inside the
    named one go with it, and a delete that stops part-way still announces those it removed before stopping.

    Authorization alone governs it (``can_drop``): a branch holds the files it references against cleanup, so
    deleting one makes them reclaimable, which is history reclaim, and deletion protection guards dropping the
    table, not its history (ADR 0150).
    """
    segments = parse_identifier(id)
    body.id = reconcile_body_id(segments, body.id)
    try:
        removed = await run_in_threadpool(dataplane.delete_branch, table, so, body)
    except dataplane.BranchDeleteStoppedError as stopped:
        await _announce_removed(control, table, segments, token, stopped.removed)
        raise
    await _announce_removed(control, table, segments, token, removed)
    return DeleteTableBranchResponse()


async def _announce_removed(
    control: ControlEmitter, table: DoorTable, segments: list[str], token: Caller | None, removed: list[dataplane.RemovedBranch]
) -> None:
    """One ``table_branch_deleted`` per branch removed, AFTER its delete: each names the incarnation it was, which the
    name alone does not, since it also names whatever branch is created under it next."""
    for branch in removed:
        await emit_control(
            control,
            action="table_branch_deleted",
            object_type="table",
            object_id=fga.table_object(table.resolved.require_uuid()),
            object_name=fga.canonical_object_id(segments),
            actor=f"user:{token.subject}" if token is not None else None,
            extra={"branch": branch.name, **({dataplane.BRANCH_IDENTIFIER_KEY: branch.identifier} if branch.identifier is not None else {})},
        )
