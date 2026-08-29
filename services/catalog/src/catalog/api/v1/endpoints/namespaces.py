"""Namespace metadata endpoints."""

from __future__ import annotations

import logging
from contextlib import suppress

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    CreateNamespaceRequest,
    CreateNamespaceResponse,
    DeregisterTableRequest,
    DescribeNamespaceRequest,
    DescribeNamespaceResponse,
    DescribeTableRequest,
    DescribeTableResponse,
    DropNamespaceRequest,
    DropNamespaceResponse,
    DropTableRequest,
    LanceNamespace,
    ListNamespacesRequest,
    ListNamespacesResponse,
    ListTablesRequest,
    ListTablesResponse,
    NamespaceExistsRequest,
    NamespaceNotFoundError,
    PermissionDeniedError,
    RegisterTableRequest,
    ServiceUnavailableError,
    TableAlreadyExistsError,
    TableNotFoundError,
)

from catalog.api import fga_deps
from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, NamespaceDep, SettingsDep, namespace_for_root
from catalog.api.pagination import paginate
from catalog.api.security import CurrentToken
from catalog.core.config import Settings
from catalog.core.formats import reject_unsupported_format

# `MAX_NAMESPACE_DEPTH` is IMPORTED, not redeclared: the point of F10 item 10 is that two walkers
# over the same tree disagreed about how deep it may go, and a second copy of the number would let
# them drift apart again the moment one is tuned.
from catalog.core.identifiers import MAX_NAMESPACE_DEPTH, parse_identifier, reconcile_body_id, require_safe_segments
from catalog.schemas import ProtectionResponse, SetProtectionRequest, TrashEntry
from catalog.services import native, warehouses
from service_kit.control_emit import emit_control
from service_kit.governed import fga
from service_kit.lakehouse import maintenance_policies, protection, trash


log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/namespace", tags=["namespace"])

# Safety ceiling on the pagination loops in _collect_descendants — a runaway/looping backend token can't
# spin forever. Far above any real namespace fan-out.
_MAX_LIST_PAGES = 1000


def _collect_descendants(ns: LanceNamespace, segments: list[str]) -> list[tuple[str, list[str]]]:
    """Every ``(resource, segments)`` under ``segments`` — child tables AND nested namespaces, recursively.

    Enumerated BEFORE a Cascade drop removes them (afterwards they can't be listed), so the caller can
    revoke their FGA tuples once the drop commits. ``include_declared`` catches declared-only tables (they
    hold an owner grant too). Blocking native list calls → the caller runs this in a threadpool.

    DEPTH-CAPPED at the same bound the listing walk uses (diff2 F10 item 10). This recursed without one
    while `tables.py::_collect_tables` capped at ``MAX_NAMESPACE_DEPTH``, so the identical pathological
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
        # fga.read_object_tuples' openfga_read_truncated warning. In practice unreachable at this ceiling.
        log.warning("namespace_list_truncated", extra={"namespace": segments, "kind": "tables"})
    token = None
    for _ in range(_MAX_LIST_PAGES):
        children: ListNamespacesResponse = native.call(ns, "list_namespaces", ListNamespacesRequest(id=segments, page_token=token))
        for name in children.namespaces or []:
            child = [*segments, name]
            found.append(("namespace", child))
            found.extend(_collect_descendants(ns, child))  # recurse into the nested namespace
        token = children.page_token or None
        if not token:
            break
    else:
        log.warning("namespace_list_truncated", extra={"namespace": segments, "kind": "namespaces"})
    return found


@router.post("/{id}/create", response_model_exclude_none=True)
async def create_namespace(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
    body: CreateNamespaceRequest | None = None,
) -> CreateNamespaceResponse:
    """Create a namespace via ``create_namespace``, then seed its FGA owner + parent edge."""
    # LANCE-ONLY (2026-08-15 ruling). A namespace carries no bytes, but its `properties` map is
    # where a client sets a DEFAULT format for the tables created under it — so accepting one here
    # would let the ruling be bypassed one level up from the door that enforces it.
    reject_unsupported_format(body.properties if body else None)
    segments = parse_identifier(id, settings.delimiter)
    # A wildcard (`*`/`?`) in a segment would widen the vended STS policy to sibling objects once a table
    # lands under this namespace — refused at SHAPE, before the namespace is created.
    require_safe_segments(segments, delimiter=settings.delimiter)
    # A top-level namespace needs a warehouse to live in. This door cannot name one, so it is refused
    # here and the caller is sent to the warehouse-scoped route; checked BEFORE the native create, so a
    # refusal leaves nothing half-made. Nested namespaces inherit their parent's binding and pass.
    fga_deps.require_warehouse_scoped(segments, delimiter=settings.delimiter, warehouses_enabled=settings.warehouses_enabled)
    # And it must not nest deeper than the authz model can resolve. Both read walkers have capped depth
    # for a while; nothing capped CREATION, so the estate could be driven into a shape where
    # Check(can_get_metadata, warehouse:X) errors instead of answering — taking browsing down for the
    # whole bucket, its owners included. Shape rung, so 400, and before the native call.
    fga_deps.require_namespace_depth(segments, delimiter=settings.delimiter)
    # …and the id must not still belong to a namespace in the trash. F10 item 4 closed this on the TABLE
    # doors and left both namespace doors open, though the guard already spoke `kind="namespace"`.
    # A recoverable drop KEEPS the object's tuples on purpose (the owner is the one caller who needs them
    # to undrop), so a create at the same id during the grace window wears the dead namespace's readers,
    # writers and validators. For a namespace it is worse than the table case in a way worth naming: the
    # #96 cascade trashes a whole SUBTREE, and `undrop` rebuilds it with `mode="exist_ok"` and then
    # re-registers the trashed TABLES underneath — so the previous tenant's data lands inside the
    # namespace the new owner now controls. Conflict rung (409), after authz, before the native write.
    await fga_deps.require_no_live_trash(settings, segments, kind="namespace")
    req = body or CreateNamespaceRequest()
    req.id = reconcile_body_id(segments, req.id)
    response: CreateNamespaceResponse = await run_in_threadpool(native.call, ns, "create_namespace", req)

    async def _undo_create() -> None:
        await run_in_threadpool(native.call, ns, "drop_namespace", DropNamespaceRequest(id=segments))

    # Owner + parent edge (parent namespace if nested, else the catalog root) so the
    # concentric cascade reaches the namespace and its tables — stops a nested-namespace
    # lockout and lets a layer-level grant (medallion bronze/silver/gold) reach children.
    #
    # Compensated (diff2 F3): the namespace is EMPTY at this instant — it was created three lines up
    # and nothing can have been put in it yet — so the undo cannot destroy anyone's data. A failed
    # seed used to leave a namespace its creator could neither see nor drop, while native
    # `NamespaceAlreadyExists` refused every retry, permanently reserving the name.
    await fga_deps.seed_ownership_or_compensate(client, settings, token, resource="namespace", segments=segments, undo=_undo_create)
    await emit_control(
        control,
        action="namespace_created",
        object_type="namespace",
        object_id=f"namespace:{id}",
        actor=f"user:{token.sub}" if token else None,
        extra={"mode": req.mode, "properties": req.properties},
    )
    return response


@router.get("/{id}/list", response_model_exclude_none=True)
def list_namespaces(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    page_token: str | None = None,
    limit: int | None = None,
) -> ListNamespacesResponse:
    """List the child namespaces under ``id`` via ``list_namespaces`` (page_token/limit paged)."""
    req = ListNamespacesRequest(id=parse_identifier(id, settings.delimiter), page_token=page_token, limit=limit)
    return native.call(ns, "list_namespaces", req)


@router.post("/{id}/describe", response_model_exclude_none=True)
def describe_namespace(id: str, ns: NamespaceDep, settings: SettingsDep) -> DescribeNamespaceResponse:
    """Return the metadata/properties of namespace ``id`` via ``describe_namespace``."""
    req = DescribeNamespaceRequest(id=parse_identifier(id, settings.delimiter))
    return native.call(ns, "describe_namespace", req)


async def _trash_subtree(ns: LanceNamespace, settings: Settings, token: CurrentToken, segments: list[str], descendants: list[tuple[str, list[str]]]) -> None:
    """DETACH the subtree instead of destroying it — the recoverable half of a cascade (#96).

    A trash record pointing at bytes the native cascade already deleted would be a LIE: undrop
    re-registers still-present bytes, and `drop_namespace(cascade)` destroys its children inside one
    native call. So with a grace period the cascade never reaches that call. Instead, exactly what
    the single-table drop door does, per child: every descendant table is DEREGISTERED (pointer
    detached, bytes untouched) and filed in the trash; every namespace — children deepest-first,
    then the cascaded root — is then empty and dropped plainly, filed as a ``kind="namespace"``
    record so undrop knows which manifest rows to rebuild. All records share one drop-time stamp
    rule: ``expires_at`` is data on the record, never policy read at expiry.

    Not atomic, deliberately: a mid-loop failure leaves every already-detached child RECOVERABLE
    (its record is filed and its parent namespace still exists), which is strictly safer than the
    all-or-nothing native destroy it replaces.
    """
    so = settings.storage_options()
    dropped_by = f"user:{token.sub}" if token is not None else None
    tables = [child for resource, child in descendants if resource == "table"]
    # Deepest-first, so every namespace is empty by the time its own drop runs.
    child_namespaces = sorted((child for resource, child in descendants if resource == "namespace"), key=len, reverse=True)
    for child in tables:
        described: DescribeTableResponse = await run_in_threadpool(native.call, ns, "describe_table", DescribeTableRequest(id=child))
        # A declared-only table has no location; its record carries "" and undrop skips it with a
        # warning — there were no bytes to lose, only a declaration the caller can redo.
        record = trash.make_record(
            fga.canonical_object_id(child, delimiter=settings.delimiter),
            location=described.location or "",
            dropped_by=dropped_by,
            grace_days=settings.trash_grace_days,
        )
        # ORDER SPLIT ON WHETHER THE RECORD OWNS BYTES (diff2 F6 leg a), matching the single-table
        # door. A byte-owning record goes FIRST: a crash between the deregister and the write used to
        # leave the bytes unreachable by undrop, by the purge and by any retry (`describe_table` 404s
        # once detached), and filing first turns that into a record on a live table, which the purge
        # refuses `STILL_REGISTERED` and the retry overwrites.
        #
        # A byte-LESS record keeps the old order, and that is not laziness. The purge's estate test is
        # skipped for records with no location, so a record filed on a still-LIVE object inside a
        # deactivated warehouse would be revoked and cleared — trading a recoverable loss for an
        # unrecoverable one. Here the window costs only a declaration the caller can redo.
        if described.location:
            await run_in_threadpool(trash.put, settings.registry_root, so, record)
            await run_in_threadpool(native.call, ns, "deregister_table", DeregisterTableRequest(id=child))
        else:
            await run_in_threadpool(native.call, ns, "deregister_table", DeregisterTableRequest(id=child))
            await run_in_threadpool(trash.put, settings.registry_root, so, record)
    # The ROOT's binding, read BEFORE anything is dropped — after the unbind below it is unreadable,
    # and after the purge it is gone for good. `None` for a nested namespace (bindings are keyed by
    # top-level segment) and for an unbound one (single-bucket estates have no binding at all).
    binding: dict[str, str] | None = None
    if len(segments) == 1:
        live = await run_in_threadpool(warehouses.binding_for_namespace, settings.registry_root, so, segments[0])
        if live:
            binding = {"warehouse_id": str(live["warehouse_id"]), "root_uri": str(live["root_uri"])}
    for child in [*child_namespaces, segments]:
        await run_in_threadpool(native.call, ns, "drop_namespace", DropNamespaceRequest(id=child))
        record = trash.make_record(
            fga.canonical_object_id(child, delimiter=settings.delimiter),
            location="",
            dropped_by=dropped_by,
            grace_days=settings.trash_grace_days,
            kind="namespace",
            # Only the ROOT carries it — `child is segments` is identity on the same list object, and
            # the root is the last iteration by construction.
            binding=binding if child is segments else None,
        )
        await run_in_threadpool(trash.put, settings.registry_root, so, record)
    # UNBIND LAST, and only once the root's record — carrying the binding — is durably written
    # (diff2 F6 leg c). Order is the whole safety argument:
    #
    #   * before the loop, a mid-loop crash would leave the surviving children unroutable, because
    #     `NamespaceDep` resolves through this very binding;
    #   * after the record, a crash between the two leaves a binding whose namespace is gone — which
    #     is exactly the old leak, but now bounded by one retry instead of forever, and `undrop`
    #     re-binds to the same values so it converges either way.
    #
    # The alternative shape — keep the binding and have the PURGE remove it — was rejected: it puts a
    # registry write in the reclaimer (which owns no write path to it and may not import the
    # catalog's), and it needs a reconciler category that would fire on every legitimately-trashed
    # subtree for its whole grace window, blocking `report_is_clean` and with it the purge itself.
    if binding is not None:
        await run_in_threadpool(warehouses.unbind_namespace, settings.registry_root, so, segments[0])


async def _destroy_subtree(ns: LanceNamespace, segments: list[str], descendants: list[tuple[str, list[str]]]) -> None:
    """Destroy the subtree BOTTOM-UP, ourselves (#117).

    `drop_namespace(behavior=Cascade)` is not implemented by the `dir` backend the chart runs — it
    answers `NamespaceNotEmpty` for EVERY casing (probed directly against the library). So the
    destructive cascade this door has always documented never happened: with the shipped default
    (`LANCE_TRASH_GRACE_DAYS=0`) a non-empty namespace could not be dropped at all, forced or not,
    and `purge=true` — the documented destroy-now opt-out — was equally dead. Three guarded loops
    below it (the force-path protection clear, the descendant revoke, the descendant protection
    sweep) were unreachable code on that backend.

    The fix is the same shape as `_trash_subtree`'s, minus the trash records: tables first (each
    `drop_table` DELETES its bytes — this is the destructive path by definition), then namespaces
    deepest-first so each is empty when its own drop runs, then the cascaded root. Not atomic, and
    deliberately so: a mid-loop failure leaves a SMALLER subtree that the same call can finish on a
    retry, which is strictly better than the all-or-nothing native call it replaces (that one simply
    refused). An already-absent child is tolerated — drift, not an error, exactly as the warehouse
    cascade treats a binding that outlived its namespace.
    """
    tables = [child for resource, child in descendants if resource == "table"]
    child_namespaces = sorted((child for resource, child in descendants if resource == "namespace"), key=len, reverse=True)
    for child in tables:
        with suppress(TableNotFoundError):
            await run_in_threadpool(native.call, ns, "drop_table", DropTableRequest(id=child))
    for child in [*child_namespaces, segments]:
        with suppress(NamespaceNotFoundError):
            await run_in_threadpool(native.call, ns, "drop_namespace", DropNamespaceRequest(id=child))


async def _require_descendants_unprotected(settings: Settings, descendants: list[tuple[str, list[str]]], *, force: bool) -> None:
    """Refuse a cascade that would destroy a PROTECTED descendant, naming the first one found.

    The guard at the named rung only sees the id in the URL; a cascade's unit of destruction is the
    whole subtree, and those children are destroyed inside one native call that never reaches a door.
    So the check belongs here, where the destroyed set is actually enumerated.
    """
    if force or not descendants:
        return
    so = settings.storage_options()
    for resource, segments in descendants:
        canonical = fga.canonical_object_id(segments, delimiter=settings.delimiter)
        record = await run_in_threadpool(protection.get_protection, settings.registry_root, so, resource, canonical)
        fga_deps.require_not_protected(record or {}, kind=resource, obj_id=canonical, force=False)


@router.post("/{id}/drop", response_model_exclude_none=True)
async def drop_namespace(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
    body: DropNamespaceRequest | None = None,
    force: bool = False,
    purge: bool = False,
) -> DropNamespaceResponse:
    """Drop namespace ``id`` (``drop_namespace``); revoke its FGA tuples — and, for a Cascade drop, every
    dropped child's — so a reused id can't inherit stale grants.

    Deletion protection (#73): a ``protected`` control-root record refuses 409 unless ``force=true``.
    A CASCADE is checked against the whole SUBTREE before the native call — its children are destroyed
    inside that one call and never reach this door, so a protected table under a cascaded namespace
    would otherwise die silently. ``force`` turns the protection lock only; the FGA gate ran first.

    With a grace period configured (#96), a CASCADE becomes RECOVERABLE: the subtree is detached —
    tables deregistered, namespaces emptied then dropped — with a trash record per destroyed object,
    and ``POST /v1/namespace/{id}/undrop`` rebuilds the whole subtree. ``purge=true`` is the same
    explicit opt-out the table door has. A plain RESTRICT drop stays destructive-but-cheap: the
    namespace it removes is empty by definition, and re-creating an empty namespace needs no trash."""
    segments = parse_identifier(id, settings.delimiter)
    canonical = fga.canonical_object_id(segments, delimiter=settings.delimiter)
    guard = await run_in_threadpool(protection.get_protection, settings.registry_root, settings.storage_options(), "namespace", canonical)
    fga_deps.require_not_protected(guard or {}, kind="namespace", obj_id=canonical, force=force)
    req = body or DropNamespaceRequest()
    req.id = reconcile_body_id(segments, req.id)
    # A Cascade drop (behavior=Cascade; case-insensitive per the lance spec) removes all child tables +
    # nested namespaces from storage. Their FGA grants must be revoked too, or a later object reusing a
    # child id would inherit the stale owner/reader/writer tuples (privilege bleed). Enumerate the
    # descendants BEFORE the drop (afterwards they can't be listed); only when FGA is on (else the revoke
    # loop is a no-op and the listing is wasted work). Restrict (the dir-backend default) errors on a
    # non-empty namespace, so there are never extra tuples to revoke on that path.
    cascade = (req.behavior or "").lower() == "cascade"
    descendants: list[tuple[str, list[str]]] = []
    # Enumerated whenever a CASCADE is asked for — not only when FGA is on, as it used to be. A cascade
    # destroys children INSIDE the single native call, so they never re-enter this door: without this
    # list, a protected table under a cascade-dropped namespace died silently while the docstring
    # claimed protection covered it. The tuple-revoke below consumes the same list when FGA is on.
    if cascade:
        descendants = await run_in_threadpool(_collect_descendants, ns, segments)
        # Protection is a property of the SUBTREE, so it is checked against every id about to die, and
        # the refusal NAMES the protected descendant — "something in here is protected" is not an
        # answer anyone can act on. `force` turns this lock exactly as it does at the named rung.
        await _require_descendants_unprotected(settings, descendants, force=force)
    # #96: with a grace period, a cascade DETACHES the subtree (trash record per child) instead of
    # destroying it inside the one native call — see _trash_subtree for why the native cascade and a
    # trash record cannot coexist. `purge=true` is the explicit destroy-now, as at the table door.
    recoverable = cascade and settings.trash_grace_days > 0 and not purge
    if recoverable:
        await _trash_subtree(ns, settings, token, segments, descendants)
        response = DropNamespaceResponse()
    elif cascade:
        # The dir backend cannot cascade (#117) — we enumerate and destroy bottom-up ourselves.
        await _destroy_subtree(ns, segments, descendants)
        response = DropNamespaceResponse()
    else:
        response = await run_in_threadpool(native.call, ns, "drop_namespace", req)
    # The record dies with the object — a reused id must not inherit protection nobody set on it.
    if guard:  # only when one existed — see the table doors
        await run_in_threadpool(protection.clear_protection, settings.registry_root, settings.storage_options(), "namespace", canonical)
    # Same reuse rule for the DESCENDANTS' protection records — only reachable with force=true (an
    # unforced cascade already proved none exist, so this loop would be guaranteed-wasted deletes).
    if force:
        for resource, child_segments in descendants:
            child_id = fga.canonical_object_id(child_segments, delimiter=settings.delimiter)
            await run_in_threadpool(protection.clear_protection, settings.registry_root, settings.storage_options(), resource, child_id)
    # The MAINTENANCE POLICIES die with a DESTROYED subtree, under the same reuse rule as the protection
    # records above and the warehouse binding below — and NOT on the recoverable path, where undrop
    # rebuilds the subtree and each object must come back configured as it was.
    # The descendants are cleared unconditionally here rather than under `force`, unlike protection: an
    # unforced cascade proves no PROTECTED descendant exists, which says nothing about policies, so
    # gating on `force` would leak a policy for every table destroyed by an ordinary cascade.
    if not recoverable:
        await run_in_threadpool(maintenance_policies.delete_policy, settings.registry_root, settings.storage_options(), "namespace", canonical)
        for resource, child_segments in descendants:
            child_id = fga.canonical_object_id(child_segments, delimiter=settings.delimiter)
            await run_in_threadpool(maintenance_policies.delete_policy, settings.registry_root, settings.storage_options(), resource, child_id)
    # The warehouse BINDING dies with a destroyed top-level namespace. `unbind_namespace` existed and
    # only the warehouse-delete door called it, so every direct drop of a top-level namespace leaked
    # its binding record — and the UI derives its namespace list from bindings, so the dropped
    # namespace kept LISTING forever, indistinguishable from a live empty one (seen on the deployed
    # estate: `casc9` survived its own successful cascade). Kept on a RECOVERABLE drop, exactly as the
    # grants are: undrop rebuilds the subtree and the binding must still be there to route it.
    if not recoverable and len(segments) == 1:
        await run_in_threadpool(warehouses.unbind_namespace, settings.registry_root, settings.storage_options(), segments[0])
    # Revoke AFTER the drop commits (so a failed/restricted drop leaves the still-valid grants in place):
    # the namespace's own tuples, then every cascaded descendant's. NOT on a recoverable cascade (#96,
    # the #75 rule): the owner is the one person who needs to undrop it, and the grants die with the
    # bytes — at purge, or when the sweep's expiry reclaims the trash.
    if not recoverable:
        await fga_deps.revoke_ownership(client, settings, resource="namespace", segments=segments, token=token)
        for resource, child_segments in descendants:
            await fga_deps.revoke_ownership(client, settings, resource=resource, segments=child_segments, token=token)
    await emit_control(
        control,
        action="namespace_dropped",
        object_type="namespace",
        object_id=f"namespace:{id}",
        actor=f"user:{token.sub}" if token else None,
        extra={"cascade": cascade, "recoverable": recoverable, "descendants_revoked": 0 if recoverable else len(descendants)},
    )
    return response


@router.get("/{id}/tasks", response_model_exclude_none=True)
async def namespace_tasks(
    id: str,
    settings: SettingsDep,
    token: CurrentToken,
) -> list[TrashEntry]:
    """What is queued for THIS namespace — a pending trash expiry after a recoverable cascade (#96).
    Same contract as the table door: an undrop deadline the owner cannot see is not a safety
    feature. Reader-gated by the router (``tasks`` is a metadata read)."""
    canonical = fga.canonical_object_id(parse_identifier(id, settings.delimiter), delimiter=settings.delimiter)
    record = await run_in_threadpool(trash.get, settings.registry_root, settings.storage_options(), canonical, kind="namespace")
    if record is None:
        return []
    return [TrashEntry(**{k: str(record.get(k, "")) for k in ("id", "location", "dropped_by", "dropped_at", "expires_at")})]


@router.post("/{id}/undrop", response_model_exclude_none=True)
async def undrop_namespace(
    id: str,
    request: Request,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    control: ControlEmitterDep,
) -> CreateNamespaceResponse:
    """Recover a cascade-dropped SUBTREE from the trash (#96) — the plural undrop.

    The unit of a cascade is the subtree, so the unit of its recovery is too: every trashed
    namespace under (and including) ``id`` is re-created shallowest-first, then every trashed table
    is re-registered at its old id from its still-present bytes, and each record is cleared as its
    object recovers. Owner-gated like the table door (``undrop`` maps to the delete rung).

    Resumable, not atomic: namespace creates run ``exist_ok`` and an already-registered table is
    treated as recovered, so a rerun after a mid-recovery failure finishes the job instead of
    409-ing on what the first attempt already rebuilt. 404 when there is no trash record: an expired
    or never-trashed drop is genuinely unrecoverable, and saying so beats a 200 that recovers
    nothing. A declared-only table (empty recorded location) is skipped with a warning — there were
    no bytes to lose."""
    segments = parse_identifier(id, settings.delimiter)
    canonical = fga.canonical_object_id(segments, delimiter=settings.delimiter)
    so = settings.storage_options()
    record = await run_in_threadpool(trash.get, settings.registry_root, so, canonical, kind="namespace")
    if record is None:
        raise NamespaceNotFoundError(f"no recoverable drop for namespace: {canonical}. The grace period may have expired, or the drop purged its subtree.")
    # RE-BIND FIRST, and re-resolve the connection from the restored root (diff2 F6 leg c).
    #
    # The drop unbound this top-level namespace, so `NamespaceDep` — which resolves BEFORE this
    # handler body runs — has already fallen back to the estate's default root. Rebuilding through it
    # would re-create the whole subtree in the WRONG BUCKET: a silent tenant-isolation break, not an
    # error. So the binding is restored from the record and the connection re-derived from it.
    #
    # `bind_namespace` is write-once at the store: identical `{warehouse_id, root_uri}` is idempotent
    # (the retry path), a DIFFERENT binding raises `NamespaceAlreadyExistsError` (409). That refusal
    # is correct and deliberate — if the id was re-bound to another warehouse during the grace window,
    # routing this subtree there would hand one tenant another's bucket, and failing loudly is the
    # only safe answer. A record with no `binding` (a nested undrop, an unbound estate, or a record
    # written before this landed) keeps the dependency's connection untouched.
    bound = record.get("binding") or None
    if bound:
        warehouse_id = str(bound["warehouse_id"])
        # THE DEACTIVATION GATE, and it is not optional — `test_no_warehouse_bucket_access_bypasses_the
        # _deactivation_gate` refuses any module that reaches a warehouse bucket through
        # `namespace_for_root` without one, and it caught this exact bypass when the re-resolve first
        # landed. `get_namespace` gates every ordinary request this way; re-deriving the connection here
        # steps around that dependency, so the check has to come with it.
        #
        # Deactivation is offboarding step one, so recovering a subtree INTO a quarantined warehouse is
        # precisely what it exists to stop. Fail-closed on both failure kinds, symmetric with
        # `dependencies.py`: an unreadable registry is 503, and a MISSING warehouse record (clean
        # `None`) is not-active → 403, never silently allowed.
        try:
            status = await run_in_threadpool(warehouses.warehouse_status, settings.registry_root, so, warehouse_id)
        except Exception as exc:
            log.warning("warehouse_status_lookup_failed", extra={"top_ns": segments[0], "warehouse_id": warehouse_id, "error": str(exc)})
            raise ServiceUnavailableError(f"warehouse status lookup failed for {warehouse_id!r}") from exc
        if status != "active":
            raise PermissionDeniedError(
                f"warehouse {warehouse_id!r} is deactivated (quarantined); namespace {segments[0]!r} cannot be recovered into it until it is reactivated"
            )
        await run_in_threadpool(
            warehouses.bind_namespace,
            settings.registry_root,
            so,
            segments[0],
            warehouse_id,
            str(bound["root_uri"]),
        )
        ns = namespace_for_root(request, settings, str(bound["root_uri"]))
    everything = await run_in_threadpool(trash.list_all, settings.registry_root, so)
    prefix = canonical + settings.delimiter
    subtree = [r for r in everything if str(r.get("id")) == canonical or str(r.get("id", "")).startswith(prefix)]
    # Shallowest first — a parent manifest row must exist before its children's create/register.
    namespace_records = sorted((r for r in subtree if r.get("kind") == "namespace"), key=lambda r: str(r["id"]).count(settings.delimiter))
    table_records = [r for r in subtree if r.get("kind") == "table"]
    response = CreateNamespaceResponse()
    for n_rec in namespace_records:
        n_id = str(n_rec["id"])
        created: CreateNamespaceResponse = await run_in_threadpool(
            native.call, ns, "create_namespace", CreateNamespaceRequest(id=parse_identifier(n_id, settings.delimiter), mode="exist_ok")
        )
        if n_id == canonical:
            response = created
        # Clear only AFTER the create commits — a failed rebuild must leave the subtree recoverable.
        await run_in_threadpool(trash.clear, settings.registry_root, so, n_id, kind="namespace")
    skipped = 0
    for t_rec in table_records:
        t_id = str(t_rec["id"])
        location = str(t_rec.get("location") or "")
        if location:
            # The RELATIVE form register_table accepts — the same #75 lesson as the table undrop:
            # the dir backend refuses the absolute URI the record carries for the operator's sake.
            body = RegisterTableRequest(id=parse_identifier(t_id, settings.delimiter), location=location.rstrip("/").rsplit("/", 1)[-1])
            try:
                await run_in_threadpool(native.call, ns, "register_table", body)
            except TableAlreadyExistsError:
                log.info("undrop_table_already_registered", extra={"table": t_id})
        else:
            skipped += 1
            log.warning("undrop_skipped_declared_only_table", extra={"table": t_id})
        await run_in_threadpool(trash.clear, settings.registry_root, so, t_id)
    # NO SEED — undrop is a PURE RESTORE (owner ruling, diff2 F10 item 4b), on this door for the same
    # reason as the table door.
    #
    # The comment that stood here made the argument against itself: "the recoverable cascade kept every
    # tuple (#75's rule), so the descendants' owners are intact — seeding the ROOT records the undrop
    # actor". If every tuple survived, there is nothing to restore; the seed only ADDED whoever pressed
    # the button, on the root of a whole subtree they may have had no prior claim to.
    #
    # It also deletes this door's worst residual. The seed carried `undo=None` deliberately, because by
    # this line the loop above has re-registered N tables and cleared N records, and a compensating
    # `drop_namespace` would destroy the cascade the user just recovered in order to repair one tuple.
    # That was the right call given a seed had to happen at all — and with no seed, the door has no
    # uncompensatable step left.
    await emit_control(
        control,
        action="namespace_undropped",
        object_type="namespace",
        object_id=f"namespace:{canonical}",
        actor=f"user:{token.sub}" if token is not None else None,
        extra={"namespaces": len(namespace_records), "tables": len(table_records) - skipped, "declared_only_skipped": skipped},
    )
    return response


@router.get("/{id}/protection", response_model_exclude_none=True)
async def get_namespace_protection(id: str, settings: SettingsDep) -> ProtectionResponse:
    """Read the namespace's deletion-protection flag (#123) — the table door's read, one rung up."""
    segments = parse_identifier(id, settings.delimiter)
    canonical = fga.canonical_object_id(segments, delimiter=settings.delimiter)
    record = await run_in_threadpool(protection.get_protection, settings.registry_root, settings.storage_options(), "namespace", canonical)
    return ProtectionResponse(id=canonical, protected=bool(record), set_by=(record or {}).get("set_by"))


@router.post("/{id}/protection", response_model_exclude_none=True)
async def set_namespace_protection(
    id: str,
    body: SetProtectionRequest,
    settings: SettingsDep,
    token: CurrentToken,
    control: ControlEmitterDep,
) -> ProtectionResponse:
    """Set or clear deletion protection on namespace ``id`` (#73). Owner-gated by the router
    (``protection`` maps to ``can_delete`` — whoever may delete the namespace decides whether
    deleting it needs a second thought). Same control-root record contract as the table door."""
    segments = parse_identifier(id, settings.delimiter)
    canonical = fga.canonical_object_id(segments, delimiter=settings.delimiter)
    so = settings.storage_options()
    if body.protected:
        record = {
            "kind": "namespace",
            "id": canonical,
            "protected": "true",
            "set_by": f"user:{token.sub}" if token is not None else "anonymous",
        }
        await run_in_threadpool(protection.set_protection, settings.registry_root, so, record)
    else:
        await run_in_threadpool(protection.clear_protection, settings.registry_root, so, "namespace", canonical)
    await emit_control(
        control,
        action="namespace_protected" if body.protected else "namespace_unprotected",
        object_type="namespace",
        object_id=f"namespace:{canonical}",
        actor=f"user:{token.sub}" if token is not None else None,
        extra={},
    )
    return ProtectionResponse(id=canonical, protected=body.protected)


@router.post("/{id}/exists", status_code=200)
def namespace_exists(id: str, ns: NamespaceDep, settings: SettingsDep) -> None:
    """Check that namespace ``id`` exists via ``namespace_exists`` — 200 on success (spec 0.9), else error."""
    req = NamespaceExistsRequest(id=parse_identifier(id, settings.delimiter))
    native.call(ns, "namespace_exists", req)


@router.get("/{id}/table/list", response_model_exclude_none=True)
async def list_tables(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    page_token: str | None = None,
    limit: int | None = None,
    include_declared: bool = True,
) -> ListTablesResponse:
    """List the tables under namespace ``id``; when FGA is on, filtered to what the caller can
    ``can_read_data``. ``include_declared=false`` drops declared-only tables (reserved, no storage yet).

    **WHY THIS FILTERS AT ALL — the route's gate stopped implying its contents.** The router mounts
    every endpoint here under ``authorize``, which resolves this route's ``list`` action to
    ``can_get_metadata``; C1 (upward visibility) then redefined that on a namespace as
    ``reader or can_get_metadata from child``. The widening is correct for what it was for — without
    it a grantee could not resolve the breadcrumb to their own table and every list above it 403'd —
    but it means holding ``reader`` on ONE table opens this route, and the route used to answer with
    every SIBLING table's name. Measured against both compiled models: before ``14a84022`` the check
    was ``false`` (403); after it, ``true`` (200, full listing). A table list is not a harmless header;
    ``viewer.api.security`` states the estate's position outright — "A corpus LIST is itself sensitive:
    it names data someone may not know exists."

    So the split ``open_notifications.md`` §3.1 already settled applies verbatim: the ROUTE may open on
    ``can_get_metadata``, each ITEM is checked on the object itself. ``list_all_tables`` (``tables.py``)
    has always done this; that this route did not was the inconsistency, not the policy.

    **Pagination moved off the native call, for the reason it moved there too (#141).** A backend
    ``limit`` truncates BEFORE the filter, so ``limit=10`` could answer 2 and hand out a cursor that
    skips everything the filter removed — pages that silently drop tables the caller CAN read. The
    native call is therefore unpaginated and the keyset cursor is applied to the filtered list. The
    list is sorted and deduped first because the cursor is the last NAME of the previous page and is
    stable only over an ordered one.
    """
    segments = parse_identifier(id, settings.delimiter)
    req = ListTablesRequest(id=segments, include_declared=include_declared)
    response: ListTablesResponse = await run_in_threadpool(native.call, ns, "list_tables", req)
    names = sorted(set(response.tables or []))
    # When FGA is on and the caller is known, keep only the tables they may actually read. The object
    # id is the table's PATH under this namespace, built with `canonical_object_id` — the same joiner
    # the grant path uses, so a check here cannot address an id a grant never wrote.
    if settings.fga_enabled and token is not None and client is not None:
        # `.objects` + `.truncated`: past OpenFGA's 1000-object cap this listing is SHORT, and the
        # page cursor below would be minted from the shortened list. Surfaced to the caller rather
        # than only logged — a client cannot otherwise tell a small estate from a truncated answer.
        listing = await fga.list_objects(client, user=token.sub, relation="can_read_data", object_type="table")
        allowed = set(listing.objects)
        authorization_truncated = listing.truncated
        names = [name for name in names if f"table:{fga.canonical_object_id([*segments, name], delimiter=settings.delimiter)}" in allowed]
        # THE SPEC'S OWN EXTENSION POINT. `ListTablesResponse` is a `lance_namespace` model with fields
        # `context`, `tables`, `page_token`, and `context` is documented as "arbitrary context as
        # key-value pairs ... custom to the specific implementation". So the flag rides there rather
        # than as a new body field, which would have broken the spec conformance the catalog is built on.
        if authorization_truncated:
            response.context = {**(response.context or {}), "authorization_truncated": "true"}
    response.tables, response.page_token = paginate(names, page_token, limit)
    return response
