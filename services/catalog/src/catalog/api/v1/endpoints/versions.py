"""Table version endpoints (delegated to the native backend / external manifest store)."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query, Request
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
    ServiceUnavailableError,
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
from service_kit.governed import fga


# The native dir backend implements create / describe / batch-delete versions, but its bindings are typed
# ``request: dict`` (not the pydantic model) — ``native.call`` marshals the request to a dict for those, so
# these delegate directly and return real results instead of the marshalling-bug 501 they surfaced before.
log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/table", tags=["version"])


@router.get("/{id}/history", response_model_exclude_none=True)
async def table_history(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    client: FgaClientDep,
    # BOUNDED AT THE DOOR, and `ge=1` is the load-bearing half. The implementation slices
    # `versions()[:limit]`, so a negative value did not mean "no limit" or "one row" — Python read
    # `?limit=-1` as all-but-the-last and returned nearly the whole history. A bound a minus sign turns
    # inside out is worse than no bound, because the caller gets a plausible-looking answer.
    #
    # `le` makes the ceiling this route's docstring already describes actually true. It is not an
    # amplification guard: `versions()` cannot manufacture reads that do not exist, so a huge limit on a
    # five-version table still does five reads. Deeper history should get the keyset cursor the sibling
    # routes use (`page_token` = last version, `version < page_token`) rather than a raised ceiling.
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
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
    # The native batch is ATOMIC — every declared table exists once that call returns — but the seeds
    # are not part of it, and they cannot be: OpenFGA is a different store with no shared transaction.
    #
    # This loop used to `seed_ownership` and abandon the rest on the FIRST failure (diff2 F3.1). An
    # OpenFGA blip partway through a 12-table batch therefore left tables 5-12 committed on storage
    # with no `owner` grant and no `parent` edge — and because `grant_on_create` writes both in one
    # batch, `owner from parent` cannot rescue them either. They are invisible to every list (per-item
    # filtering) and undroppable by every caller including an estate admin. The endpoint 5xx'd naming
    # none of them.
    #
    # Two changes here, neither of which needs a convergence ruling:
    #
    #  * KEEP GOING. The seeds are independent, so stopping at the first failure strands every table
    #    after it as well. Continuing minimises the stranded set instead of maximising it.
    #  * NAME WHAT IS STRANDED. The error now lists exactly which tables landed without ownership, so
    #    an operator can repair them through the estate-admin editor instead of discovering the state
    #    when a bucket-vs-catalog byte count disagrees months later.
    #
    # WHAT IS STILL OPEN, deliberately: the batch does not CONVERGE. A retry re-runs the native batch,
    # which fails `TableAlreadyExists` and never reaches the seeds again. Fixing that is a design
    # decision — pre-flight the seeds, seed-then-commit (which trades this failure for tuples on
    # tables that may not exist), or an idempotent re-seed keyed on the declared ids — and it is
    # recorded rather than guessed at.
    stranded: list[str] = []
    first_error: Exception | None = None
    for operation in body.operations or []:
        declare = getattr(operation, "declare_table", None)
        segments = getattr(declare, "id", None) if declare is not None else None
        if not segments:
            continue
        try:
            await fga_deps.seed_ownership(client, settings, token, resource="table", segments=segments)
        except Exception as exc:  # noqa: BLE001 — every remaining seed still gets its chance
            stranded.append(fga.canonical_object_id(segments, delimiter=settings.delimiter))
            first_error = first_error or exc
            log.warning("batch_commit_seed_failed", extra={"table": segments, "error": str(exc)})
    if stranded:
        raise ServiceUnavailableError(
            f"batch committed, but {len(stranded)} table(s) landed WITHOUT ownership and are "
            f"unreachable until an estate admin grants it: {', '.join(stranded)}. "
            f"Retrying this batch will not repair them — the tables already exist. Cause: {first_error}"
        )
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
