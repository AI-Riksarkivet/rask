"""Table version endpoints.

``list`` and ``describe`` delegate to the native backend, ``delete`` is served in-process through
``cleanup_old_versions``, and the three version-TRACKING ops answer 406 — see :func:`_versions_are_unmanaged`.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Query
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
    InvalidInputError,
    ListTableVersionsRequest,
    ListTableVersionsResponse,
    UnsupportedOperationError,
)

from catalog.api import fga_deps
from catalog.api.dependencies import FgaClientDep, NamespaceDep, SettingsDep, StorageOptionsDep
from catalog.api.pagination import paginate_versions
from catalog.api.rask_params import RaskFlag
from catalog.api.security import CurrentToken
from catalog.core.base_judge import BaseJudge
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.core.namespace import open_dataset_unchecked
from catalog.services import dataplane, maintenance, native
from service_kit.governed import fga
from service_kit.lakehouse import protection


log = logging.getLogger(__name__)

#: Ceiling for the spec list ops' `limit`. The Lance Namespace spec pages these with
#: `page_token`, so a server answering fewer rows than asked and handing back a token is
#: SPEC-CORRECT — the cap costs a caller nothing but a second call. Declared here rather than
#: clamped in the body so the schema states the real bound. An over-limit request is refused by
#: `install_problem_handlers`, which carries the spec `code` (INVALID_INPUT) a generated client
#: dispatches on.
_MAX_LIST_LIMIT = 1000

router = APIRouter(prefix="/v1/table", tags=["version"])

#: THE MANAGEMENT SURFACE ([[LH-021]]). This module serves SPEC operations on `router` and rask's own
#: on this one — split by AUDIENCE rather than by topic, because a spec client discovering a verb like
#: `blobs` or `commit` on a spec prefix meets something the Lance namespace document never defines.
#: Both routers inherit the same authn/authz and delimiter guard from `api/v1/router.py`.
management_router = APIRouter(prefix="/management/v1/table", tags=["version-management"])


@management_router.get("/{id}/history", response_model_exclude_none=True)
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

    THE RUNG IS THE ROUTER'S, and this route has to be NAMED in ``fga_deps._META_READ_ACTIONS`` to get it.
    The check below is the second one a request meets, not the first: ``authorize`` is a router-wide
    dependency, so an unnamed suffix is refused for every caller before this line runs and no reader ever
    arrives to be metadata-checked. Pinned by
    ``tests/unit/test_fga_model_contract.py::test_every_DATA_READ_door_is_gated_as_a_READ``.

    ``limit`` bounds the per-version transaction reads — a table with 10k versions must not turn a UI page
    into 10k object-store round trips.
    """
    segments = parse_identifier(id, settings.delimiter)
    await fga_deps.require_can_get_metadata(client, settings, token, segments=segments)
    rows = await run_in_threadpool(dataplane.table_history, ns, so, segments, limit)
    return {"table": id, "versions": rows}


def _versions_are_unmanaged(operation: str) -> UnsupportedOperationError:
    """The refusal for a version-TRACKING op while this catalog does not manage table versions.

    The spec pairs these ops with ``managed_versioning``: a caller routes commits through them only when
    ``describe_table`` reports it, and the catalog's namespace never does (pinned by
    ``test_the_catalog_namespace_does_not_advertise_managed_versioning``). Advertising it would put
    clients on a catalog-mediated commit pointer, the Iceberg shape the LANCE-ONLY ruling in CLAUDE.md
    exists to avoid. A table commits through Lance's own manifest commit or the catalog's ``/commit``
    door, which judge what they publish; these would move a client-staged manifest into the version
    slot unjudged.
    """
    return UnsupportedOperationError(
        f"{operation} is not supported: this catalog does not manage table versions (describe_table never reports managed_versioning). "
        "Commit through Lance's own manifest commit, or POST /management/v1/table/{id}/commit"
    )


@router.post("/version/batch-create", response_model_exclude_none=True)
def batch_create_table_versions(body: BatchCreateTableVersionsRequest) -> BatchCreateTableVersionsResponse:
    """Not supported (406) while this catalog does not manage table versions — ``managed_versioning`` is
    never reported, so no entry is created, whatever the mounted backend implements."""
    raise _versions_are_unmanaged("BatchCreateTableVersions")


@router.post("/batch-commit", response_model_exclude_none=True)
def batch_commit_tables(body: BatchCommitTablesRequest) -> BatchCommitTablesResponse:
    """Not supported (406) while this catalog does not manage table versions — ``managed_versioning`` is
    never reported, so no sub-operation runs, ``declare_table`` and ``deregister_table`` included. Those
    two have their own doors, which carry the protection, ownership and lineage a batch would skip."""
    raise _versions_are_unmanaged("BatchCommitTables")


@router.post("/{id}/version/list", response_model_exclude_none=True)
def list_table_versions(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    page_token: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=_MAX_LIST_LIMIT)] = None,
    descending: bool | None = None,
    branch: Annotated[str | None, Query(description="The ref whose version history to list. Omit for main.")] = None,
) -> ListTableVersionsResponse:
    """List the versions of table ``id`` via ``list_table_versions``; ``descending=true`` guarantees
    latest-to-oldest ordering, ``branch`` targets a non-main branch (spec 0.9 query params).

    PAGED HERE, NOT DOWNSTREAM, because the backend cannot page. Driven against a `dir` namespace
    2026-09-13 on a seven-version table: `limit=3` serves three rows and answers `page_token: None`,
    and a token it is handed changes nothing. `None` is what a client stops on, so forwarding `limit`
    told a caller asking for three of seven that it had seen everything — truncation wearing
    pagination's clothes, the shape `GET /v1/model` already names.

    So the native call is made UNPAGINATED — the same rule `catalog.api.pagination` states for the name
    listings — and the cursor is this layer's. The full list is what the backend returns unbounded
    (measured: no limit gives all seven), and `_MAX_LIST_LIMIT` still bounds what leaves the door.
    """
    req = ListTableVersionsRequest(
        id=parse_identifier(id, settings.delimiter),
        # Deliberately not forwarded: either one truncates or skips before this layer can page, and the
        # two cursors would then disagree about what "the next page" means.
        page_token=None,
        limit=None,
        descending=descending,
        branch=branch,
    )
    answer = native.call(ns, "list_table_versions", req)
    rows = list(answer.versions or [])
    by_version = {row.version: row for row in rows}
    try:
        page, next_token = paginate_versions(list(by_version), page_token, limit, descending=bool(descending))
    except ValueError as exc:
        raise InvalidInputError(f"page_token must be a version number, got {page_token!r}") from exc
    answer.versions = [by_version[version] for version in page]
    answer.page_token = next_token
    return answer


@router.post("/{id}/version/create", response_model_exclude_none=True)
def create_table_version(id: str, body: CreateTableVersionRequest) -> CreateTableVersionResponse:
    """Not supported (406) while this catalog does not manage table versions.

    ``CreateTableVersion`` moves a client-staged manifest into the table's next version slot. The spec
    pairs it with ``managed_versioning``, which ``describe_table`` never reports here, so no client is
    sent to it; served, it would publish a manifest whose data files nothing judges against the table's
    file version.
    """
    raise _versions_are_unmanaged("CreateTableVersion")


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
def batch_delete_table_versions(
    id: str, body: BatchDeleteTableVersionsRequest, ns: NamespaceDep, settings: SettingsDep, so: StorageOptionsDep, force: RaskFlag = False
) -> BatchDeleteTableVersionsResponse:
    """Delete old versions of this table — never the current one, and never one a tag or a branch pins.

    ``ranges`` are the spec's ``VersionRange``: start inclusive, end exclusive, ``-1`` for "through the
    latest". A range that reaches the current version is refused (400), the spec's ``{0, -1}`` included:
    Lance commits by put-if-not-exists on the next number, so a deleted latest manifest is minted again
    by the next write and a pinned ``(table, version)`` would name different rows. A version a tag or a
    child branch pins is refused (409), naming the pin. Every range is judged before anything is
    deleted, and ``branch`` scopes the delete to that ref's own history.

    Owner tier (``can_drop``, the rung ``maintenance/run`` clears), protection-gated like drop; ``force``
    turns the protection lock only. FastAPI runs this sync handler off the event loop.
    """
    segments = parse_identifier(id, settings.delimiter)
    body.id = reconcile_body_id(segments, body.id)
    canonical = fga.canonical_object_id(segments, delimiter=settings.delimiter)
    guard = protection.get_protection(settings.registry_root, settings.storage_options(), "table", canonical)
    fga_deps.require_not_protected(guard or {}, kind="table", obj_id=canonical, force=force)
    ds = open_dataset_unchecked(ns, so, segments, branch=body.branch)
    ranges = [(r.start_version, r.end_version) for r in body.ranges]
    # Judged before the sibling scan, which opens every dataset beside this one, so a refused request
    # does no estate I/O. `delete_versions` judges again against the handle it deletes from.
    maintenance.versions_in_ranges(ranges, current=int(ds.version))
    location = str(getattr(ds, "uri", "") or "")
    refs = BaseJudge.from_settings(settings).sibling_base_refs(location, so)
    if refs.unreadable:
        log.warning("version_delete_base_refs_incomplete", extra={"location": location, "unreadable": len(refs.unreadable)})
    deleted = maintenance.delete_versions(ds, ranges=ranges, branch=body.branch, protected=refs)
    return BatchDeleteTableVersionsResponse(deleted_count=deleted)
