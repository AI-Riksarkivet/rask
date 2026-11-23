"""A namespace's subtree, walked and removed: the listings a cascade and a filtered page drain to their end, the
recoverable cascade that detaches every table into the trash, and the destroying one that deletes bottom-up.

The door authorizes the caller, identifies what stands under the namespace, holds the subtree to its protection and
announces what goes; nothing here reads a request or a grant.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from functools import partial

from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    DeregisterTableRequest,
    DescribeNamespaceRequest,
    DescribeTableRequest,
    DescribeTableResponse,
    DropNamespaceRequest,
    InternalError,
    InvalidTableStateError,
    LanceNamespace,
    ListNamespacesRequest,
    ListNamespacesResponse,
    ListTablesRequest,
    ListTablesResponse,
    NamespaceNotFoundError,
    TableNotFoundError,
)

from catalog.core import native
from catalog.core.config import CatalogStore
from catalog.core.identifiers import MAX_NAMESPACE_DEPTH
from catalog.services import clone_endings, object_uuids, table_claims
from catalog.services.clone_ending_records import CloneEnding
from catalog.services.governed_object import GovernedObject
from catalog.services.tables import destroy
from lakehouse.events.control_events import ControlUuids
from lakehouse.governed import fga_ids
from lakehouse.naming import uuid_of
from lakehouse.records import base_registry, clone_sources, location_claims, trash
from lakehouse.storage.options import StorageOptions


log = logging.getLogger(__name__)

# Safety ceiling on the pagination loops in collect_descendants — a runaway/looping backend token can't
# spin forever. Far above any real namespace fan-out.
_MAX_LIST_PAGES = 1000


def collect_descendants(ns: LanceNamespace, segments: list[str]) -> list[tuple[str, list[str]]]:
    """Every ``(resource, segments)`` under ``segments`` — child tables AND nested namespaces, recursively.

    Enumerated BEFORE a Cascade drop removes them (afterwards they can't be listed), so the caller can
    revoke their FGA tuples once the drop commits. ``include_declared`` catches declared-only tables (they
    hold an owner grant too). Blocking native list calls → the caller runs this in a threadpool.

    DEPTH-CAPPED at the same bound the listing walk uses (diff2 F10 item 10). This recursed without one
    while the table listing's walk capped at ``MAX_NAMESPACE_DEPTH``, so the identical pathological
    tree was a truncated listing in one walker and a Python stack overflow in the other — and this is the
    walker that runs on the DESTRUCTIVE path, where crashing mid-enumeration means the cascade proceeds
    against a partial descendant list. Hitting the cap is reported the same way page truncation already
    is, because it has the same consequence: an incomplete revoke leaves orphan grants.
    """
    if len(segments) >= MAX_NAMESPACE_DEPTH:
        log.warning("namespace_depth_capped", extra={"namespace": segments, "cap": MAX_NAMESPACE_DEPTH})
        return []
    found: list[tuple[str, list[str]]] = []
    token: str | None = None
    for _ in range(_MAX_LIST_PAGES):
        tables: ListTablesResponse = native.call(ns, "list_tables", ListTablesRequest(id=segments, include_declared=True, page_token=token))
        found.extend(("table", [*segments, name]) for name in tables.tables or [])
        token = tables.page_token or None
        if not token:
            break
    else:
        # Hit the ceiling with a token still outstanding → a PARTIAL enumeration. Surface it (a partial
        # descendant list makes the cascade revoke silently incomplete → orphan grants), mirroring
        # fga_reads.read_object_tuples' openfga_read_truncated warning. In practice unreachable at this ceiling.
        log.warning("namespace_list_truncated", extra={"namespace": segments, "kind": "tables"})
    token = None
    for _ in range(_MAX_LIST_PAGES):
        children: ListNamespacesResponse = native.call(ns, "list_namespaces", ListNamespacesRequest(id=segments, page_token=token))
        for name in children.namespaces or []:
            child = [*segments, name]
            found.append(("namespace", child))
            found.extend(collect_descendants(ns, child))  # recurse into the nested namespace
        token = children.page_token or None
        if not token:
            break
    else:
        log.warning("namespace_list_truncated", extra={"namespace": segments, "kind": "namespaces"})
    return found


async def drain_tables(ns: LanceNamespace, segments: list[str]) -> tuple[ListTablesResponse, list[str]]:
    """Every table under ``segments``, declared ones included, following the backend's cursor to exhaustion.

    The twin of `drain_namespaces` and for the identical reason: the listing door filters per item and then
    applies its own cursor, so reading the backend's first page alone answers a short list whose
    missing rows are indistinguishable from rows the caller may not read.
    """
    seen: list[str] = []
    token: str | None = None
    response = ListTablesResponse(tables=[])
    for _ in range(_MAX_LIST_PAGES):
        response = await run_in_threadpool(native.call, ns, "list_tables", ListTablesRequest(id=segments, include_declared=True, page_token=token))
        seen.extend(response.tables or [])
        token = response.page_token or None
        if not token:
            break
    else:
        log.warning("namespace_list_truncated", extra={"namespace": segments, "kind": "tables"})
    return response, sorted(set(seen))


async def drain_namespaces(ns: LanceNamespace, segments: list[str]) -> tuple[ListNamespacesResponse, list[str]]:
    """Every child namespace under ``segments``, following the backend's own cursor to exhaustion.

    THE BACKEND'S PAGINATION IS NOT THE CALLER'S. The listing door applies its own keyset cursor AFTER the
    authorization filter, because a backend `limit` truncates BEFORE the filter, so a page could answer short and
    hand out a cursor that skips everything the filter removed. Reading only the backend's first page has the same
    failure in another place: against a paginating backend the door answers a short list with a cursor that looks
    complete, and the dropped rows are indistinguishable from rows the caller may not read. So this drains, as
    `collect_descendants` does on the destructive path.
    """
    seen: list[str] = []
    token: str | None = None
    response = ListNamespacesResponse(namespaces=[])
    for _ in range(_MAX_LIST_PAGES):
        response = await run_in_threadpool(native.call, ns, "list_namespaces", ListNamespacesRequest(id=segments, page_token=token))
        seen.extend(response.namespaces or [])
        token = response.page_token or None
        if not token:
            break
    else:
        # A token still outstanding at the ceiling is a PARTIAL enumeration, and a partial listing that
        # answers 200 is exactly the silence this drain exists to remove. Logged like its sibling.
        log.warning("namespace_list_truncated", extra={"namespace": segments, "kind": "namespaces"})
    return response, sorted(set(seen))


async def trash_subtree(
    ns: LanceNamespace,
    settings: CatalogStore,
    *,
    dropped_by: str | None,
    segments: list[str],
    descendants: list[GovernedObject],
    own: ControlUuids,
    grace_days: float,
) -> list[GovernedObject]:
    """DETACH the subtree instead of destroying it — the recoverable half of a cascade (#96); the tables it detached.

    A trash record pointing at bytes the native cascade already deleted would be a LIE: undrop
    re-registers still-present bytes, and `drop_namespace(cascade)` destroys its children inside one
    native call. So with a grace period the cascade never reaches that call. Instead, exactly what
    the single-table drop door does, per child: every descendant table is DEREGISTERED (pointer
    detached, bytes untouched) and filed in the trash (:func:`_trash_listed_table`); every namespace — children
    deepest-first, then the cascaded root — is then empty and dropped plainly, filed as a ``kind="namespace"``
    record so undrop knows which manifest rows to rebuild. All records share one drop-time stamp
    rule: ``expires_at`` is data on the record, never policy read at expiry.

    Not atomic, deliberately: a mid-loop failure leaves every already-detached child RECOVERABLE
    (its record is filed and its parent namespace still exists), which is strictly safer than the
    all-or-nothing native destroy it replaces.

    Each record is filed under its object's uuid, names its parent namespace's uuid, and carries the uuid of the
    namespace the cascade dropped (``cascade``), so the namespace undrop restores exactly this cascade's records, never
    another object dropped under one of their names ([[LH-525]]). ``descendants`` and ``own`` are what the door read
    while the subtree stood, and ``grace_days`` the window its warehouse's delete profile gives (ADR 0146). A listed
    table that left before its turn is detached by no one and is not among the tables answered.

    Raises:
        InternalError: The dropped namespace keeps no uuid, which no door leaves.
    """
    so = settings.storage_options()
    if own.object_uuid is None:
        raise InternalError(f"namespace {fga_ids.canonical_object_id(segments)!r} keeps no uuid, so its trash cannot be filed")
    cascade = own.object_uuid
    uuid_at = {tuple(item.segments): item.uuid for item in descendants} | {tuple(segments): cascade}

    def parent_of(child: list[str]) -> str | None:
        return uuid_at.get(tuple(child[:-1]), own.namespace_uuid)

    # Deepest-first, so every namespace is empty by the time its own drop runs.
    child_namespaces = sorted((item.segments for item in descendants if item.kind == "namespace"), key=len, reverse=True)
    tables = [item for item in descendants if item.kind == "table"]
    # EVERY NAME RESOLVED FIRST, CONCURRENTLY (catalog-api-11): independent reads whose results are only collected, so a
    # subtree of N tables pays no N sequential round trips before its first record is filed. Each table is then
    # detached in turn under the claim its resolution read, so one that changed since answers 409 at its fence.
    claims = table_claims.claim_store(settings)
    resolved_all = await asyncio.gather(*(run_in_threadpool(table_claims.resolve, ns, claims, item.segments) for item in tables))
    detached: list[GovernedObject] = []
    for item, resolved in zip(tables, resolved_all, strict=True):
        trashed = await _trash_listed_table(
            ns,
            settings,
            item,
            resolved=resolved,
            claims=claims,
            namespace_uuid=parent_of(item.segments),
            cascade=cascade,
            dropped_by=dropped_by,
            grace_days=grace_days,
        )
        if trashed:
            detached.append(item)
    for child in [*child_namespaces, segments]:
        # Read before the drop removes the `__manifest` row that is their only copy.
        described = await run_in_threadpool(native.call, ns, "describe_namespace", DescribeNamespaceRequest(id=child))
        # The backend drops by name, so the namespace the walk listed is asked for in what it describes, the last read
        # before the drop: one created under the name since would be dropped, and filed under the listed one's uuid.
        object_uuids.require_standing_as_identified(child, identified=uuid_at[tuple(child)], standing=uuid_of(described.properties))
        await run_in_threadpool(native.call, ns, "drop_namespace", DropNamespaceRequest(id=child))
        record = trash.make_record(
            fga_ids.canonical_object_id(child),
            uuid=uuid_at[tuple(child)],
            location="",
            dropped_by=dropped_by,
            grace_days=grace_days,
            kind="namespace",
            namespace_uuid=parent_of(child),
            cascade=cascade,
            properties=described.properties,
        )
        await run_in_threadpool(trash.put, settings.registry_root, so, record)
    return detached


async def _trash_listed_table(
    ns: LanceNamespace,
    settings: CatalogStore,
    item: GovernedObject,
    *,
    resolved: table_claims.ResolvedTable,
    claims: location_claims.ClaimStore,
    namespace_uuid: str | None,
    cascade: str,
    dropped_by: str | None,
    grace_days: float,
) -> bool:
    """Detach one table a recoverable cascade listed and file it in the trash, under the fence the drop door holds its
    table by; whether this cascade detached it.

    ``resolved`` is what the table's name resolved to once the cascade had listed it, and its claim is fenced under
    that reading (:func:`table_claims.fence_listed`, [[LH-586]]), so a rename landing while the table goes answers 409
    rather than leaving a trash record on a table that lives on under another name. ``False`` for a table that left
    the namespace since the listing: no record is filed for it.

    RECORD FIRST, DETACH SECOND, as at the table door (diff2 F6 leg a): a crash between the two leaves a record on a
    live table, which the purge refuses ``STILL_REGISTERED`` and a retry overwrites, where the other order left bytes
    no undrop, purge or retry could reach. The trashed table keeps its location's claim until its undrop or the purge
    ([[LH-204]]); only the fence is lifted, whether the detach landed or failed.

    Raises:
        ConcurrentModificationError: The table changed, or another request is acting on it; nothing of it changed here.
        TableNotFoundError: The row went between the fence and the detach, by a hand the fence does not bind. Unlike the
            destroying cascade, this one has already filed its record, so it answers rather than skip the table: the
            record stays, and the call issued again lists only what still stands.
    """
    fenced = await run_in_threadpool(partial(table_claims.fence_listed, ns, claims, resolved, listed_uuid=item.uuid, verb="drop"))
    if fenced is None:
        return False
    record = trash.make_record(
        fga_ids.canonical_object_id(item.segments),
        uuid=item.uuid,
        location=fenced.location,
        dropped_by=dropped_by,
        grace_days=grace_days,
        namespace_uuid=namespace_uuid,
        cascade=cascade,
    )
    try:
        await run_in_threadpool(trash.put, settings.registry_root, settings.storage_options(), record)
        await run_in_threadpool(native.call, ns, "deregister_table", DeregisterTableRequest(id=item.segments))
    finally:
        await run_in_threadpool(table_claims.lift_fence, claims, fenced)
    return True


async def destroy_subtree(
    ns: LanceNamespace,
    segments: list[str],
    descendants: list[GovernedObject],
    *,
    root_uuid: str,
    registry: base_registry.BaseRegistry,
    claims: location_claims.ClaimStore,
    storage_options: StorageOptions,
    estate_roots: tuple[str, ...],
    dropped_by: str | None = None,
) -> AsyncIterator[list[str]]:
    """Destroy the subtree BOTTOM-UP, ourselves (#117), yielding each table as it goes.

    `drop_namespace(behavior=Cascade)` is not implemented by the `dir` backend the chart runs — it
    answers `NamespaceNotEmpty` for EVERY casing (probed directly against the library). Left to the backend, a
    non-empty namespace cannot be dropped at all, forced or not, and `purge=true`, the destroy-now opt-out, does
    nothing; so the destroying cascade is walked here.

    The walk is the same shape as `trash_subtree`'s, minus the trash records: tables first (each one's bytes are
    deleted at the location it is described at, by `tables.destroy.destroy_table`, [[LH-532]] — this is the destructive
    path by definition), then namespaces
    deepest-first so each is empty when its own drop runs, then the cascaded root. Not atomic, and
    deliberately so: a mid-loop failure leaves a SMALLER subtree that the same call can finish on a
    retry, which is strictly better than the all-or-nothing native call it replaces (that one simply
    refused). An already-absent child is tolerated: drift, not an error.

    Each destroyed table's base record goes with its bytes ([[LH-279]]): left behind, it would sanction its bases for
    the next table registered there. Its location claim goes too ([[LH-204]]), or the location stays held for the
    claim's lease.

    Each table is yielded once this cascade has removed it, so the caller announces it before the next one goes
    ([[LH-495]]): the retry lists only what still stands, so a table the first attempt destroyed is announced then or
    not at all. A listed table that left before its turn (:func:`_destroy_listed_table`) is yielded by no one: its own
    drop announced and ended it, or a rename carried it out alive, and the caller ends only what was yielded.

    A table's residue record, filed while its bytes are deleted, names its uuid and its parent namespace's: the root's,
    ``root_uuid``, or the descendant namespace's it sits in.
    """
    uuid_at = {tuple(item.segments): item.uuid for item in descendants} | {tuple(segments): root_uuid}
    tables = [item for item in descendants if item.kind == "table"]
    child_namespaces = sorted((item.segments for item in descendants if item.kind == "namespace"), key=len, reverse=True)
    # SEQUENTIAL, DELIBERATELY (catalog-api-11 asked about this loop and the answer is no). Every call
    # here DESTROYS bytes, and the ordered partial progress is the property the docstring above trades
    # for: a mid-loop failure must leave a strictly SMALLER subtree that the same call finishes on a
    # retry, and the namespace drops must run deepest-first so each is empty when its own drop lands.
    # Concurrency buys latency on a path nobody is waiting on and costs both of those.
    #
    # [[LH-204]] BEFORE ANYTHING IS DESTROYED, every table's location is checked against its claim: a
    # location another live table holds refuses the whole cascade, as a protected descendant does. So is every table a
    # registered clone outside the subtree reads (ADR 0198 §1), and the clones inside it go before their sources.
    located: dict[tuple[str, ...], str] = {}
    for item in tables:
        with suppress(TableNotFoundError):
            doomed: DescribeTableResponse = await run_in_threadpool(native.call, ns, "describe_table", DescribeTableRequest(id=item.segments))
            if doomed.location:
                await run_in_threadpool(table_claims.require_no_other_holder, ns, claims, doomed.location, table=fga_ids.canonical_object_id(item.segments))
                located[tuple(item.segments)] = doomed.location
    carried = {
        key: await run_in_threadpool(partial(destroy.clone_readers, location, storage_options, table=fga_ids.canonical_object_id(list(key))))
        for key, location in located.items()
    }
    try:
        order = clone_sources.destroy_order({key: clone_sources.tag_of(location) for key, location in located.items()}, carried)
    except clone_sources.ReadFromOutsideError as exc:
        log.warning(
            "cascade_refused_clone_sources",
            extra={"namespace": segments, "read": {fga_ids.canonical_object_id(list(k)): sorted(v) for k, v in exc.read.items()}},
        )
        raise InvalidTableStateError(
            f"namespace {fga_ids.canonical_object_id(segments)} cannot be destroyed: {len(exc.read)} table(s) in it are read by registered clones outside "
            "it; drop or deregister those clones first, or drop the namespace recoverably"
        ) from exc
    tables = sorted(tables, key=lambda item: order.index(tuple(item.segments)) if tuple(item.segments) in located else len(order))
    for item in tables:
        removed = await _destroy_listed_table(
            ns,
            item,
            namespace_uuid=uuid_at.get(tuple(item.segments[:-1])),
            registry=registry,
            claims=claims,
            storage_options=storage_options,
            estate_roots=estate_roots,
            dropped_by=dropped_by,
        )
        if removed:
            yield item.segments
    for child in child_namespaces:
        with suppress(NamespaceNotFoundError):
            await run_in_threadpool(native.call, ns, "drop_namespace", DropNamespaceRequest(id=child))
    # The root by name, once the name is asked to hold it still: the walk took the time a drop and a create need. A root
    # already gone is the drift the drops above tolerate; one another namespace replaced is refused.
    standing = await run_in_threadpool(object_uuids.resolve_namespace_uuid, ns, segments)
    if standing is not None:
        object_uuids.require_standing_as_identified(segments, identified=root_uuid, standing=standing)
    with suppress(NamespaceNotFoundError):
        await run_in_threadpool(native.call, ns, "drop_namespace", DropNamespaceRequest(id=segments))


async def _destroy_listed_table(
    ns: LanceNamespace,
    item: GovernedObject,
    *,
    namespace_uuid: str | None,
    registry: base_registry.BaseRegistry,
    claims: location_claims.ClaimStore,
    storage_options: StorageOptions,
    estate_roots: tuple[str, ...],
    dropped_by: str | None,
) -> bool:
    """Destroy one table a cascade listed, under the fence the drop door holds its table by; whether this cascade removed it.

    The table's name is resolved again as its turn comes and its claim fenced (:func:`table_claims.fence_listed`,
    [[LH-586]]), so the bytes deleted are those of the row removed ([[LH-532]]) and a rename landing while the table
    goes answers 409 rather than carrying it out of a cascade that then ends its grants. ``False`` for a table that
    left the namespace since the listing: it is neither destroyed, announced nor ended here.

    A clone's ending is filed once every judgement has passed, right before its row goes (ADR 0198 §1), so a refusal
    files none: a tag release failing afterwards is finished by maintenance's audit, where a re-issued cascade would no
    longer list the clone it belongs to. One filed before a failure stays, for the finish to judge.

    Raises:
        ConcurrentModificationError: The table changed, or another request is acting on it; nothing of it changed here.
        TableBytesLeftError: Its row is gone and bytes remain, so its claim stays held and fenced, as at the drop door.
    """
    child = item.segments
    canonical = fga_ids.canonical_object_id(child)
    resolved = await run_in_threadpool(table_claims.resolve, ns, claims, child)
    fenced = await run_in_threadpool(partial(table_claims.fence_listed, ns, claims, resolved, listed_uuid=item.uuid, verb="drop"))
    if fenced is None:
        return False
    filed: list[CloneEnding] = []

    def _file_the_ending() -> None:
        found = clone_endings.file_ending(registry, claims, fenced.location, table=canonical, forget_record=True, door="namespace_cascade", filing=fenced.token)
        if found is not None:
            filed.append(found)

    try:
        await run_in_threadpool(
            partial(
                destroy.destroy_table,
                ns,
                storage_options,
                child,
                location=fenced.location,
                estate_roots=estate_roots,
                table_uuid=item.uuid,
                namespace_uuid=namespace_uuid,
                control=claims,
                dropped_by=dropped_by,
                before_removal=_file_the_ending,
            )
        )
    except destroy.TableBytesLeftError:
        raise
    except TableNotFoundError:
        # The row went by a hand the fence does not bind, so the table is not this cascade's to announce or end. An
        # ending filed before it stays, for the finish to judge; the fence is lifted, as the drop door lifts its own.
        await run_in_threadpool(table_claims.lift_fence, claims, fenced)
        return False
    except Exception:
        # The row stands, so the fence is lifted for the re-issue that finishes the cascade.
        await run_in_threadpool(table_claims.lift_fence, claims, fenced)
        raise
    # The base record goes with the bytes ([[LH-279]]) and the location's claim with the row ([[LH-204]]); the clone's
    # ending last. The claim is released under the uuid this cascade listed the table by, as well as under its fence: a
    # table's bytes can take longer to go than a fence stands, and the same name registered at the location once the
    # fence has lapsed holds the claim under a uuid of its own.
    await run_in_threadpool(partial(base_registry.forget_base_record, registry, fenced.location, uuid=item.uuid))
    await run_in_threadpool(partial(location_claims.release, claims, fenced.location, canonical, uuid=item.uuid))
    if filed:
        await run_in_threadpool(clone_endings.clear_ending, claims, filed[0])
    return True
