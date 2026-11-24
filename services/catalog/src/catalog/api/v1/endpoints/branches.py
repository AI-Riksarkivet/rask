"""Table branch endpoints — backed in-process via the pylance data plane.

The native ``DirectoryNamespace`` answers Unsupported to branch ops, but ``lance.LanceDataset`` implements Git-like
branches (``ds.branches`` / ``ds.create_branch``), so we back them in-process here exactly like tags, as real, working
operations.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    CreateTableBranchRequest,
    CreateTableBranchResponse,
    DeleteTableBranchRequest,
    DeleteTableBranchResponse,
    InvalidTableStateError,
    ListTableBranchesRequest,
    ListTableBranchesResponse,
)
from pydantic import BaseModel, ConfigDict

from catalog.api.dependencies import ClockDep, ControlEmitterDep, DoorTableDep, SettingsDep, StorageOptionsDep
from catalog.api.pagination import ListLimit
from catalog.api.security import Caller, CurrentToken
from catalog.core.config import Settings
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.services import erasure_obligations, erasure_release, warehouses
from catalog.services.table_claims import DoorTable
from catalog.services.tables import hidden_branches, refs, versions
from lakehouse.events.control_emit import ControlEmitter, emit_control
from lakehouse.governed import fga_ids
from lakehouse.process.supervision import is_fault
from lakehouse.storage.options import StorageOptions


log = logging.getLogger(__name__)

spec_router = APIRouter(prefix="/v1/table", tags=["branch"])
management_router = APIRouter(prefix="/management/v1/table", tags=["branch"])


@spec_router.post("/{id}/branches/list", response_model_exclude_none=True)
def list_table_branches(
    *,
    id: str,
    table: DoorTableDep,
    so: StorageOptionsDep,
    limit: ListLimit,
    page_token: str | None = None,
) -> ListTableBranchesResponse:
    """List a table's Git-like branches (paginated) — wraps the pylance ``list_branches`` data-plane op."""
    req = ListTableBranchesRequest(id=parse_identifier(id), page_token=page_token, limit=limit)
    return refs.list_branches(table, so, req)


@spec_router.post("/{id}/branches/create", response_model_exclude_none=True)
async def create_table_branch(
    *,
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
    nobody, so no notifier targets it. A console invalidating a branch list is
    the consumer this serves.
    """
    segments = parse_identifier(id)
    body.id = reconcile_body_id(segments, body.id)
    response = await run_in_threadpool(refs.create_branch, table, so, body)
    # AFTER the data-plane call, never before — the rule `namespaces.py` states: a change that did not
    # happen is never announced, so a refused create emits nothing.
    await emit_control(
        control,
        action="table_branch_created",
        object_type="table",
        object_id=fga_ids.table_object(table.resolved.require_uuid()),
        object_name=fga_ids.canonical_object_id(segments),
        actor=f"user:{token.subject}" if token is not None else None,
        # `name`, not `branch` — that is the field `CreateTableBranchRequest` declares. "Branched from
        # what" is the question a consumer asks next, and it is answered from the DATASET rather than
        # from the request: a branch taken from main sends no `from_version` and records main's current
        # version, so announcing the request answered `null` for the case that happens most.
        extra={"branch": body.name, **await run_in_threadpool(_recorded_parent, table, so, body)},
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
        listed = refs.list_branches(table, so, ListTableBranchesRequest(id=body.id))
        entry = (listed.branches or {}).get(body.name)
    except BaseException as exc:  # noqa: BLE001 — an announcement must survive an unreadable parent, a native panic too
        if not is_fault(exc):
            raise
        log.warning("branch_parent_unreadable", extra={"branch": body.name, "error": str(exc)[:300], "error_type": type(exc).__name__})
        return {}
    if entry is None:
        return {}
    # ATTRIBUTES, not `.get` — `ListTableBranchesResponse.branches` holds `BranchContents` models and a
    # dict-shaped read raises. The data-plane builds them from plain dicts, which is what makes the
    # mistake easy and silent right up to the AttributeError.
    recorded: dict[str, Any] = {"parent_branch": entry.parent_branch, "parent_version": entry.parent_version}
    # WHICH INCARNATION was created: a branch deleted and created again under the same name restarts its
    # version numbering, so a consumer keyed on the name alone cannot tell the two apart.
    if (identifier := (entry.metadata or {}).get(versions.BRANCH_IDENTIFIER_KEY)) is not None:
        recorded[versions.BRANCH_IDENTIFIER_KEY] = identifier
    return recorded


@spec_router.post("/{id}/branches/delete", response_model_exclude_none=True)
async def delete_table_branch(
    *,
    id: str,
    body: DeleteTableBranchRequest,
    table: DoorTableDep,
    so: StorageOptionsDep,
    control: ControlEmitterDep,
    settings: SettingsDep,
    clock: ClockDep,
    token: CurrentToken = None,
) -> DeleteTableBranchResponse:
    """Delete a branch from the table — wraps the pylance ``delete_branch`` data-plane op.

    Where the table's warehouse keeps what is dropped for a while (its delete profile, else ``LANCE_TRASH_GRACE_DAYS``),
    the branch is HIDDEN for that long instead of destroyed ([[LH-429]]): no listing names it and no door opens it, and
    ``POST /management/v1/table/{id}/branches/restore`` brings it back with every commit made on it. It is announced as
    deleted either way, since it is gone from every reader's view.

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
    hiding = await _hiding(settings, table, segments, by=token.subject if token is not None else "anonymous", now=clock())
    try:
        removed = await run_in_threadpool(partial(refs.delete_branch, table, so, body, hiding=hiding))
    except (refs.BranchDeleteStoppedError, refs.BranchHideStoppedError) as stopped:
        await _announce_removed(control, table, segments, token=token, removed=stopped.removed)
        raise
    await _announce_removed(control, table, segments, token=token, removed=removed)
    return DeleteTableBranchResponse()


def _erasure_in_flight(settings: Settings, table_uuid: str) -> bool:
    """Whether an erasure of the table is recorded and not finished: an obligation no verification has settled, or a
    release of its pins still scheduled. BLOCKING IO."""
    root, options = settings.registry_root, settings.storage_options()
    return bool(erasure_obligations.open_on(root, options, table_uuid)) or erasure_release.stands_on(root, options, table_uuid)


async def _hiding(settings: Settings, table: DoorTable, segments: list[str], *, by: str, now: datetime) -> Callable[[], hidden_branches.Hiding | None] | None:
    """How a recoverable delete of the table's branches decides, or ``None`` where every delete destroys: the table's
    warehouse keeps nothing, or no uuid identifies the table. The decision is asked inside the delete's hold: a deadline
    to hide until, or ``None`` while an erasure of the table is in flight, whose job and verification judge only the refs
    that stand."""
    table_uuid = table.resolved.uuid
    if table_uuid is None:
        return None
    days = await run_in_threadpool(
        partial(warehouses.grace_days_under, settings.registry_root, settings.storage_options(), segments, estate=settings.trash_grace_days)
    )
    if days <= 0:
        return None

    def decide() -> hidden_branches.Hiding | None:
        if _erasure_in_flight(settings, table_uuid):
            return None
        return hidden_branches.Hiding(dropped_by=by, dropped_at=now, expires_at=now + timedelta(days=days))

    return decide


async def _announce_removed(control: ControlEmitter, table: DoorTable, segments: list[str], *, token: Caller | None, removed: list[refs.RemovedBranch]) -> None:
    """One ``table_branch_deleted`` per branch removed, AFTER its delete: each names the incarnation it was, which the
    name alone does not, since it also names whatever branch is created under it next."""
    for branch in removed:
        await emit_control(
            control,
            action="table_branch_deleted",
            object_type="table",
            object_id=fga_ids.table_object(table.resolved.require_uuid()),
            object_name=fga_ids.canonical_object_id(segments),
            actor=f"user:{token.subject}" if token is not None else None,
            extra={"branch": branch.name, **({versions.BRANCH_IDENTIFIER_KEY: branch.identifier} if branch.identifier is not None else {})},
        )


class RestoreTableBranchRequest(BaseModel):
    """Which deleted branch to bring back."""

    model_config = ConfigDict(extra="forbid")

    name: str


class RestoreTableBranchResponse(BaseModel):
    """The branches brought back: the one named, and each deleted branch whose directory lies inside its own."""

    restored: list[str]


@management_router.post("/{id}/branches/restore")
async def restore_table_branch(
    *,
    id: str,
    body: RestoreTableBranchRequest,
    table: DoorTableDep,
    so: StorageOptionsDep,
    control: ControlEmitterDep,
    settings: SettingsDep,
    clock: ClockDep,
    token: CurrentToken = None,
) -> RestoreTableBranchResponse:
    """Bring back a branch a recoverable delete hid, with every commit made on it ([[LH-429]]).

    Asks what the delete asks (``can_drop``). Refused 404 (code 22) when no deleted branch of that name is kept, and 409
    once its deadline has passed or while an erasure of the table is in flight. Each branch brought back is announced as created, naming the incarnation it still is.
    """
    segments = parse_identifier(id)
    if await run_in_threadpool(_erasure_in_flight, settings, table.resolved.require_uuid()):
        # Its bytes may hold what the erasure removes, and the erasure deletes every hidden branch for good.
        raise InvalidTableStateError("an erasure of this table is in flight: no deleted branch of it is restored")
    restored = await run_in_threadpool(partial(refs.restore_branch, table, so, body.name, now=clock()))
    for branch in restored:
        await emit_control(
            control,
            action="table_branch_created",
            object_type="table",
            object_id=fga_ids.table_object(table.resolved.require_uuid()),
            object_name=fga_ids.canonical_object_id(segments),
            actor=f"user:{token.subject}" if token is not None else None,
            extra={"branch": branch.name, "restored": True, **({versions.BRANCH_IDENTIFIER_KEY: branch.identifier} if branch.identifier is not None else {})},
        )
    return RestoreTableBranchResponse(restored=[branch.name for branch in restored])


class DeletedTableBranch(BaseModel):
    """One branch a recoverable delete keeps."""

    name: str
    #: The branch it was cut from, ``None`` for main, and the version on it.
    parent_branch: str | None
    parent_version: int
    deleted_at: datetime
    #: When it is deleted for good; a restore is served until then.
    expires_at: datetime


class ListDeletedTableBranchesResponse(BaseModel):
    """The table's deleted branches that can still be restored, by name."""

    branches: list[DeletedTableBranch]


@management_router.post("/{id}/branches/deleted/list")
async def list_deleted_table_branches(*, id: str, table: DoorTableDep, so: StorageOptionsDep, clock: ClockDep) -> ListDeletedTableBranchesResponse:
    """List the table's branches a recoverable delete keeps, each with when it is deleted for good ([[LH-603]]).

    A metadata read (``can_get_metadata``), as the branch listing is. One past its deadline is left out, as a restore of it
    is refused. It names who deleted none of them: that is in the
    audit trail. A table no uuid identifies keeps none.
    """
    if table.resolved.uuid is None:
        return ListDeletedTableBranchesResponse(branches=[])
    kept = await run_in_threadpool(partial(refs.deleted_branches, table, so, now=clock()))
    return ListDeletedTableBranchesResponse(
        branches=[
            DeletedTableBranch(
                name=hidden.name,
                parent_branch=hidden.parent_branch,
                parent_version=hidden.parent_version,
                deleted_at=datetime.fromisoformat(hidden.dropped_at),
                expires_at=datetime.fromisoformat(hidden.expires_at),
            )
            for hidden in kept
        ]
    )
