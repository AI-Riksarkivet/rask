"""Table version endpoints (delegated to the native backend / external manifest store)."""

from __future__ import annotations

import logging
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Header, Query, Request
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
    DescribeTableRequest,
    DescribeTableVersionRequest,
    DescribeTableVersionResponse,
    InvalidInputError,
    LanceNamespace,
    ListTableVersionsRequest,
    ListTableVersionsResponse,
    ServiceUnavailableError,
)

from catalog.api import fga_deps, lineage_deps
from catalog.api.dependencies import (
    FgaClientDep,
    LineageEmitterDep,
    NamespaceDep,
    SettingsDep,
    StorageOptionsDep,
    assert_no_warehouse_bound_namespace,
)
from catalog.api.pagination import paginate_versions
from catalog.api.security import CurrentToken
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.core.lineage_emit import CREATE_TABLE_VERSION
from catalog.services import dataplane, native
from service_kit.governed import fga


# The native dir backend implements create / describe / batch-delete versions, but its bindings are typed
# ``request: dict`` (not the pydantic model) — ``native.call`` marshals the request to a dict for those, so
# these delegate directly and return real results instead of the marshalling-bug 501 they surfaced before.
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
    dependency, so an unmapped suffix demands ``can_write_data`` before this line runs and no reader ever
    arrives to be metadata-checked. Pinned by
    ``tests/unit/test_fga_model_contract.py::test_every_DATA_READ_door_is_gated_as_a_READ_not_by_the_writer_fallthrough``.

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
    # Every entry carries its own `manifest_path`, on the same terms as the single-table door. This
    # operation answers 406 on the dir backend today, which is not a reason to let the field through
    # unchecked: the guard belongs with the route, not with whichever backend happens to be mounted.
    for entry in body.entries or []:
        await _refuse_a_manifest_this_table_does_not_own(ns, list(getattr(entry, "id", None) or []), getattr(entry, "manifest_path", None))
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


def _refuse_a_manifest_whose_SHAPE_can_leave_any_table(manifest_path: str) -> None:
    """Shape refusals that need no knowledge of where the table lives, so they cost no round trip."""
    unsafe = (
        not manifest_path
        or "\\" in manifest_path
        or ".." in manifest_path.split("/")
        or any(character.isspace() or ord(character) < 0x20 for character in manifest_path)
    )
    if unsafe:
        raise InvalidInputError(
            f"manifest_path {manifest_path!r} is not a path inside a single table — it contains a traversal, "
            f"a backslash or a control character, and creating a version MOVES the file it names"
        )


def _in_the_store(path: str) -> tuple[str, str]:
    """Split a location or a ``manifest_path`` into ``(authority, key)`` as the OBJECT STORE sees it.

    The authority is ``<scheme>://<netloc>`` when the value is fully qualified and empty when it is
    store-relative; the key is the rest, with the leading and trailing separators stripped so a bucket
    key and a filesystem path compare on the same terms.
    """
    parts = urlsplit(path)
    if parts.scheme:
        return f"{parts.scheme}://{parts.netloc}", parts.path.strip("/")
    return "", path.strip("/")


def _refuse_a_manifest_outside(manifest_path: str, *, table_location: str | None) -> None:
    """Refuse a ``manifest_path`` that does not name a place inside ``table_location``.

    FAIL-CLOSED ON AN UNKNOWN LOCATION: with nothing to compare against there is no way to tell this
    table's manifest from a sibling's, and the failure mode is a destructive move rather than a refused
    read.

    CONTAINMENT IS TESTED AGAINST ``<key>/``, never as a bare string prefix, and a qualified candidate's
    authority must match exactly — otherwise ``medallion/bronze-evil`` passes for ``medallion/bronze``
    and the bucket ``acme-bucket-evil`` passes for ``acme-bucket``. Both near-misses are reachable by
    anyone who can choose a table name, which is every writer. Same reasoning and same shape as
    :func:`catalog.core.vending._location_within`; not reused directly because that one splits an
    ``s3://`` location and this field is also a bare filesystem path on a ``dir`` namespace.

    AN UNQUALIFIED CANDIDATE IS COMPARED KEY-ONLY, because the backend resolves it inside the SAME store
    the table is in — so "no scheme" means "this store", not "this table".

    AN EMPTY OUTER KEY FAILS CLOSED, and this is the one place the shape deliberately differs from
    ``vending._location_within``: there an empty prefix IS the whole bucket, because the policy it
    renders says so. Here it would mean every object in the store is inside the table and the guard
    stops guarding, so a table location that reduces to a bare store root is refused instead. No table
    in this estate has one — locations carry a project prefix — so the branch costs nothing and removes
    a way for the check to silently become a no-op.
    """
    if not table_location:
        raise InvalidInputError(
            f"manifest_path {manifest_path!r} cannot be checked because this table's location is unknown, so there is nothing to confine it to"
        )
    outer_authority, outer_key = _in_the_store(table_location)
    inner_authority, inner_key = _in_the_store(manifest_path)
    authority_agrees = not inner_authority or inner_authority == outer_authority
    within = authority_agrees and bool(outer_key) and (inner_key == outer_key or inner_key.startswith(f"{outer_key}/"))
    if not within:
        raise InvalidInputError(
            f"manifest_path {manifest_path!r} does not name a place inside this table's own location "
            f"({table_location!r}); creating a version MOVES that file, so naming one this table does not own "
            f"both destroys it and grafts its rows here. The path is resolved inside the table's object store, "
            f"so it must carry the table's own prefix"
        )


async def _refuse_a_manifest_this_table_does_not_own(ns: LanceNamespace, segments: list[str], manifest_path: str | None) -> None:
    """Refuse a ``manifest_path`` that can name a file outside the table it is being created for.

    ``create_table_version`` MOVES the file at this path into the table's version slot — it is not a
    copy. Driven against a real ``dir`` namespace 2026-09-11 with no privileged access: a caller holding
    ``can_write_data`` on one table named a manifest inside another table's ``_versions/`` directory and
    both destroyed that version and grafted its rows into their own table (victim [1, 2, 3] -> [1, 3];
    attacker [1] -> [1, 2]). The FGA gate above is sound and does not reach this: it authorises the table
    in ``id``, while the reach came from a field nothing inspected.

    THE BACKEND RESOLVES THIS FIELD INSIDE THE TABLE'S OBJECT STORE, NOT INSIDE THE TABLE, and that is
    what decides the guard. Measured 2026-09-16 by staging one real manifest and driving every spelling
    at it — on a local ``dir`` namespace, and again against the estate's own S3 store::

        dir  '_versions/<n>.manifest-<uuid>'                   -> InvalidInput "Staging manifest not found"
        dir  't.lance/_versions/<n>.manifest-<uuid>'           -> InvalidInput "Staging manifest not found"
        dir  '/<root>/t.lance/_versions/<n>.manifest-<uuid>'   -> OK, version 2 committed
        s3   '_versions/<n>.manifest-<uuid>'                   -> InvalidInput "Staging manifest not found"
        s3   '<prefix>/t.lance/_versions/<n>.manifest-<uuid>'  -> OK, version 2 committed

    So the accepted spelling is the STORE-relative one — the whole filesystem path for ``dir``, the
    bucket key for S3 — and the spec's own table-relative example
    (``namespace.md``, "Table Version Metadata Schema": ``"_versions/9223372036854775806.manifest"``)
    commits on neither backend.

    THERE IS THEREFORE NO SAFE "RELATIVE MEANS CONFINED" SHORTCUT, and reading the S3 row as one is the
    live hole this closes: a bare ``other-project/other.lance/_versions/x`` carries no scheme and no
    leading slash, so a guard that only judges absolute paths waves it through — and the backend
    resolves it against the BUCKET and moves another project's manifest into this table. Every spelling
    is compared, and a bare ``_versions/x`` is refused with the rest: as a store key it is outside the
    table, it commits on neither backend, and if such an object existed it would be moved in.

    THE VERSION CAS IS NOT THIS GUARD, which is why the door looked safe. The backend refuses any version
    but ``latest + 1``, so an arbitrary slot cannot be written — but aimed at the version the CAS demands,
    the cross-table move succeeds.
    """
    if manifest_path is None:
        return
    _refuse_a_manifest_whose_SHAPE_can_leave_any_table(manifest_path)
    described = await run_in_threadpool(native.call, ns, "describe_table", DescribeTableRequest(id=segments))
    # NARROWED, not trusted: `location` is optional in the spec's response model, and a backend that
    # answers without one leaves nothing to compare against. Anything that is not a string is the same
    # situation as absent, and both fail closed below rather than reaching `urlsplit`.
    location = getattr(described, "location", None)
    _refuse_a_manifest_outside(manifest_path, table_location=location if isinstance(location, str) else None)


@router.post("/{id}/version/create", response_model_exclude_none=True)
async def create_table_version(
    id: str,
    body: CreateTableVersionRequest,
    ns: NamespaceDep,
    so: StorageOptionsDep,
    settings: SettingsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    authorization: Annotated[str | None, Header()] = None,
) -> CreateTableVersionResponse:
    """Create one version entry for this table, from a manifest the table itself owns.

    THE ONLY DOOR IN THIS MODULE THAT WRITES, and until [[LH-018]] it recorded nothing. It MOVES a
    manifest into the table's version slot, so a version exists afterwards that did not before — and a
    version minted through the SPEC's own commit door left no provenance, while the same table's
    `/commit` door emitted. Two doors onto one table, one of them silent.

    Emitted AFTER the native call, pinned to the version just minted, exactly as the column doors and
    `restore_table` do. The other routes here mint nothing (reads, a delete governed by the deletion
    control, and two ops the dir backend answers 406) and stay quiet on purpose — an emit from those
    would be provenance for work that never happened.

    NO `Idempotency-Key` SEAM HERE, DELIBERATELY: the version CAS already converges a replay, and
    wiring `catalog.api.idempotency` on top would add a second answer to a question the spec has
    settled. Measured 2026-09-16 against a real `dir` namespace — a staged manifest committed at version
    2, then the identical request replayed::

        attempt 1 -> OK, version 2
        attempt 2 -> ConcurrentModificationError (code 14)

    which is precisely the error set `lance_docs/namespace.md:1772` declares for this operation
    (1 NamespaceNotFound, 4 TableNotFound, 14 ConcurrentModification). That is the seam's own bar and it
    is met without it: the replay is non-destructive (the slot is occupied, so nothing moves), it maps to
    409 rather than a bare 500, and the caller can tell its own commit from a competing writer's by
    reading `DescribeTableVersion` and comparing the `e_tag` of the manifest it staged. Contrast
    `create_table`, which the seam DOES wrap: there a replay's `AlreadyExists` is indistinguishable from
    a name collision and the caller cannot learn whether its own write landed.
    """
    segments = parse_identifier(id, settings.delimiter)
    body.id = reconcile_body_id(segments, body.id)
    await _refuse_a_manifest_this_table_does_not_own(ns, segments, body.manifest_path)
    response = await run_in_threadpool(native.call, ns, "create_table_version", body)
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        ns=ns,
        so=so,
        settings=settings,
        token=token,
        operation=CREATE_TABLE_VERSION,
        authorization=authorization,
        pin_version=body.version,
    )
    return response


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
