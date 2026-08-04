"""Table version endpoints (delegated to the native backend / external manifest store)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    BatchCommitTablesRequest,
    BatchCommitTablesResponse,
    BatchCreateTableVersionsRequest,
    BatchCreateTableVersionsResponse,
    BatchDeleteTableVersionsRequest,
    BatchDeleteTableVersionsResponse,
    CreateTableVersionRequest,
    CreateTableVersionResponse,
    DescribeTableVersionRequest,
    DescribeTableVersionResponse,
    ListTableVersionsRequest,
    ListTableVersionsResponse,
)

from catalog.api import fga_deps
from catalog.api.dependencies import (
    FgaClientDep,
    NamespaceDep,
    SettingsDep,
    StorageOptionsDep,
    assert_no_warehouse_bound_namespace,
)
from catalog.api.security import CurrentToken
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.services import dataplane, native


# The native dir backend implements create / describe / batch-delete versions, but its bindings are typed
# ``request: dict`` (not the pydantic model) — ``native.call`` marshals the request to a dict for those, so
# these delegate directly and return real results instead of the marshalling-bug 501 they surfaced before.
router = APIRouter(prefix="/v1/table", tags=["version"])


@router.get("/{id}/history", response_model_exclude_none=True)
async def table_history(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    client: FgaClientDep,
    limit: int = 50,
) -> dict[str, object]:
    """The table's commit log — one row per version, newest first: **what** changed and **when**.

    Answers the question a catalog history view asks, from the format itself rather than from a
    side-table we would have to keep in sync. Lance is immutable and append-only at the manifest level, so
    ``versions()`` gives the timestamps and the transaction log gives the substance: the operation kind, the
    delete predicate exactly as the caller wrote it, which fields an update rewrote, fragment deltas, and
    whether the schema was set at that version.

    **It does not answer WHO, deliberately.** Lance's transaction log has no notion of a user and should not
    have one — identity is this estate's concern, not the format's. The actor per version already lives in
    the lineage store, on the ``author`` run facet
    (``GET /datasets/{name}/producers`` → ``dataset_version`` + ``author`` + ``operation``), which is written
    from the verified OIDC subject on every governed write. A who/when/what view joins the two on the version
    number. Two sources, each authoritative for its own half — a third that merged them would just be a copy
    of one of them, free to drift.

    Reader-tier: ``can_get_metadata`` on the table, the same rung as describe/list-versions. A commit log is
    metadata about the data, and it leaks real information (predicates name values, field names name
    columns), so it is gated exactly like the schema is rather than being treated as public.

    ``limit`` bounds the per-version transaction reads — a table with 10k versions must not turn a UI page
    into 10k object-store round trips.
    """
    segments = parse_identifier(id, settings.delimiter)
    await fga_deps.require_can_get_metadata(client, settings, token, segments=segments)
    rows = await run_in_threadpool(dataplane.table_history, ns, so, segments, limit)
    return {"table": id, "versions": rows}


@router.post("/version/batch-create", response_model_exclude_none=True)
async def batch_create_table_versions(
    request: Request,
    body: BatchCreateTableVersionsRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
) -> BatchCreateTableVersionsResponse:
    """Atomically create version entries for multiple tables — delegates to the native
    ``batch_create_table_versions`` (implemented by the 0.9 dir backend).

    #3-A: this batch route has no ``{id}`` to route by, so it runs against the default root — reject a body
    that names a warehouse-bound namespace rather than writing its version metadata to the wrong bucket."""
    await assert_no_warehouse_bound_namespace(request, settings, [getattr(e, "id", None) for e in (body.entries or [])])
    return await run_in_threadpool(native.call, ns, "batch_create_table_versions", body)


@router.post("/batch-commit", response_model_exclude_none=True)
async def batch_commit_tables(
    request: Request,
    body: BatchCommitTablesRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
) -> BatchCommitTablesResponse:
    """Atomic multi-table commit — delegates to the native ``batch_commit_tables``.

    OWNERSHIP PARITY with ``/declare`` (audit 2026-07-12): a ``declare_table`` sub-op CREATES a table
    (``_authorize_batch`` gated it as create-on-parent), so the creator must be seeded owner + parent
    edge exactly like the dedicated route — without this, a batch-declared table had NO owner tuple
    (fail-closed asymmetry: the creator couldn't manage their own table at owner tier, and the
    reused-id revoke assumptions didn't hold). ``seed_ownership`` is a no-op with FGA off.

    #3-A: no ``{id}`` to route by → runs against the default root, so a ``declare_table`` for a
    warehouse-bound namespace would create the table in the SHARED bucket. Reject that (use the per-table
    routes) before touching storage.
    """
    await assert_no_warehouse_bound_namespace(
        request,
        settings,
        [getattr(getattr(op, "declare_table", None), "id", None) for op in (body.operations or [])],
    )
    response: BatchCommitTablesResponse = await run_in_threadpool(native.call, ns, "batch_commit_tables", body)
    for operation in body.operations or []:
        declare = getattr(operation, "declare_table", None)
        segments = getattr(declare, "id", None) if declare is not None else None
        if segments:
            await fga_deps.seed_ownership(client, settings, token, resource="table", segments=segments)
    return response


@router.post("/{id}/version/list", response_model_exclude_none=True)
def list_table_versions(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    page_token: str | None = None,
    limit: int | None = None,
    descending: bool | None = None,
    branch: str | None = None,
) -> ListTableVersionsResponse:
    """List the versions of table ``id`` via ``list_table_versions``; ``descending=true`` guarantees
    latest-to-oldest ordering, ``branch`` targets a non-main branch (spec 0.9 query params)."""
    req = ListTableVersionsRequest(
        id=parse_identifier(id, settings.delimiter),
        page_token=page_token,
        limit=limit,
        descending=descending,
        branch=branch,
    )
    return native.call(ns, "list_table_versions", req)


@router.post("/{id}/version/create", response_model_exclude_none=True)
def create_table_version(id: str, body: CreateTableVersionRequest, ns: NamespaceDep, settings: SettingsDep) -> CreateTableVersionResponse:
    body.id = reconcile_body_id(parse_identifier(id, settings.delimiter), body.id)
    return native.call(ns, "create_table_version", body)


@router.post("/{id}/version/describe", response_model_exclude_none=True)
def describe_table_version(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    version: int | None = None,
    body: DescribeTableVersionRequest | None = None,
) -> DescribeTableVersionResponse:
    """Describe one table version (the latest when ``version`` is omitted).

    Backed by the native dir backend, which returns a spec-correct ``TableVersion`` (with ``manifest_path``
    / ``manifest_size`` / ``e_tag`` / ``timestamp``); ``native.call`` marshals the request to the dict the
    binding expects. A missing version raises the backend's ``TableVersionNotFoundError`` → 404.
    """
    req = body or DescribeTableVersionRequest()
    req.id = reconcile_body_id(parse_identifier(id, settings.delimiter), req.id)
    if version is not None:
        req.version = version
    return native.call(ns, "describe_table_version", req)


@router.post("/{id}/version/delete", response_model_exclude_none=True)
def batch_delete_table_versions(id: str, body: BatchDeleteTableVersionsRequest, ns: NamespaceDep, settings: SettingsDep) -> BatchDeleteTableVersionsResponse:
    body.id = reconcile_body_id(parse_identifier(id, settings.delimiter), body.id)
    return native.call(ns, "batch_delete_table_versions", body)
