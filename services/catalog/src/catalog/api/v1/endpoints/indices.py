"""Index endpoints.

The lakehouse builds no index (ADR 0162). ``create_index`` and ``create_scalar_index`` answer the spec's
``Unsupported`` (406) naming the bring-your-own path: a build reads a whole column, so it runs on the caller's
compute, and pylance commits it through ``version/create``, which judges it like any other commit. A drop bumps the
Lance version without changing data or columns, so it emits a best-effort versioned lineage ``WROTE`` event
(``drop_index``) naming the version its transaction committed (``dataplane.committed_version``).
"""

from __future__ import annotations

import logging
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Header, Query
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    CreateTableIndexRequest,
    CreateTableIndexResponse,
    CreateTableScalarIndexResponse,
    DescribeTableIndexStatsRequest,
    DescribeTableIndexStatsResponse,
    DropTableIndexRequest,
    DropTableIndexResponse,
    ListTableIndicesRequest,
    ListTableIndicesResponse,
    UnsupportedOperationError,
)

from catalog.api import lineage_deps
from catalog.api.dependencies import LineageEmitterDep, NamespaceDep, ObjectUuidsDep, SettingsDep, StorageOptionsDep
from catalog.api.security import CurrentToken
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.core.lineage_emit import DROP_INDEX
from catalog.core.namespace import open_dataset_for_commit
from catalog.services import dataplane, native


#: Ceiling for the spec list ops' `limit`. The Lance Namespace spec pages these with
#: `page_token`, so a server answering fewer rows than asked and handing back a token is
#: SPEC-CORRECT — the cap costs a caller nothing but a second call. Declared here rather than
#: clamped in the body so the schema states the real bound. An over-limit request is refused by
#: `install_problem_handlers`, which carries the spec `code` (INVALID_INPUT) a generated client
#: dispatches on.
_MAX_LIST_LIMIT = 1000

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/table", tags=["index"])


#: What both create doors answer: where an index is built instead.
_BUILD_IT_YOURSELF = (
    "the catalog builds no index. Build it on your own compute: open the table through this catalog and call pylance's "
    "create_index or create_scalar_index; the commit goes through version/create, which judges it"
)


@router.post("/{id}/create_index", response_model_exclude_none=True)
def create_index(id: str, body: CreateTableIndexRequest) -> CreateTableIndexResponse:
    """Refused 406: a vector index is built on the caller's compute and committed through ``version/create``."""
    raise UnsupportedOperationError(_BUILD_IT_YOURSELF)


@router.post("/{id}/create_scalar_index", response_model_exclude_none=True)
def create_scalar_index(id: str, body: CreateTableIndexRequest) -> CreateTableScalarIndexResponse:
    """Refused 406: a scalar index is built on the caller's compute and committed through ``version/create``."""
    raise UnsupportedOperationError(_BUILD_IT_YOURSELF)


@router.post("/{id}/index/list", response_model_exclude_none=True)
def list_table_indices(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    body: ListTableIndicesRequest | None = None,
    page_token: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=_MAX_LIST_LIMIT)] = None,
    branch: Annotated[
        str | None,
        Query(
            description="REFUSED: the upstream implementation answers from main regardless, so honouring this would return main's data labelled as this ref."
        ),
    ] = None,
) -> ListTableIndicesResponse:
    """List the indices defined on a table (paged) — wraps the native ``list_table_indices`` op.

    ``branch`` is DECLARED here only so it can be refused. The route did not accept it at all, which
    read as safe and was not: a caller who asked for a branch's indices got 200 and MAIN's list.
    Verified live 2026-08-31 against a table whose branch carried a BTREE on `id` that main did not —
    this door reported an empty list for the branch, for main, and for a branch never created. Being
    told "no indices" about the wrong dataset is a worse answer than an error, because it is actionable.
    """
    sent = body.model_fields_set if body else frozenset()
    segments = reconcile_body_id(parse_identifier(id, settings.delimiter), body.id if body else None)
    if body is not None:
        page_token = body.page_token if "page_token" in sent else page_token
        limit = body.limit if "limit" in sent else limit
        branch = body.branch if "branch" in sent else branch
    req = ListTableIndicesRequest(id=segments, page_token=page_token, limit=limit, branch=branch, version=body.version if body else None)
    dataplane.refuse_a_branch_this_door_cannot_honour(branch, door="list_table_indices")
    return native.call(ns, "list_table_indices", req)


@router.post("/{id}/index/{index_name}/stats", response_model_exclude_none=True)
def describe_table_index_stats(
    id: str,
    index_name: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    body: DescribeTableIndexStatsRequest | None = None,
    branch: Annotated[
        str | None,
        Query(
            description="REFUSED: the upstream implementation answers from main regardless, so honouring this would return main's data labelled as this ref."
        ),
    ] = None,
) -> DescribeTableIndexStatsResponse:
    """Report stats for a named index on a table — wraps the native ``describe_table_index_stats`` op.

    ``branch`` is declared to be refused, for the same reason as the listing beside it: an index of the
    same name can exist on both refs with different coverage, so answering from main is a plausible
    wrong number rather than a visible failure."""
    segments = reconcile_body_id(parse_identifier(id, settings.delimiter), body.id if body else None)
    if body is not None and "branch" in body.model_fields_set:
        branch = body.branch
    req = DescribeTableIndexStatsRequest(id=segments, index_name=index_name, branch=branch, version=body.version if body else None)
    dataplane.refuse_a_branch_this_door_cannot_honour(branch, door="describe_table_index_stats")
    return native.call(ns, "describe_table_index_stats", req)


@router.post("/{id}/index/{index_name}/drop", response_model_exclude_none=True)
async def drop_table_index(
    id: str,
    index_name: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    uuids: ObjectUuidsDep,
    authorization: Annotated[str | None, Header()] = None,
    body: DropTableIndexRequest | None = None,
    branch: Annotated[
        str | None,
        Query(
            description="REFUSED: the upstream implementation answers from main regardless, so honouring this would return main's data labelled as this ref."
        ),
    ] = None,
) -> DropTableIndexResponse:
    """Drop a named index from a table — wraps the native ``drop_table_index`` op; emits a DROP_INDEX
    lineage event at the new version.

    ``branch`` is DECLARED only so it can be REFUSED, the same rule the listing and the stats door
    beside it already carry — and this door is the one where getting it wrong destroys something.
    `DropTableIndexRequest` has a `branch` field in the spec ("Branch to target. When not specified,
    the main branch is used"), and the route accepted no body at all, so a spec-conformant client
    asking to drop `work`'s index was answered 200 having dropped MAIN's. Its siblings were fixed for
    exactly this shape on 2026-08-31 — they returned main's LIST for a branch's request — and this
    one was missed, where the consequence is a write rather than a read.

    Refused BEFORE `native.call`, because the whole point is that the native op would otherwise
    succeed against the wrong object.
    """
    segments = parse_identifier(id, settings.delimiter)
    if body is not None and "branch" in body.model_fields_set:
        branch = body.branch
    dataplane.refuse_a_branch_this_door_cannot_honour(branch, door="drop_table_index")
    req = DropTableIndexRequest(id=segments, index_name=index_name)
    await run_in_threadpool(partial(open_dataset_for_commit, ns, so, segments, checked=False))
    response: DropTableIndexResponse = await run_in_threadpool(native.call, ns, "drop_table_index", req)
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        ns=ns,
        so=so,
        settings=settings,
        token=token,
        operation=DROP_INDEX,
        authorization=authorization,
        uuids=uuids,
        pin_version=await run_in_threadpool(partial(dataplane.committed_version, ns, so, segments, response.transaction_id, branch=None)),
        # Main only: this door refuses a branch before the build.
        branch=None,
    )
    return response
