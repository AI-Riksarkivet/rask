"""Index endpoints (delegated to the native backend).

A build/drop bumps the Lance version (new manifest) without changing data or columns, so each emits a
best-effort versioned lineage ``WROTE`` event (operation ``create_index`` / ``drop_index``) — provenance of
when a scalar/vector index was (re)built or removed. The native responses carry only a ``transaction_id``,
so the shared ``lineage_deps.emit_measured_write`` trailer reads the produced version back off the dataset
(one open, best-effort — a readback failure never fails the already-committed index op).
"""

from __future__ import annotations

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
)

from catalog.api import lineage_deps
from catalog.api.dependencies import LineageEmitterDep, NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.api.security import CurrentToken
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.core.lineage_emit import CREATE_INDEX, DROP_INDEX
from catalog.services import dataplane, native


#: Ceiling for the spec list ops' `limit`. The Lance Namespace spec pages these with
#: `page_token`, so a server answering fewer rows than asked and handing back a token is
#: SPEC-CORRECT — the cap costs a caller nothing but a second call. Declared here rather than
#: clamped in the body so the schema states the real bound. An over-limit request is refused by
#: `install_problem_handlers`, which carries the spec `code` (INVALID_INPUT) a generated client
#: dispatches on.
_MAX_LIST_LIMIT = 1000

router = APIRouter(prefix="/v1/table", tags=["index"])


@router.post("/{id}/create_index", response_model_exclude_none=True)
async def create_index(
    id: str,
    body: CreateTableIndexRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    authorization: Annotated[str | None, Header()] = None,
) -> CreateTableIndexResponse:
    """Build a vector index on a table's column — wraps the native ``create_table_index`` op; emits a
    CREATE_INDEX lineage event at the new version."""
    segments = parse_identifier(id, settings.delimiter)
    body.id = reconcile_body_id(segments, body.id)
    dataplane.refuse_a_branch_this_door_cannot_honour(body.branch, door="create_table_index")
    response: CreateTableIndexResponse = await run_in_threadpool(native.call, ns, "create_table_index", body)
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        ns=ns,
        so=so,
        settings=settings,
        token=token,
        operation=CREATE_INDEX,
        authorization=authorization,
    )
    return response


@router.post("/{id}/create_scalar_index", response_model_exclude_none=True)
async def create_scalar_index(
    id: str,
    body: CreateTableIndexRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    authorization: Annotated[str | None, Header()] = None,
) -> CreateTableScalarIndexResponse:
    """Build a scalar index on a table's column — wraps the native ``create_table_scalar_index`` op; emits a
    CREATE_INDEX lineage event at the new version."""
    segments = parse_identifier(id, settings.delimiter)
    body.id = reconcile_body_id(segments, body.id)
    dataplane.refuse_a_branch_this_door_cannot_honour(body.branch, door="create_table_scalar_index")
    response: CreateTableScalarIndexResponse = await run_in_threadpool(native.call, ns, "create_table_scalar_index", body)
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        ns=ns,
        so=so,
        settings=settings,
        token=token,
        operation=CREATE_INDEX,
        authorization=authorization,
    )
    return response


@router.post("/{id}/index/list", response_model_exclude_none=True)
def list_table_indices(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    page_token: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=_MAX_LIST_LIMIT)] = None,
) -> ListTableIndicesResponse:
    """List the indices defined on a table (paged) — wraps the native ``list_table_indices`` op."""
    req = ListTableIndicesRequest(id=parse_identifier(id, settings.delimiter), page_token=page_token, limit=limit)
    return native.call(ns, "list_table_indices", req)


@router.post("/{id}/index/{index_name}/stats", response_model_exclude_none=True)
def describe_table_index_stats(id: str, index_name: str, ns: NamespaceDep, settings: SettingsDep) -> DescribeTableIndexStatsResponse:
    """Report stats for a named index on a table — wraps the native ``describe_table_index_stats`` op."""
    req = DescribeTableIndexStatsRequest(id=parse_identifier(id, settings.delimiter), index_name=index_name)
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
    authorization: Annotated[str | None, Header()] = None,
) -> DropTableIndexResponse:
    """Drop a named index from a table — wraps the native ``drop_table_index`` op; emits a DROP_INDEX
    lineage event at the new version."""
    segments = parse_identifier(id, settings.delimiter)
    req = DropTableIndexRequest(id=segments, index_name=index_name)
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
    )
    return response
