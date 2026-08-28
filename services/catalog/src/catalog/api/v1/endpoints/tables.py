"""Table metadata + lifecycle endpoints (no data plane)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    DeclareTableRequest,
    DeclareTableResponse,
    DeregisterTableRequest,
    DeregisterTableResponse,
    DescribeTableRequest,
    DescribeTableResponse,
    DropTableRequest,
    DropTableResponse,
    GetTableStatsRequest,
    GetTableStatsResponse,
    GetTableTagVersionRequest,
    InvalidInputError,
    LanceNamespace,
    ListNamespacesRequest,
    ListTablesRequest,
    ListTablesResponse,
    RegisterTableRequest,
    RegisterTableResponse,
    RenameTableRequest,
    RenameTableResponse,
    RestoreTableRequest,
    RestoreTableResponse,
    TableAlreadyExistsError,
    TableExistsRequest,
    TableNotFoundError,
)

from catalog.api import fga_deps, lineage_deps
from catalog.api.dependencies import (
    ControlEmitterDep,
    FgaClientDep,
    LineageEmitterDep,
    NamespaceDep,
    SettingsDep,
    StorageOptionsDep,
    VendorDep,
    namespace_for_top_ns,
)
from catalog.api.pagination import _paginate
from catalog.api.security import CurrentToken
from catalog.api.v1.endpoints.credentials import _has_external_bases
from catalog.core.formats import reject_unsupported_format
from catalog.core.identifiers import MAX_NAMESPACE_DEPTH, parse_identifier, reconcile_body_id
from catalog.core.lineage_emit import (
    DECLARE_TABLE,
    DEREGISTER_TABLE,
    DROP_TABLE,
    REGISTER_TABLE,
    RESTORE_TABLE,
    InputPin,
    emit_write_event,
)
from catalog.schemas import ProtectionResponse, SetProtectionRequest, TrashEntry
from catalog.services import dataplane, native, warehouses
from service_kit.control_emit import emit_control
from service_kit.governed import fga
from service_kit.lakehouse import maintenance_policies, protection, trash


log = logging.getLogger(__name__)
#: Ceiling for the spec list ops' `limit`. The Lance Namespace spec pages these with
#: `page_token`, so a server answering fewer rows than asked and handing back a token is
#: SPEC-CORRECT — the cap costs a caller nothing but a second call. Declared here rather than
#: clamped in the body so the schema states the real bound. An over-limit request is refused by
#: `install_problem_handlers`, which carries the spec `code` (INVALID_INPUT) a generated client
#: dispatches on.
_MAX_LIST_LIMIT = 1000

router = APIRouter(prefix="/v1/table", tags=["table"])


#: Re-exported, not redefined — the number lives in `catalog.core.identifiers` now that the CREATE
#: guard depends on it too, and its ceiling is an OpenFGA resolution limit rather than a taste. See
#: that constant for the measurement.
_MAX_NAMESPACE_DEPTH = MAX_NAMESPACE_DEPTH


def _is_expired(expires_at: str) -> bool:
    """Has the purge-eligibility deadline passed? Unparseable or absent → NOT expired.

    Failing to "not expired" is the safe direction: this flag only ever adds urgency to a warning,
    and a malformed timestamp must not make a still-recoverable table read as beyond saving. It
    gates no behaviour — undrop checks whether the RECORD exists, never this (diff2 F10 item 5).
    """
    if not expires_at:
        return False
    try:
        deadline = datetime.fromisoformat(expires_at)
    except ValueError:
        log.warning("trash_expiry_unparseable", extra={"expires_at": expires_at})
        return False
    if deadline.tzinfo is None:  # records written before the stamp carried an offset
        deadline = deadline.replace(tzinfo=UTC)
    return deadline < datetime.now(UTC)


def _collect_tables(ns: LanceNamespace, delimiter: str, root_tables: list[str], include_declared: bool, extra_roots: Sequence[str] = ()) -> list[str]:
    """Every table in the tree, fully qualified — root tables plus each namespace's, depth-first.

    Synchronous and run in a threadpool by the caller: the native namespace client is blocking, and
    a walk of N namespaces is N blocking calls.

    ``extra_roots`` seeds the walk with top-level namespaces the NATIVE enumeration cannot see. The
    shipped ``dir`` backend stores a namespace as a ``__manifest`` ROW and answers a per-namespace
    ``list_tables`` fine — but ``list_namespaces`` at the ROOT yields nothing (measured live: the
    estate held media/silver/alpha/beta with registered tables while this walk returned only the two
    flat root rows, so the lakehouse Tables registry showed 2 of 9). The BINDINGS registry is the
    estate's sanctioned tolerant enumerator (``list_bindings`` — enumeration only, never destructive
    decisions), so its ``top_ns`` names ride in as additional roots. Each visited namespace lists its
    OWN tables — not its children's — so a seeded root with no native child listing still reports.
    """
    found = list(root_tables)
    stack: list[list[str]] = [[], *[[name] for name in extra_roots]]
    seen: set[tuple[str, ...]] = set()
    while stack:
        parent = stack.pop()
        if len(parent) >= _MAX_NAMESPACE_DEPTH or tuple(parent) in seen:
            continue
        seen.add(tuple(parent))
        if parent:
            try:
                tables = native.call(ns, "list_tables", ListTablesRequest(id=parent, include_declared=include_declared)).tables or []
                found.extend(delimiter.join([*parent, name]) for name in tables)
            except Exception:  # noqa: BLE001 — one bad namespace, not a blank registry
                log.warning("could not list tables in %s", parent, exc_info=True)
        try:
            children = native.call(ns, "list_namespaces", ListNamespacesRequest(id=parent)).namespaces or []
        except Exception:  # noqa: BLE001 — a namespace we cannot list must not empty the whole list
            log.warning("could not list namespaces under %s", parent or "<root>", exc_info=True)
            continue
        stack.extend([*parent, child] for child in children)
    # A bound namespace the native walk ALSO found arrives twice — dedupe, deterministically.
    return sorted(set(found))


@router.get("", response_model_exclude_none=True)
async def list_all_tables(
    request: Request,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    page_token: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=_MAX_LIST_LIMIT)] = None,
    include_declared: bool = True,
) -> ListTablesResponse:
    """List every table in the namespace via ``list_all_tables``; when FGA is on,
    filter the result down to the tables the caller can ``can_read_data``.
    ``include_declared=false`` drops declared-only tables (reserved, no storage yet).

    Pagination is applied to the FINAL merged result, never to the native root call: the response is
    root tables + bound seeds + the namespace walk, so a native-level ``limit`` would truncate only
    the first ingredient and silently DROP tables from every page (#141 — the endpoint advertised
    pagination and ignored it). The merged list is sorted and deduped, so the cursor is keyset-style:
    ``page_token`` = the last name of the page, stateless and stable across pages. The walk itself
    still costs the full estate server-side per call — acceptable at admin frequency, and honest now.
    """
    req = ListTablesRequest(id=[], include_declared=include_declared)
    response: ListTablesResponse = await run_in_threadpool(native.call, ns, "list_all_tables", req)
    # RECURSE INTO CHILD NAMESPACES. `list_all_tables` at the root returns only tables sitting
    # DIRECTLY at the root — despite the name — so with the estate's own convention (tiers are
    # namespaces: bronze$pages, silver$features, gold$catalog) this endpoint answered `{"tables":[]}`
    # while the catalog happily reported `bronze` -> ["pages"] one call away. Both lakehouse
    # registries (Tables and Namespaces, which derives its groups from THIS list) were therefore
    # permanently empty the moment anyone did the right thing and put a table in a namespace.
    #
    # Names come back FULLY QUALIFIED (`bronze$pages`), which is what the UI already assumes — it
    # groups on the delimiter — and what the FGA filter below matches against `table:<name>`.
    # BOUND namespaces first, each through its OWN warehouse-rooted connection. The request-scoped
    # `ns` on a collection route is ALWAYS the default root (get_namespace has no {id} to route by),
    # so listing a bound seed through it structurally returns nothing — measured live as the estate
    # listing showing 2 of 9 while every per-namespace route saw everything. `namespace_for_top_ns`
    # is the same binding resolution the per-id routes use; a seed that fails (deactivated warehouse,
    # unreadable bucket) degrades to a warning, never a blank registry.
    bound = await run_in_threadpool(warehouses.list_bindings, settings.registry_root, settings.storage_options())
    for b in bound:
        top = b["top_ns"]
        try:
            seed_ns = await namespace_for_top_ns(request, settings, top)
            listed = await run_in_threadpool(native.call, seed_ns, "list_tables", ListTablesRequest(id=[top], include_declared=include_declared))
            response.tables.extend(f"{top}{settings.delimiter}{name}" for name in (listed.tables or []))
        except Exception:  # noqa: BLE001 — one bad warehouse, not a blank estate
            log.warning("could not list bound namespace %s", top, exc_info=True)
    response.tables = await run_in_threadpool(_collect_tables, ns, settings.delimiter, response.tables, include_declared)
    # When FGA is on and the caller is known, return only the tables they can read.
    if settings.fga_enabled and token is not None and client is not None:
        # `.objects` + `.truncated`: past OpenFGA's 1000-object cap this listing is SHORT, and the
        # page cursor below would be minted from the shortened list. Surfaced to the caller rather
        # than only logged — a client cannot otherwise tell a small estate from a truncated answer.
        listing = await fga.list_objects(client, user=token.sub, relation="can_read_data", object_type="table")
        allowed = set(listing.objects)
        authorization_truncated = listing.truncated
        response.tables = [name for name in response.tables if f"table:{name}" in allowed]
        # THE SPEC'S OWN EXTENSION POINT. `ListTablesResponse` is a `lance_namespace` model with fields
        # `context`, `tables`, `page_token`, and `context` is documented as "arbitrary context as
        # key-value pairs ... custom to the specific implementation". So the flag rides there rather
        # than as a new body field, which would have broken the spec conformance the catalog is built on.
        if authorization_truncated:
            response.context = {**(response.context or {}), "authorization_truncated": "true"}
    # Paginate AFTER the FGA filter so pages count only tables the caller can see.
    response.tables, response.page_token = _paginate(response.tables, page_token, limit)
    return response


# `_paginate` moved to `catalog.api.pagination` when the model registry needed the same cursor —
# two keyset implementations in one service is two things to keep in step. Re-exported below so this
# module's callers are unchanged.
@router.post("/{id}/declare", response_model_exclude_none=True)
async def declare_table(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    emitter: LineageEmitterDep,
    control: ControlEmitterDep,
    body: DeclareTableRequest | None = None,
    authorization: Annotated[str | None, Header()] = None,
) -> DeclareTableResponse:
    """Declare a new (empty) table at ``id`` via ``declare_table``, then seed the caller's FGA ownership
    and emit a versionless DECLARE_TABLE marker (the table's first provenance — who reserved it + where)."""
    # LANCE-ONLY (2026-08-15 ruling). This door takes `properties` through its spec request
    # model and never checked it, so a non-Lance format could be selected here while the create
    # door rejected it. Before anything is reserved.
    reject_unsupported_format(body.properties if body else None)
    segments = parse_identifier(id, settings.delimiter)
    await fga_deps.require_parent_exists(ns, "table", segments, delimiter=settings.delimiter)
    # The id must not still belong to a trashed table (diff2 F10 item 4): a recoverable drop KEEPS
    # its grants, so creating here would hand the new table the dead one's readers and writers.
    await fga_deps.require_no_live_trash(settings, segments)
    req = body or DeclareTableRequest()
    req.id = reconcile_body_id(segments, req.id)
    response: DeclareTableResponse = await run_in_threadpool(native.call, ns, "declare_table", req)

    async def _undo_declare() -> None:
        await run_in_threadpool(native.call, ns, "drop_table", DropTableRequest(id=segments))

    # A declared table holds NO data — the undo can never destroy bytes, so this door is the safest
    # possible place to compensate and there was never a reason for it to be the riskiest. Without it a
    # failed seed left a declared-only table that its declarer could not see, could not drop, and could
    # not re-declare (native `AlreadyExists`), reserving the id against everyone, permanently (F3).
    await fga_deps.seed_ownership_or_compensate(client, settings, token, resource="table", segments=segments, undo=_undo_declare)
    # Versionless (no data yet): records who declared it + the reserved location, and keys the CREATED edge
    # (declare_table ∈ lineage _CREATE_OPS). Reconcile fills the real version once data lands at the URI.
    await emit_write_event(
        emitter,
        segments,
        delimiter=settings.delimiter,
        author=token.sub if token is not None else None,
        version=None,
        operation=DECLARE_TABLE,
        authorization=authorization,
        source_uri=response.location,
    )
    await emit_control(
        control,
        action="table_declared",
        object_type="table",
        object_id=f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}",
        actor=f"user:{token.sub}" if token is not None else None,
        extra={"location": response.location},
    )
    return response


@router.post("/{id}/describe", response_model_exclude_none=True)
def describe_table(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    vendor: VendorDep,
    with_table_uri: bool | None = None,
    load_detailed_metadata: bool | None = None,
    check_declared: bool | None = None,
    version: int | None = None,
    tag: str | None = None,
    vend_credentials: bool | None = None,
) -> DescribeTableResponse:
    """Describe the table at ``id`` (schema / uri / detailed metadata) via ``describe_table``,
    optionally at ``?version=N`` or ``?tag=<name>`` (spec 0.9: mutually exclusive).

    ``tag`` is resolved to its version HERE via the dataplane's tag store: the native backend at
    pylance 8.0.0 silently IGNORES a describe-request ``tag`` (probed 2026-07-10 — a nonexistent tag
    described the LATEST version with no error), so forwarding it would lie; the catalog resolves
    (404 on an unknown tag, like ``/tags/version``) and describes at the resolved version instead.

    ``vend_credentials`` (spec 0.9) returns short-lived, table-scoped ``storage_options`` in the response —
    the SPEC'S OWN way for a client to get credentials. We previously ignored the field entirely and offered
    only a bespoke ``POST /{id}/credentials``, which meant a GENERIC Lance client (including medallion-producer in
    REST mode) got no credentials and had no way to discover our endpoint: interop-breaking, and the one
    confirmed reinvention in the 2026-07-14 audit. ``/credentials`` remains as the richer superset (tiers,
    web-identity exchange).

    READ TIER ONLY, deliberately. The router already gates describe on the reader rung; vending a WRITE
    credential from a read-authorized call would be a privilege escalation. A write credential still requires
    ``POST /{id}/credentials?tier=write``, which separately checks ``can_write_data``.

    Multi-base tables fall back to server-mediated (no storage_options): the STS session policy is scoped to
    the table's PRIMARY root bucket, so a vended client could not reach fragments living in a registered data
    base — the same #3-B ⊥ #2 conflict the credentials endpoint already handles.
    """
    segments = parse_identifier(id, settings.delimiter)
    if tag is not None:
        if version is not None:
            raise InvalidInputError("`tag` cannot be used together with `version` (spec 0.9)")
        resolved = dataplane.get_tag_version(ns, so, GetTableTagVersionRequest(id=segments, tag=tag))
        version = resolved.version
    req = DescribeTableRequest(
        id=segments,
        with_table_uri=with_table_uri,
        load_detailed_metadata=load_detailed_metadata,
        check_declared=check_declared,
        version=version,
    )
    response: DescribeTableResponse = native.call(ns, "describe_table", req)

    # pylance 8.0.0 leaves `metadata` empty even with load_detailed_metadata — so the #74 Table Properties
    # UI could write a property but never read it back (browser-driven find 2026-07-21). Fill it from the
    # dataset's schema metadata (best-effort: a read failure must never break describe).
    if load_detailed_metadata and not response.metadata:
        try:
            schema_meta = dataplane.read_schema_metadata(ns, so, segments)
            if schema_meta:
                response.metadata = schema_meta
        except Exception:
            log.warning("describe_schema_metadata_read_failed", extra={"table": "/".join(segments)})

    if vend_credentials and response.location:
        if settings.multibase_data_base_list and _has_external_bases(response.location, so):
            return response  # multi-base: a root-scoped credential cannot reach the data bases
        creds = vendor.vend(table_location=response.location, tier="read")
        if creds is not None:
            response.storage_options = creds.storage_options
    return response


@router.post("/{id}/exists", status_code=200)
def table_exists(id: str, ns: NamespaceDep, settings: SettingsDep) -> None:
    """Check the table at ``id`` exists via ``table_exists`` — 200 if present (spec 0.9), else error."""
    native.call(ns, "table_exists", TableExistsRequest(id=parse_identifier(id, settings.delimiter)))


@router.post("/{id}/drop", response_model_exclude_none=True)
async def drop_table(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    client: FgaClientDep,
    emitter: LineageEmitterDep,
    control: ControlEmitterDep,
    token: CurrentToken,
    authorization: Annotated[str | None, Header()] = None,
    force: bool = False,
    purge: bool = False,
) -> DropTableResponse:
    """Drop the table at ``id`` via ``drop_table``, then revoke its FGA tuples and
    emit a best-effort ``drop_table`` lineage event.

    Deletion protection (#73, the warehouse door's Decision-5 contract extended to the rung where a
    drop deletes BYTES): a ``protected`` control-root record refuses 409 unless ``force=true``, and
    ``force`` turns the protection lock ONLY — the FGA gate ran before this handler, identically
    with or without it."""
    segments = parse_identifier(id, settings.delimiter)
    canonical = fga.canonical_object_id(segments, delimiter=settings.delimiter)
    guard = await run_in_threadpool(protection.get_protection, settings.registry_root, settings.storage_options(), "table", canonical)
    fga_deps.require_not_protected(guard or {}, kind="table", obj_id=canonical, force=force)
    # #75 the drop→undrop path. With a grace period configured, a drop DEREGISTERS (detaches the
    # pointer; the bytes stay exactly where they are) and files a trash record naming the location and
    # the deadline. This is what makes a fat-fingered drop survivable: time-travel cannot help here,
    # because `restore_table` rewinds a LIVE table and a real drop leaves no version to rewind to.
    # `purge=true` is the explicit opt-out — a caller who means "destroy the bytes now" says so.
    trashed = False
    if settings.trash_grace_days > 0 and not purge:
        described: DescribeTableResponse = await run_in_threadpool(native.call, ns, "describe_table", DescribeTableRequest(id=segments))
        if described.location:
            # RECORD FIRST, DETACH SECOND (diff2 F6 leg a). This was deregister-then-record, and a
            # crash in that window produced the worst state in the plane: the manifest row was gone,
            # no trash record existed, and the bytes sat on storage unreachable by everything.
            # `undrop` 404s (no record), the purge never sees it (it enumerates records), and the
            # obvious retry cannot converge either — `describe_table` 404s two lines up, so the door
            # can never reach the write again. Only a hand-run `register_table` recovers it.
            #
            # Reversed, the crash window leaves a record on a LIVE table, which every consumer already
            # handles: the purge refuses it `STILL_REGISTERED` (`live_ids` is checked before anything
            # destructive), the drop retry re-describes cleanly and overwrites the same hashed key,
            # and `undrop` converges on it now that the register tolerates `TableAlreadyExists`. The
            # residue is bounded by one retry; the old one was permanent.
            #
            # The `if described.location` guard is what makes this sound HERE and is why the cascade
            # path is not reversed with it: a byte-LESS record (a declared-only table, a namespace
            # row) skips the purge's estate test, so a record filed on a still-live object inside a
            # DEACTIVATED warehouse would be revoked and cleared. Every record this branch writes owns
            # bytes, so that case cannot arise.
            #
            # Known interaction, deliberate: while the record stands on a live table the maintenance
            # sweep excludes that table (F6 leg d). Bounded by the same retry, and the alternative is
            # losing the data outright.
            record = trash.make_record(
                canonical,
                location=described.location,
                dropped_by=f"user:{token.sub}" if token is not None else None,
                grace_days=settings.trash_grace_days,
            )
            await run_in_threadpool(trash.put, settings.registry_root, settings.storage_options(), record)
            await run_in_threadpool(native.call, ns, "deregister_table", DeregisterTableRequest(id=segments))
            trashed = True
    if not trashed:
        response: DropTableResponse = await run_in_threadpool(native.call, ns, "drop_table", DropTableRequest(id=segments))
    else:
        response = DropTableResponse()
    # The record's job ends with the object: clear it so a LATER table reusing this id does not
    # inherit a protection nobody set on it (the same reuse rule as the FGA revoke below).
    if guard:  # only when one existed — a clear on nothing is a guaranteed-wasted S3 DELETE per drop
        await run_in_threadpool(protection.clear_protection, settings.registry_root, settings.storage_options(), "table", canonical)
    # The MAINTENANCE POLICY is the same kind of record and needs the same reuse rule — it had none, so
    # `delete_policy` was reachable only from the explicit policy-delete endpoints and a destroyed table
    # left its policy behind forever. That record is not inert: the sweep discovers datasets by walking
    # storage for a `_versions/` marker, not by reading the registry, so a re-created table at the same
    # id is swept under a retention window and fragment sizing nobody set on it.
    # NOT on the recoverable path, exactly as the namespace door keeps its warehouse binding and the
    # drop keeps its grants: undrop restores the table, and it must come back configured as it was.
    if not trashed:
        await run_in_threadpool(maintenance_policies.delete_policy, settings.registry_root, settings.storage_options(), "table", canonical)
    # Record the drop as best-effort lineage — provenance of the deletion (the dataset node persists in the
    # graph, named a `drop_table` run). Inline-awaited (NOT BackgroundTasks) → reaches the durable
    # Dapr/JetStream transport before the response; best-effort, so it never fails the drop. Emitted BEFORE
    # the revoke: on the http transport the caller's bearer authorizes ingest against their (still-live)
    # write grant — revoking first would 403 the very event that records who dropped the table.
    await emit_write_event(
        emitter,
        segments,
        delimiter=settings.delimiter,
        author=token.sub if token is not None else None,
        version=None,
        operation=DROP_TABLE,
        authorization=authorization,
    )
    # Revoke the table's FGA tuples so a later table reusing this id can't inherit stale grants — but
    # ONLY on a destructive drop. A RECOVERABLE drop (#75) leaves an object that still exists and whose
    # owner is the one person who needs to undrop it; revoking here made undrop unreachable for exactly
    # that caller (found by driving the deployed catalog, not by a unit test — the unit tests run FGA
    # off). The grants die with the bytes instead: at purge, or when the sweep's expiry reclaims it.
    if not trashed:
        await fga_deps.revoke_ownership(client, settings, resource="table", segments=segments, token=token)
    await emit_control(
        control,
        action="table_dropped",
        object_type="table",
        object_id=f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}",
        actor=f"user:{token.sub}" if token is not None else None,
        extra={"recoverable": trashed},
    )
    return response


@router.post("/{id}/deregister", response_model_exclude_none=True)
async def deregister_table(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    client: FgaClientDep,
    emitter: LineageEmitterDep,
    control: ControlEmitterDep,
    token: CurrentToken,
    authorization: Annotated[str | None, Header()] = None,
    force: bool = False,
) -> DeregisterTableResponse:
    """Deregister the table at ``id`` (detach it without deleting data) via lance_namespace
    ``deregister_table``, then revoke its FGA ownership and emit a best-effort ``deregister_table`` marker.

    Protection-gated like drop (#73): deregister keeps bytes but REMOVES the object from governance —
    the flag's whole jurisdiction — so leaving it ungated would make "deregister, then delete the
    files by hand" the unprotected path around the protected drop."""
    segments = parse_identifier(id, settings.delimiter)
    canonical = fga.canonical_object_id(segments, delimiter=settings.delimiter)
    guard = await run_in_threadpool(protection.get_protection, settings.registry_root, settings.storage_options(), "table", canonical)
    fga_deps.require_not_protected(guard or {}, kind="table", obj_id=canonical, force=force)
    response: DeregisterTableResponse = await run_in_threadpool(native.call, ns, "deregister_table", DeregisterTableRequest(id=segments))
    if guard:  # only when one existed — a clear on nothing is a guaranteed-wasted S3 DELETE per drop
        await run_in_threadpool(protection.clear_protection, settings.registry_root, settings.storage_options(), "table", canonical)
    # Record the detach as best-effort lineage — asymmetric with drop (which deletes data), deregister
    # only detaches, so without this marker the Dataset node looks like a still-live, never-touched table.
    # Versionless (no data was written), inline-awaited so it reaches the durable transport before the
    # reply. Emitted BEFORE the revoke: on the http transport the caller's bearer authorizes ingest against
    # their (still-live) write grant — revoking first would 403 the very marker this endpoint exists to
    # record, and the graph would keep showing the table as live.
    await emit_write_event(
        emitter,
        segments,
        delimiter=settings.delimiter,
        author=token.sub if token is not None else None,
        version=None,
        operation=DEREGISTER_TABLE,
        authorization=authorization,
    )
    await fga_deps.revoke_ownership(client, settings, resource="table", segments=segments, token=token)
    await emit_control(
        control,
        action="table_deregistered",
        object_type="table",
        object_id=f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}",
        actor=f"user:{token.sub}" if token is not None else None,
        extra={},
    )
    return response


@router.post("/{id}/register", response_model_exclude_none=True)
async def register_table(
    id: str,
    body: RegisterTableRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    emitter: LineageEmitterDep,
    control: ControlEmitterDep,
    authorization: Annotated[str | None, Header()] = None,
) -> RegisterTableResponse:
    """Register an existing table location at ``id`` via ``register_table``, then seed the caller's FGA
    ownership and emit a REGISTER_TABLE marker (who attached it + where)."""
    # LANCE-ONLY (2026-08-15 ruling) — same bypass as `declare_table`; `body` is required here.
    reject_unsupported_format(body.properties)
    segments = parse_identifier(id, settings.delimiter)
    await fga_deps.require_parent_exists(ns, "table", segments, delimiter=settings.delimiter)
    # The id must not still belong to a trashed table (diff2 F10 item 4): a recoverable drop KEEPS
    # its grants, so creating here would hand the new table the dead one's readers and writers.
    await fga_deps.require_no_live_trash(settings, segments)
    body.id = reconcile_body_id(segments, body.id)
    response: RegisterTableResponse = await run_in_threadpool(native.call, ns, "register_table", body)

    async def _undo_register() -> None:
        # DEREGISTER, never drop. Register ATTACHES bytes that already existed and are not ours to
        # destroy — the whole point of the door. Deregister is the exact inverse: it removes the
        # catalog object this request created and leaves the data exactly where it was found.
        await run_in_threadpool(native.call, ns, "deregister_table", DeregisterTableRequest(id=segments))

    await fga_deps.seed_ownership_or_compensate(client, settings, token, resource="table", segments=segments, undo=_undo_register)
    # Versionless + source_uri=the attached location, keying the CREATED edge (register_table ∈ _CREATE_OPS).
    # The registered table already holds data at some version; we don't reopen a possibly-external location on
    # the request path (a reopen failure must never fail an already-committed register) — #23 reconcile reads
    # that source_uri and back-fills the real on-disk version (UNTRACKED → in-sync).
    await emit_write_event(
        emitter,
        segments,
        delimiter=settings.delimiter,
        author=token.sub if token is not None else None,
        version=None,
        operation=REGISTER_TABLE,
        authorization=authorization,
        source_uri=response.location,
    )
    await emit_control(
        control,
        action="table_registered",
        object_type="table",
        object_id=f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}",
        actor=f"user:{token.sub}" if token is not None else None,
        extra={"location": response.location},
    )
    return response


@router.get("/{id}/protection", response_model_exclude_none=True)
async def get_table_protection(id: str, settings: SettingsDep) -> ProtectionResponse:
    """Read the deletion-protection flag (#123). The SET door shipped a year of writes with NO read:
    an operator could not arm, disarm-check, or audit protection — they discovered it by eating a
    409. Owner-gated like the set door (the observer of a safety is whoever might trip it), and the
    record's `set_by` comes back so a refused drop can name who armed it."""
    segments = parse_identifier(id, settings.delimiter)
    canonical = fga.canonical_object_id(segments, delimiter=settings.delimiter)
    record = await run_in_threadpool(protection.get_protection, settings.registry_root, settings.storage_options(), "table", canonical)
    return ProtectionResponse(id=canonical, protected=bool(record), set_by=(record or {}).get("set_by"))


@router.get("/{id}/tasks", response_model_exclude_none=True)
async def table_tasks(
    id: str,
    settings: SettingsDep,
    token: CurrentToken,
) -> list[TrashEntry]:
    """What is queued for THIS table (#75 brings §2.4). Today that is exactly one thing: a pending
    trash expiry. It exists the moment expiry does, because an undrop deadline the owner cannot see
    is not a safety feature — the estate's task surfaces are otherwise all estate-global, so "what is
    scheduled against my table" was unanswerable. Reader-gated by the router alongside describe.

    `expires_at` is when the object becomes PURGE-ELIGIBLE, not when recovery stops (diff2 F10 item
    5). Undrop does not check the clock — it checks whether the record is still there — so a passed
    deadline is a warning, not a verdict, and `expired` says which state you are in. Reporting it as
    finality would push an owner to give up on data that is still on storage and still recoverable.
    """
    canonical = fga.canonical_object_id(parse_identifier(id, settings.delimiter), delimiter=settings.delimiter)
    record = await run_in_threadpool(trash.get, settings.registry_root, settings.storage_options(), canonical)
    if record is None:
        return []
    fields = {k: str(record.get(k, "")) for k in ("id", "location", "dropped_by", "dropped_at", "expires_at")}
    return [TrashEntry(**fields, expired=_is_expired(fields["expires_at"]))]


@router.post("/{id}/undrop", response_model_exclude_none=True)
async def undrop_table(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
) -> RegisterTableResponse:
    """Recover a dropped table from the trash (#75) — re-register its still-present bytes at its old
    id and clear the record. Owner-gated (``undrop`` maps to the drop rung: restoring an object into
    the namespace is the same authority as removing it).

    404 when there is no trash record — a never-trashed or already-PURGED drop is genuinely
    unrecoverable, and saying so plainly beats a 200 that recovers nothing.

    THE CLOCK DOES NOT GATE THIS, deliberately (diff2 F10 item 5). A record whose `expires_at` has
    passed but which the purge has not yet collected still undrops, because the bytes are still on
    storage and refusing would destroy recoverable data on a timestamp alone. The estate already
    reasons this way one service over — maintenance's own config says "a record that survives is a
    recovery that still works; a purged one is not" — and this door simply never said so out loud.
    What was wrong was the DESCRIPTION: the previous docstring called an expired drop "genuinely
    unrecoverable" alongside a never-trashed one, and `/tasks` reported the deadline as finality.
    Both now say purge-eligible, which is what it is.

    An expired undrop IS logged: it means the purge is behind, or somebody recovered at the very
    edge of the window, and an operator should be able to see either.
    """
    segments = parse_identifier(id, settings.delimiter)
    canonical = fga.canonical_object_id(segments, delimiter=settings.delimiter)
    so = settings.storage_options()
    record = await run_in_threadpool(trash.get, settings.registry_root, so, canonical)
    if record is None:
        raise TableNotFoundError(
            f"no recoverable drop for table: {canonical}. Either it was never dropped recoverably "
            "(the grace period was off), or the purge has already reclaimed its bytes."
        )
    if _is_expired(str(record.get("expires_at", ""))):
        log.warning("undrop_past_expiry", extra={"table": canonical, "expires_at": record.get("expires_at")})
    # `register_table` REFUSES an absolute URI ("Location must be a relative path within the root
    # directory") — found by driving the deployed catalog, where undrop 400'd on the very location
    # `describe_table` had just reported. The `dir` backend lays table directories out FLAT under the
    # connection root (`<uuid>_<table_id>`), so the relative form is the final path segment; the
    # absolute URI stays on the record because that is what an operator reading the trash needs.
    location = str(record["location"])
    body = RegisterTableRequest(id=segments, location=location.rstrip("/").rsplit("/", 1)[-1])
    try:
        response: RegisterTableResponse = await run_in_threadpool(native.call, ns, "register_table", body)
    except TableAlreadyExistsError:
        # ALREADY RECOVERED — converge instead of refusing. The namespace undrop has tolerated this
        # since #96 (`undrop_table_already_registered`); the table door did not, and that asymmetry
        # became a trap the moment the maintenance sweep started honouring trash records (F6(d)): a
        # crash between this register and the `trash.clear` below leaves a record on a LIVE table, so
        # the table is silently excluded from compaction and version GC — and the obvious retry used
        # to 409 here, leaving the record in place forever. Now the retry falls through to the clear
        # and the table rejoins the sweep.
        log.info("undrop_table_already_registered", extra={"table": canonical})
        response = RegisterTableResponse(location=location)

    # NO SEED — undrop is a PURE RESTORE (owner ruling, diff2 F10 item 4b).
    #
    # This used to call `seed_ownership_or_compensate`, which made the CALLER an owner. The recoverable
    # drop deliberately keeps the table's tuples (#75 — the owner is the one person who needs to undrop
    # it), so the original owner's grants are already live when we get here: the seed never restored
    # anything, it only ADDED whoever pressed the button. Anna drops a table, Bob undrops it, and the
    # estate silently gained a second owner with no grant ever having been made.
    #
    # The deliberate trade: a table that was ALREADY ownerless before the drop stays ownerless after
    # the undrop. The seed used to paper over that by granting the rescuer, which hid a pre-existing
    # stranding behind a privilege escalation. Surfacing stranded objects is the reconciler's job
    # (diff2 F3.3), not this door's.
    #
    # With the seed gone, nothing between the register and the clear can fail in a way that needs
    # compensating — the register is idempotent above, and a failed clear converges on retry — so the
    # `_undo_undrop` deregister went with it.
    await run_in_threadpool(trash.clear, settings.registry_root, so, canonical)
    await emit_control(
        control,
        action="table_undropped",
        object_type="table",
        object_id=f"table:{canonical}",
        actor=f"user:{token.sub}" if token is not None else None,
        extra={},
    )
    return response


@router.post("/{id}/protection", response_model_exclude_none=True)
async def set_table_protection(
    id: str,
    body: SetProtectionRequest,
    settings: SettingsDep,
    token: CurrentToken,
    control: ControlEmitterDep,
) -> ProtectionResponse:
    """Set or clear deletion protection on the table at ``id`` (#73 — the warehouse contract on the
    rung where a drop deletes bytes). Owner-gated by the router (``protection`` maps to ``can_drop``:
    whoever may destroy the table decides whether destroying it needs a second thought). The flag is
    a CONTROL-ROOT record, deliberately not schema metadata — control-plane state that emits a
    control event and never creates a table version, readable even when the dataset is corrupted,
    and unreachable from the future properties write door (#78)."""
    segments = parse_identifier(id, settings.delimiter)
    canonical = fga.canonical_object_id(segments, delimiter=settings.delimiter)
    so = settings.storage_options()
    if body.protected:
        record = {
            "kind": "table",
            "id": canonical,
            "protected": "true",
            "set_by": f"user:{token.sub}" if token is not None else "anonymous",
        }
        await run_in_threadpool(protection.set_protection, settings.registry_root, so, record)
    else:
        await run_in_threadpool(protection.clear_protection, settings.registry_root, so, "table", canonical)
    await emit_control(
        control,
        action="table_protected" if body.protected else "table_unprotected",
        object_type="table",
        object_id=f"table:{canonical}",
        actor=f"user:{token.sub}" if token is not None else None,
        extra={},
    )
    return ProtectionResponse(id=canonical, protected=body.protected)


@router.post("/{id}/rename", response_model_exclude_none=True)
async def rename_table(
    id: str,
    body: RenameTableRequest,
    ns: NamespaceDep,
    so: StorageOptionsDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    emitter: LineageEmitterDep,
    control: ControlEmitterDep,
    authorization: Annotated[str | None, Header()] = None,
    force: bool = False,
) -> RenameTableResponse:
    """Rename the table at ``id`` IN-PROCESS (#5b), then migrate its FGA ownership and emit dest←source
    lineage.

    Lance has no format-level rename (table rename is a namespace-layer remap, not a data-plane commit) and
    the ``dir`` backend's ``rename_table`` is a hard 501 — 501 is also off-spec (the Namespace REST spec
    makes rename a first-class Metadata op). So the rename is done in-process: the self-contained dataset
    root is byte-copied to the destination location (preserving version history) and the source pointer is
    deregistered (``dataplane.rename_table``). The caller must hold ``can_drop`` on the source (owner tier,
    gated by the router ``authorize``) AND ``can_create_table`` on the DESTINATION parent — else a source
    owner could plant their table into a namespace/tenant they lack create rights on. FGA tuples migrate
    from the old id to the new; a versionless REGISTER marker records the (re)attachment at the new location
    so the destination appears in the graph with its provenance (#23 reconcile back-fills its on-disk
    version). Source missing → 404 ``TableNotFound``; destination name taken → 409 ``TableAlreadyExists``."""
    segments = parse_identifier(id, settings.delimiter)
    body.id = reconcile_body_id(segments, body.id)  # a contradictory body id is a 400, like every {id} route
    # #73: a rename DELETES the source bytes (byte-copy + deregister below), so the source's protection
    # gates it exactly like drop. `force` rides the query string as on the sibling doors. Checked FIRST —
    # before the parent/create gates — so a protected source refuses identically regardless of destination.
    canonical = fga.canonical_object_id(segments, delimiter=settings.delimiter)
    guard = await run_in_threadpool(protection.get_protection, settings.registry_root, settings.storage_options(), "table", canonical)
    fga_deps.require_not_protected(guard or {}, kind="table", obj_id=canonical, force=force)
    # Rename mints a new table identifier under ``new_namespace_id`` (defaulting to the source's parent
    # namespace, i.e. all source segments but the last) + ``new_table_name``.
    dest_parent = list(body.new_namespace_id) if body.new_namespace_id else segments[:-1]
    new_segments = [*dest_parent, body.new_table_name]
    # A rename can MINT an orphan as surely as a create: `new_namespace_id: []` moves the table to a
    # parentless id. Same rule, same door — checked before the native call, so a refused rename leaves
    # the source exactly where it was.
    await fga_deps.require_parent_exists(ns, "table", new_segments, delimiter=settings.delimiter)
    # The id must not still belong to a trashed table (diff2 F10 item 4): a recoverable drop KEEPS
    # its grants, so creating here would hand the new table the dead one's readers and writers.
    await fga_deps.require_no_live_trash(settings, new_segments)
    # Renaming INTO a namespace is a create in that namespace: authorize can_create_table on the DESTINATION
    # parent BEFORE the (destructive, relocating) rename — else a source-table owner could plant their table
    # into a namespace/tenant they have no create rights on. (authorize already gated can_drop on the source.)
    await fga_deps.require_create_on_parent(client, settings, token, resource="table", segments=new_segments)
    new_segments, location = await run_in_threadpool(dataplane.rename_table, ns, so, segments, body.new_table_name, body.new_namespace_id)
    # Emit the SOURCE's terminal DROP marker BEFORE revoking its tuples. The default ``http`` lineage
    # transport runs ``enforce_output_authz`` (``can_write_data`` on the source); revoking first deletes the
    # parent→writer edge, so even an admin would 403 and this best-effort emit would be silently swallowed —
    # the sibling drop/deregister endpoints emit-before-revoke for exactly this reason (audit 2026-07-14). A
    # rename DELETES the source bytes, so it is a DROP (not a deregister, which keeps data); without this
    # marker the source's most recent successful run stays a WRITE and it looks alive forever
    # (``dropped_at()`` never fires; reconcile WARNs ``missing_on_storage`` against the deleted location).
    await emit_write_event(
        emitter,
        segments,
        delimiter=settings.delimiter,
        author=token.sub if token is not None else None,
        version=None,
        operation=DROP_TABLE,
        authorization=authorization,
    )
    # SEED THE DESTINATION FIRST, then revoke the source. The order is the fix, and it is a deliberate
    # choice between two imperfect failure states (owner ruling 2026-08-15, diff2 F3).
    #
    # It was revoke-then-seed. The native rename has already committed by this point, so if the seed
    # then failed the table existed at the NEW id holding no tuples at all, while the OLD id's grants
    # had just been deleted — no owner anywhere, and `can_drop: owner` (with `owner from parent`
    # unable to help, since seed writes owner and parent as ONE batch) means literally nobody in the
    # estate could act on it. Permanently stranded, repairable only by hand-written tuples.
    #
    # Seeding first inverts which half is lost: a failed REVOKE leaves a stale grant on the source id,
    # which no longer names a table. That is a real but narrow risk — it only matters if that exact id
    # is later reused, and the create path's own `revoke_ownership` already clears stale grants on a
    # reused id for precisely this reason. Weighed against an object nobody can touch, the estate
    # prefers the recoverable state: the table always has a live owner who can finish the cleanup.
    #
    # The revoke is best-effort for the same reason the compensations are: it is an OpenFGA call, and
    # if OpenFGA is what is down, failing the whole rename here would strand the caller after a native
    # mutation that already succeeded. The stale grant is logged, not raised.
    await fga_deps.seed_ownership(client, settings, token, resource="table", segments=new_segments)
    try:
        await fga_deps.revoke_ownership(client, settings, resource="table", segments=segments, token=token)
    except Exception as revoke_exc:
        log.warning(
            "rename_source_revoke_failed",
            extra={"source": fga.canonical_object_id(segments, delimiter=settings.delimiter), "error": str(revoke_exc)},
        )
    # The MAINTENANCE POLICY migrates with the tuples, for the same reason they do: a rename relocates an
    # object and keeps what is attached to it. Nothing moved it before, so the record stayed keyed to the
    # OLD canonical id — `_key` hashes `kind:canonical_id` — and therefore matched nothing while the
    # renamed table silently reverted to default retention, fragment sizing and cleanup toggles. A rename
    # is supposed to move a table, not reconfigure it. Best-effort and logged, never raised: the native
    # rename has already committed by here, so a failed policy move must not strand the caller.
    try:
        await run_in_threadpool(
            maintenance_policies.migrate_policy,
            settings.registry_root,
            so,
            "table",
            canonical,
            fga.canonical_object_id(new_segments, delimiter=settings.delimiter),
        )
    except Exception as policy_exc:
        log.warning("rename_policy_migrate_failed", extra={"source": canonical, "error": str(policy_exc)})
    # …then the DESTINATION's (re)attachment at its new location — AFTER its ownership is seeded, so the
    # emit's own ``can_write_data`` check passes on the http transport. Versionless: #23 reconcile back-fills
    # the real on-disk version, which the relocate preserved along with the whole version history.
    await emit_write_event(
        emitter,
        new_segments,
        delimiter=settings.delimiter,
        author=token.sub if token is not None else None,
        version=None,
        operation=REGISTER_TABLE,
        authorization=authorization,
        source_uri=location,
        # DERIVED_FROM: the destination is the renamed SOURCE, so record the source table as this event's
        # input — otherwise the rename severs the provenance chain and the renamed table appears in the graph
        # as an orphan with no history (audit 2026-07-14). The source's DROP marker above ends its own line;
        # this input edge stitches the destination onto it.
        inputs=[InputPin(segments=segments)],
    )
    await emit_control(
        control,
        action="table_renamed",
        object_type="table",
        object_id=f"table:{fga.canonical_object_id(segments, delimiter=settings.delimiter)}",
        actor=f"user:{token.sub}" if token is not None else None,
        extra={
            "from": fga.canonical_object_id(segments, delimiter=settings.delimiter),
            "to": fga.canonical_object_id(new_segments, delimiter=settings.delimiter),
        },
    )
    return RenameTableResponse()


@router.post("/{id}/restore", response_model_exclude_none=True)
async def restore_table(
    id: str,
    body: RestoreTableRequest,
    ns: NamespaceDep,
    settings: SettingsDep,
    so: StorageOptionsDep,
    token: CurrentToken,
    emitter: LineageEmitterDep,
    authorization: Annotated[str | None, Header()] = None,
) -> RestoreTableResponse:
    """Restore the table at ``id`` to a prior version via ``restore_table``; emits a RESTORE_TABLE event at
    the NEW current version (restore mints a fresh version pointing at the restored data)."""
    segments = parse_identifier(id, settings.delimiter)
    body.id = reconcile_body_id(segments, body.id)
    response: RestoreTableResponse = await run_in_threadpool(native.call, ns, "restore_table", body)
    # The response carries only a transaction_id — the shared trailer reads the new current version + its
    # schema off one reopen (best-effort: a readback failure never fails the already-committed restore).
    await lineage_deps.emit_measured_write(
        emitter,
        segments,
        ns=ns,
        so=so,
        settings=settings,
        token=token,
        operation=RESTORE_TABLE,
        authorization=authorization,
    )
    return response


@router.post("/{id}/stats", response_model_exclude_none=True)
def get_table_stats(id: str, ns: NamespaceDep, settings: SettingsDep) -> GetTableStatsResponse:
    """Return storage/row statistics for the table at ``id`` via ``get_table_stats``."""
    req = GetTableStatsRequest(id=parse_identifier(id, settings.delimiter))
    return native.call(ns, "get_table_stats", req)
