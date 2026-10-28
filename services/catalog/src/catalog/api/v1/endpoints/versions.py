"""Table version endpoints.

``list`` and ``describe`` delegate to the native backend, ``delete`` is served in-process through
``cleanup_old_versions``, and the three version-TRACKING ops answer 406 — see :func:`_versions_are_unmanaged`.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from functools import partial
from typing import Annotated

from fastapi import APIRouter, Header, Query
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

from catalog.api import fga_deps, lineage_deps
from catalog.api.dependencies import (
    DoorTableDep,
    FgaClientDep,
    LineageEmitterDep,
    ObjectUuidsDep,
    ResolverDep,
    SettingsDep,
    StorageOptionsDep,
)
from catalog.api.pagination import ListLimit, paginate_versions
from catalog.api.security import CurrentToken
from catalog.core import run_context
from catalog.core.base_judge import BaseJudge
from catalog.core.config import Settings
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.services import dataplane, maintenance, managed_versions, table_inputs
from catalog.services.table_claims import DoorTable
from service_kit.governed import fga
from service_kit.governed.audit import AuditAction, AuditOutcome, audit
from service_kit.lakehouse import commit_records, maintenance_due
from service_kit.lakehouse.history_fences import HistoryWork


log = logging.getLogger(__name__)


router = APIRouter(prefix="/v1/table", tags=["version"])

#: THE MANAGEMENT SURFACE ([[LH-021]]). This module serves SPEC operations on `router` and rask's own
#: on this one — split by AUDIENCE rather than by topic, because a spec client discovering a verb like
#: `blobs` or `commit` on a spec prefix meets something the Lance namespace document never defines.
#: Both routers inherit the same three rungs from `api/v1/router.py`.
management_router = APIRouter(prefix="/management/v1/table", tags=["version-management"])


@management_router.get("/{id}/history", response_model_exclude_none=True)
async def table_history(
    id: str,
    table: DoorTableDep,
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
    the lineage store, on the ``rask_author`` run facet
    (``GET /datasets/{name}/producers`` → ``dataset_version`` + ``author`` + ``operation``), which is written
    from the verified caller's principal key on every governed write. A who/when/what view joins the two on the version
    number. Two sources, each authoritative for its own half — a third that merged them would just be a copy
    of one of them, free to drift.

    Reader-tier: ``can_get_metadata`` on the table, the same rung as describe/list-versions. A commit log is
    metadata about the data, and it leaks real information (predicates name values, field names name
    columns), so it is gated exactly like the schema is rather than being treated as public.

    THE RUNG IS THE ROUTER'S, and this route has to be NAMED in ``fga_deps._META_READ_ACTIONS`` to get it:
    ``authorize`` is a router-wide dependency, so an unnamed suffix is refused for every caller and no reader
    ever arrives. Pinned by
    ``tests/unit/test_fga_model_contract.py::test_every_DATA_READ_door_is_gated_as_a_READ``.

    ``limit`` bounds the per-version transaction reads — a table with 10k versions must not turn a UI page
    into 10k object-store round trips.
    """
    rows = await run_in_threadpool(dataplane.table_history, table, so, limit)
    return {"table": id, "versions": rows}


def _batches_are_not_judged(operation: str) -> UnsupportedOperationError:
    """The refusal for a multi-table commit: the catalog judges and finalizes one table version at a time.

    ``CreateTableVersion`` judges a staged manifest before the backend's put-if-not-exists, so a refusal leaves
    that table at its version. A batch finalizes several tables as one, and judging each before one atomic
    finalize is not something the ``dir`` backend offers, so a stock client commits each table on its own.
    """
    return UnsupportedOperationError(f"{operation} is not supported: commit each table through CreateTableVersion, which the catalog judges per table")


@router.post("/version/batch-create", response_model_exclude_none=True)
def batch_create_table_versions(body: BatchCreateTableVersionsRequest) -> BatchCreateTableVersionsResponse:
    """Not supported (406): a batch finalizes several tables at once, and the catalog judges one at a time."""
    raise _batches_are_not_judged("BatchCreateTableVersions")


@router.post("/batch-commit", response_model_exclude_none=True)
def batch_commit_tables(body: BatchCommitTablesRequest) -> BatchCommitTablesResponse:
    """Not supported (406): no sub-operation runs, ``declare_table`` and ``deregister_table`` included. Those two
    have their own doors, which carry the protection, ownership and lineage a batch would skip, and a version is
    committed per table through ``CreateTableVersion``."""
    raise _batches_are_not_judged("BatchCommitTables")


@router.post("/{id}/version/list", response_model_exclude_none=True)
def list_table_versions(
    id: str,
    table: DoorTableDep,
    so: StorageOptionsDep,
    page_token: str | None = None,
    limit: ListLimit = None,
    descending: bool | None = None,
    branch: Annotated[str | None, Query(description="The ref whose version history to list. Omit for main.")] = None,
) -> ListTableVersionsResponse:
    """List the versions of table ``id`` via ``list_table_versions``; ``descending=true`` guarantees
    latest-to-oldest ordering, ``branch`` targets a non-main branch (spec 0.9 query params).

    PAGED HERE, NOT DOWNSTREAM, because the backend cannot page. Driven against a `dir` namespace
    2026-09-13 on a seven-version table: `limit=3` serves three rows and answers `page_token: None`,
    and a token it is handed changes nothing. `None` is what a client stops on, so forwarding `limit`
    told a caller asking for three of seven that it had seen everything — truncation wearing
    pagination's clothes, the shape `GET /management/v1/model` already names.

    So the native call is made UNPAGINATED — the same rule `catalog.api.pagination` states for the name
    listings — and the cursor is this layer's. The full list is what the backend returns unbounded
    (measured: no limit gives all seven), and `ListLimit` still bounds what leaves the door.
    """
    req = ListTableVersionsRequest(
        id=parse_identifier(id),
        # Deliberately not forwarded: either one truncates or skips before this layer can page, and the
        # two cursors would then disagree about what "the next page" means.
        page_token=None,
        limit=None,
        descending=descending,
        branch=branch,
    )
    # The backend reports a branch the table lacks as a missing table; opening the ref answers the spec's 22 ([[LH-406]]).
    table.open_unchecked(so, branch=dataplane.recorded_branch(branch))
    answer = table.read_natively("list_table_versions", req)
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
async def create_table_version(
    id: str,
    body: CreateTableVersionRequest,
    table: DoorTableDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    client: FgaClientDep,
    emitter: LineageEmitterDep,
    uuids: ObjectUuidsDep,
    resolver: ResolverDep,
    authorization: Annotated[str | None, Header()] = None,
) -> CreateTableVersionResponse:
    """Finalize a client-staged manifest as the next version of the table or of one of its branches, after judging it
    ([[LH-471]], [[LH-490]], ADR 0157).

    The catalog answers ``managed_versioning=true``, so a stock writer stages ``_versions/<n>.manifest-<uuid>`` (under
    ``tree/<branch>/`` for a branch, which ``branch`` names) and asks here; ``managed_versions`` judges the version it
    describes against the one it follows on that ref and finalizes the judged bytes through the backend's
    put-if-not-exists. A refusal leaves the ref at its version and answers a typed spec error. A branch is created
    through ``CreateTableBranch``, and a branch this table does not list answers 404 (code 22).

    The staged manifest is judged at the door's location while it still holds the table authorized, and the finalize,
    which names the table, is made once the name is found still resolving there ([[LH-530]]).
    """
    segments = parse_identifier(id)
    body.id = reconcile_body_id(segments, body.id)
    await run_in_threadpool(table.require_held)
    location = table.location
    canonical = fga.canonical_object_id(segments)
    ref = await run_in_threadpool(managed_versions.commit_ref, location, so, dataplane.recorded_branch(body.branch))
    staged = await run_in_threadpool(managed_versions.stage, ref, so, body)
    try:
        await fga_deps.require_staged_operation(
            client, settings, token, segments=segments, location=location, operation=staged.operation, creates=staged.creates
        )
        judge = BaseJudge.from_settings(settings)
        record = await run_in_threadpool(judge.read_record, location)
        run = run_context.from_properties(staged.properties)
        run_context.require_caller_authored(staged.properties, caller=token.subject if token is not None else None)
        adds_data = await run_in_threadpool(
            partial(
                managed_versions.judge,
                base_judge=judge,
                external_bases=record.entries if record is not None else (),
                clone_evidence=await _clone_evidence_for(staged, settings),
            ),
            staged,
            so,
        )
        declared = await fga_deps.judge_staged_run(settings, token, client, resolver, ns=table.namespace, segments=segments, run=run, adds_data=adds_data)
        if staged.creates:
            # A later version may change no field's labels (`managed_versions`); the first sets them, as a create does.
            labels = await run_in_threadpool(managed_versions.staged_field_labels, staged, so)
            await fga_deps.require_classifier_to_set_labels(
                client, settings, token, obj=fga.table_object(table.resolved.require_uuid()), label=canonical, labels=labels
            )
    except BaseException:
        await run_in_threadpool(managed_versions.refuse, staged, so)
        raise
    finally:
        await run_in_threadpool(managed_versions.discard, staged, so)
    caller = token.subject if token is not None else None
    if caller is not None and run_context.AUTHOR_PROPERTY not in staged.properties:
        # BEFORE THE FINALIZE: once the version lands it must name its author, and this commit cannot ([[LH-497]]).
        await run_in_threadpool(
            commit_records.record_author, settings.registry_root, settings.storage_options(), table.resolved.require_uuid(), staged.transaction_uuid, caller
        )
    if declared:
        # BEFORE THE FINALIZE: an erasure names this table from the moment the version lands ([[LH-541]]).
        await run_in_threadpool(
            partial(table_inputs.record, settings.registry_root, settings.storage_options(), table.resolved.require_uuid(), name=canonical, declared=declared)
        )
    response = await run_in_threadpool(managed_versions.finalize, table, staged, body, so)
    audit(
        AuditAction.COMMIT_VERSION,
        AuditOutcome.SUCCESS,
        subject=token.subject if token is not None else None,
        resource=f"table:{canonical}",
        version=staged.version,
        operation=staged.operation,
        branch=staged.branch,
    )
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        location=location,
        so=so,
        token=token,
        operation=managed_versions.lineage_operation(staged),
        authorization=authorization,
        uuids=uuids,
        pin_version=staged.version,
        branch=staged.branch,
        run=run,
        commits_rows=staged.creates and adds_data,
    )
    return response


async def _clone_evidence_for(staged: managed_versions.StagedVersion, settings: Settings) -> managed_versions.CloneEvidence | None:
    """The maintenance sweep's clone list, for a compaction's two commits only (ADR 0162); ``None`` when there is none."""
    if staged.operation not in managed_versions.COMPACTION_OPERATIONS:
        return None
    clones = await run_in_threadpool(maintenance_due.read_clone_list, settings.registry_root, settings.storage_options())
    if clones is None:
        return None
    return managed_versions.CloneEvidence(clones=clones, max_age=timedelta(seconds=settings.clone_list_max_age_seconds))


@router.post("/{id}/version/describe", response_model_exclude_none=True)
def describe_table_version(
    id: str,
    table: DoorTableDep,
    so: StorageOptionsDep,
    version: int | None = None,
    body: DescribeTableVersionRequest | None = None,
) -> DescribeTableVersionResponse:
    """Describe one table version (the latest when ``version`` is omitted).

    Backed by the native dir backend, which returns a spec-correct ``TableVersion`` (with ``manifest_path``
    / ``manifest_size`` / ``e_tag`` / ``timestamp``); ``native.call`` marshals the request to the dict the
    binding expects. A missing version raises the backend's ``TableVersionNotFoundError`` → 404. The answer names the
    table, so it is served only while the name still resolves to the door's location ([[LH-530]]).
    """
    req = body or DescribeTableVersionRequest()
    req.id = reconcile_body_id(parse_identifier(id), req.id)
    if version is not None:
        req.version = version
    table.open_unchecked(so, branch=dataplane.recorded_branch(req.branch))
    described = table.read_natively("describe_table_version", req)
    return described


@router.post("/{id}/version/delete", response_model_exclude_none=True)
async def batch_delete_table_versions(
    id: str,
    body: BatchDeleteTableVersionsRequest,
    table: DoorTableDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
) -> BatchDeleteTableVersionsResponse:
    """Delete old versions of this table — never the current one, and never one a tag or a branch pins.

    ``ranges`` are the spec's ``VersionRange``: start inclusive, end exclusive, ``-1`` for "through the
    latest". A range that reaches the current version is refused (400), the spec's ``{0, -1}`` included:
    Lance commits by put-if-not-exists on the next number, so a deleted latest manifest is minted again
    by the next write and a pinned ``(table, version)`` would name different rows. A version a tag or a
    child branch pins is refused (409), naming the pin. Every range is judged before anything is
    deleted, and ``branch`` scopes the delete to that ref's own history.

    Owner tier (``can_drop``, the rung ``maintenance/run`` clears), and authorization alone governs it: this
    reclaims history, as ``maintenance/run`` does, and deletion protection guards dropping the table, not its
    history (ADR 0150). A version that must survive is tagged.
    """
    segments = parse_identifier(id)
    body.id = reconcile_body_id(segments, body.id)
    return await run_in_threadpool(_delete_versions, table, so, settings, body)


def _delete_versions(table: DoorTable, so: dict[str, str], settings: Settings, body: BatchDeleteTableVersionsRequest) -> BatchDeleteTableVersionsResponse:
    ds = table.open_unchecked(so, branch=body.branch)
    ranges = [(r.start_version, r.end_version) for r in body.ranges]
    # Judged before the sibling scan, which opens every dataset beside this one, so a refused request
    # does no estate I/O. `delete_versions` judges again against the handle it deletes from.
    maintenance.versions_in_ranges(ranges, current=int(ds.version))
    location = str(getattr(ds, "uri", "") or "")
    refs = BaseJudge.from_settings(settings).sibling_base_refs(location, so)
    if refs.unreadable:
        log.warning("version_delete_base_refs_incomplete", extra={"location": location, "unreadable": len(refs.unreadable)})
    # [[LH-308]] Held while Lance deletes: it reads the refs once, so a tag or branch written meanwhile could pin a
    # version this deletes. A ref write holding it answers this request 409.
    with table.history_held(HistoryWork.RECLAIM):
        deleted = maintenance.delete_versions(ds, ranges=ranges, branch=body.branch, protected=refs)
    return BatchDeleteTableVersionsResponse(deleted_count=deleted)
