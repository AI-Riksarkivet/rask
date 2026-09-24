"""Table branch endpoints — backed in-process via the pylance data plane.

The native ``DirectoryNamespace`` 501s branch ops, but ``lance.LanceDataset`` implements Git-like branches
(``ds.branches`` / ``ds.create_branch``), so we back them in-process here exactly like tags — turning the
former spec-correct 501 into a real, working operation.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    CreateTableBranchRequest,
    CreateTableBranchResponse,
    DeleteTableBranchRequest,
    DeleteTableBranchResponse,
    LanceNamespace,
    ListTableBranchesRequest,
    ListTableBranchesResponse,
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


#: Ceiling for the spec list ops' `limit`. The Lance Namespace spec pages these with
#: `page_token`, so a server answering fewer rows than asked and handing back a token is
#: SPEC-CORRECT — the cap costs a caller nothing but a second call. Declared here rather than
#: clamped in the body so the schema states the real bound. An over-limit request is refused by
#: `install_problem_handlers`, which carries the spec `code` (INVALID_INPUT) a generated client
#: dispatches on.
_MAX_LIST_LIMIT = 1000

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/table", tags=["branch"])


@router.post("/{id}/branches/list", response_model_exclude_none=True)
def list_table_branches(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    page_token: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=_MAX_LIST_LIMIT)] = None,
) -> ListTableBranchesResponse:
    """List a table's Git-like branches (paginated) — wraps the pylance ``list_branches`` data-plane op."""
    req = ListTableBranchesRequest(id=parse_identifier(id, settings.delimiter), page_token=page_token, limit=limit)
    return dataplane.list_branches(ns, so, req)


@router.post("/{id}/branches/create", response_model_exclude_none=True)
async def create_table_branch(
    id: str,
    body: CreateTableBranchRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
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
    segments = parse_identifier(id, settings.delimiter)
    body.id = reconcile_body_id(segments, body.id)
    response = dataplane.create_branch(ns, so, body)
    # AFTER the data-plane call, never before — the rule `namespaces.py` states: a change that did not
    # happen is never announced, so a refused create emits nothing.
    await emit_control(
        control,
        action="table_branch_created",
        object_type="table",
        object_id=f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}",
        actor=f"user:{token.sub}" if token is not None else None,
        # `name`, not `branch` — that is the field `CreateTableBranchRequest` declares. "Branched from
        # what" is the question a consumer asks next, and it is answered from the DATASET rather than
        # from the request: a branch taken from main sends no `from_version` and records main's current
        # version, so announcing the request answered `null` for the case that happens most.
        extra={"branch": body.name, **_recorded_parent(ns, so, body)},
    )
    return response


def _recorded_parent(ns: LanceNamespace, so: dict[str, str], body: CreateTableBranchRequest) -> dict[str, Any]:
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
        listed = dataplane.list_branches(ns, so, ListTableBranchesRequest(id=body.id))
        entry = (listed.branches or {}).get(body.name)
    except Exception:  # noqa: BLE001 — an announcement must survive an unreadable parent
        log.warning("branch_parent_unreadable", extra={"branch": body.name})
        return {}
    if entry is None:
        return {}
    # ATTRIBUTES, not `.get` — `ListTableBranchesResponse.branches` holds `BranchContents` models and a
    # dict-shaped read raises. The data-plane builds them from plain dicts, which is what makes the
    # mistake easy and silent right up to the AttributeError.
    return {"parent_branch": entry.parent_branch, "parent_version": entry.parent_version}


@router.post("/{id}/branches/delete", response_model_exclude_none=True)
async def delete_table_branch(
    id: str,
    body: DeleteTableBranchRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    control: ControlEmitterDep,
    force: RaskFlag = False,
    token: CurrentToken = None,
) -> DeleteTableBranchResponse:
    """Delete a branch from the table — wraps the pylance ``delete_branch`` data-plane op.

    The disappearance is the half worth announcing: a console holding a branch list has no other way
    to learn the branch is gone, and a reader that polls discovers it by a failing read.

    PROTECTION-GATED ([[LH-056]]). This is the heavier of the two deletions the table offers — it
    destroys the branch's data and its own version sequence — so a protected table refuses it on the
    same record its drop consults. ``force`` turns that lock only; the FGA gate ran before this
    handler and runs identically either way.
    """
    segments = parse_identifier(id, settings.delimiter)
    body.id = reconcile_body_id(segments, body.id)
    canonical = fga.canonical_object_id(segments, delimiter=settings.delimiter)
    guard = await run_in_threadpool(protection.get_protection, settings.registry_root, settings.storage_options(), "table", canonical)
    fga_deps.require_not_protected(guard or {}, kind="table", obj_id=canonical, force=force)
    response = dataplane.delete_branch(ns, so, body)
    await emit_control(
        control,
        action="table_branch_deleted",
        object_type="table",
        object_id=f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}",
        actor=f"user:{token.sub}" if token is not None else None,
        extra={"branch": body.name},
    )
    return response
