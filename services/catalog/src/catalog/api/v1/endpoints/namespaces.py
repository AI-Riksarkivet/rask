"""Namespace metadata endpoints."""

from __future__ import annotations

import logging
from contextlib import suppress

from fastapi import APIRouter
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
    RegisterTableRequest,
    TableAlreadyExistsError,
    TableNotFoundError,
)

from catalog.api import fga_deps
from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, NamespaceDep, SettingsDep
from catalog.api.security import CurrentToken
from catalog.core.config import Settings
from catalog.core.control_emit import emit_control
from catalog.core.identifiers import parse_identifier, reconcile_body_id
from catalog.schemas import ProtectionResponse, SetProtectionRequest, TrashEntry
from catalog.services import native, warehouses
from service_kit.governed import fga
from service_kit.lakehouse import protection, trash


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
    """
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
    segments = parse_identifier(id, settings.delimiter)
    # A top-level namespace needs a warehouse to live in. This door cannot name one, so it is refused
    # here and the caller is sent to the warehouse-scoped route; checked BEFORE the native create, so a
    # refusal leaves nothing half-made. Nested namespaces inherit their parent's binding and pass.
    fga_deps.require_warehouse_scoped(segments, delimiter=settings.delimiter, warehouses_enabled=settings.warehouses_enabled)
    req = body or CreateNamespaceRequest()
    req.id = reconcile_body_id(segments, req.id)
    response: CreateNamespaceResponse = await run_in_threadpool(native.call, ns, "create_namespace", req)
    # Owner + parent edge (parent namespace if nested, else the catalog root) so the
    # concentric cascade reaches the namespace and its tables — stops a nested-namespace
    # lockout and lets a layer-level grant (medallion bronze/silver/gold) reach children.
    await fga_deps.seed_ownership(client, settings, token, resource="namespace", segments=segments)
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
        await run_in_threadpool(native.call, ns, "deregister_table", DeregisterTableRequest(id=child))
        # A declared-only table has no location; its record carries "" and undrop skips it with a
        # warning — there were no bytes to lose, only a declaration the caller can redo.
        record = trash.make_record(
            fga.canonical_object_id(child, delimiter=settings.delimiter),
            location=described.location or "",
            dropped_by=dropped_by,
            grace_days=settings.trash_grace_days,
        )
        await run_in_threadpool(trash.put, settings.registry_root, so, record)
    for child in [*child_namespaces, segments]:
        await run_in_threadpool(native.call, ns, "drop_namespace", DropNamespaceRequest(id=child))
        record = trash.make_record(
            fga.canonical_object_id(child, delimiter=settings.delimiter),
            location="",
            dropped_by=dropped_by,
            grace_days=settings.trash_grace_days,
            kind="namespace",
        )
        await run_in_threadpool(trash.put, settings.registry_root, so, record)


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
    # The recoverable cascade kept every tuple (#75's rule), so the descendants' owners are intact;
    # seeding the ROOT records the undrop actor the same way the table undrop does.
    await fga_deps.seed_ownership(client, settings, token, resource="namespace", segments=segments)
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
def list_tables(
    id: str,
    ns: NamespaceDep,
    settings: SettingsDep,
    page_token: str | None = None,
    limit: int | None = None,
    include_declared: bool = True,
) -> ListTablesResponse:
    """List the tables under namespace ``id`` via ``list_tables`` (page_token/limit paged);
    ``include_declared=false`` drops declared-only tables (reserved, no storage yet)."""
    req = ListTablesRequest(
        id=parse_identifier(id, settings.delimiter),
        page_token=page_token,
        limit=limit,
        include_declared=include_declared,
    )
    return native.call(ns, "list_tables", req)
