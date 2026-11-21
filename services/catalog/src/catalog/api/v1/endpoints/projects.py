"""``/management/v1/projects`` — first-class tenants: an explicit registry record, created in ONE operation with its tuples.

A *project* is the FGA model's tenant type (``lakehouse/governed/auth/model.fga``): warehouses hang off it
via their ``project`` field, and every grant cascades down from it. A project EXISTS when its registry
record does (``catalog/services/projects.py`` — ``_projects/<id>.json`` on the control root, no DB), and
``POST /management/v1/projects`` writes that record AND the creator's ``admin`` tuple in one operation, so existence
and permission cannot drift (`open_hierarchy_lifecycle.md`, Decision 1 — the live estate once held three
tenants in authz the catalog had never heard of, seeded FGA-only).

Creation used to be IMPLICIT via warehouse-create, with an estate-admin bootstrap exception for the first
warehouse of a not-yet-existing project. That door is gone: the estate admin creates the PROJECT here,
then that project's admins create warehouses (``require_project_exists`` refuses a warehouse for an
unknown tenant with a 404 that names this route). One way to make a tenant.

The LISTING is the union of the registry and the warehouse-derived view: registry records define the
tenants (a fresh project legitimately has zero warehouses), while a warehouse claiming a project with no
record still surfaces — that is pre-registry legacy the reconciler reports, and hiding it would make the
drift invisible instead of fixable.

**The list is the caller's projects.** The estate admin (``can_list_all_projects`` on the fixed root object,
``settings.fga_root_object``) lists every tenant; anyone else lists the projects they are a ``member`` of
(the model folds ``admin`` into ``member``), batch-checked over the tenant map
above, so a membership FGA holds for a project no registry knows lists nothing. Authorization scope ==
data scope (the ``/management/v1/events`` #12 rationale): a member sees their own tenants' names,
buckets and admins and no one else's. One tenant's detail is read by the callers the list shows it to, the
estate admin or a member of that tenant, and anyone else is answered the missing tenant's 404
(:func:`get_project`); create asks ``can_create_project`` on the root through ``door_gates.require_relation``.
``/management/v1/projects`` matches no resource prefix in
``route_guard.authorize``, so the router-wide gate only sets the 401/503 floor; every decision here is
audited (allow/deny/outage, #41), and an OpenFGA outage fails the list closed with 503.

**DELETE is gated differently, on purpose: ``project:<id>#can_administer``.** Retiring a tenant is an act
INSIDE it, so the tenant's own admins must be able to do it without holding estate-wide privilege — and an
estate OBSERVER, whose bar is "may watch the whole estate", must not be able to delete a tenant they do not
administer. The delete itself is bottom-up (`open_hierarchy_lifecycle.md` Decision 3): it refuses 409 while
the project still holds warehouses, naming them, and carries no ``cascade`` at all (see ``delete_project``).
Its deletion protection is set and cleared on the same authority, ``POST /management/v1/projects/{id}/protection``
([[LH-549]]), decided through :func:`administered_project`, which answers a caller who may not administer the tenant
exactly as it answers a missing one; the delete decides through :func:`project_to_delete`, which also lets the caller
who started a delete finish it ([[LH-576]]).

``admins`` comes from OpenFGA ``list_users`` (the #51 access-review primitive) on
``project:<id>#can_administer`` — EFFECTIVE admins (role assignees, nested roles, the cascade), not raw
tuples. It degrades to ``[]`` when FGA is off or unavailable (the registry-derived facts still serve;
an authz-lookup outage must not 500 the tenant list — the gate above already failed closed for callers).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    ConcurrentModificationError,
    InvalidInputError,
    NamespaceNotEmptyError,
    PermissionDeniedError,
    ServiceUnavailableError,
    TableNotFoundError,
)
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel, ConfigDict, Field, SkipValidation

from catalog.api import announcing
from catalog.api import idempotency as idem
from catalog.api.announcing import Announcer, AnnouncerDep
from catalog.api.authz import decisions, door_gates, hierarchy, object_grants
from catalog.api.authz.context import AuthzContextDep
from catalog.api.declared import Author, author_of, refuse_what_git_declares
from catalog.api.dependencies import ControlEmitterDep, SettingsDep
from catalog.api.security import ANONYMOUS_SUBJECT, CurrentToken, current_subject
from catalog.core.config import CatalogStore, Settings
from catalog.schemas import (
    CreateProjectRequest,
    DeleteProjectResponse,
    ProjectResponse,
    ProjectWarehouse,
    ProtectionResponse,
    SetProtectionRequest,
)
from catalog.services import object_labels, role_grants, roles, warehouses
from catalog.services import projects as project_registry
from catalog.services.announcement_intents import Announcement, ControlMessage, RecordGone, RecordHeld
from catalog.services.control_records import GIT, ProtectionChange
from lakehouse.events.control_emit import ControlEmitter, emit_control
from lakehouse.events.control_events import CatalogControlEvent
from lakehouse.governed import fga_ids, fga_reads, fga_resilience, fga_writes
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit
from lakehouse.governed.settings import FgaSettings
from lakehouse.naming import CONTROL_ID_RE
from lakehouse.records import maintenance_policies
from lakehouse.records.project_records import ProjectRecord
from lakehouse.records.warehouse_records import WarehouseRecord, WarehouseStatus
from lakehouse.storage.conditional_records import RecordExistsError, RecordMissingError
from lakehouse.storage.options import StorageOptions


log = logging.getLogger(__name__)

management_router = APIRouter(prefix="/management/v1/projects", tags=["projects"])

# The DNS-safe id shape the whole control plane enforces — a project id doubles as an FGA object id and a
# registry filename, so a malformed one is rejected up front. SHARED (`catalog.core.identifiers`): this
# `\Z` anchor was fixed here first and the two other copies kept the `$` that lets "acme\n" through.
_ID_RE = CONTROL_ID_RE


def _group_by_project(records: list[WarehouseRecord]) -> dict[str, list[ProjectWarehouse]]:
    """Group warehouse records by their ``project``, sorted for a deterministic response.

    A record that names no ``project`` or ``id`` is no warehouse record and never reaches here: the listing left it out
    with a warning (``warehouses.list_warehouses``), so one tenant's registry corruption does not void the whole estate
    listing. ``status`` is ``"active"`` for a record that carries none.
    """
    grouped: dict[str, list[ProjectWarehouse]] = {}
    for record in records:
        grouped.setdefault(record.project, []).append(ProjectWarehouse(id=record.id, bucket=record.bucket, status=str(record.status or WarehouseStatus.ACTIVE)))
    for entries in grouped.values():
        entries.sort(key=lambda w: w.id)
    return grouped


# Per-project budget for the display-only admins lookup. A caller gives the whole request a few seconds; a slow (not
# down) OpenFGA must never spend that N-projects-over: one attempt (no retries — the wrapper's default
# bounded-retry ladder alone can exceed the budget) under a tight deadline, degrading to [] on expiry.
_ADMINS_TIMEOUT_SECONDS = 2.0


async def _admins_for(client: OpenFgaClient | None, settings: FgaSettings, project: str) -> list[str]:
    """The project's effective ``can_administer`` subjects via ``fga_reads.list_users`` (#51), or ``[]``.

    Degrades on an outage: FGA off yields ``[]`` outright (on with no client wired it is the 503 every gate answers); an OpenFGA outage
    (``ServiceUnavailableError``) or a lookup blowing the per-call ``_ADMINS_TIMEOUT_SECONDS`` budget is
    logged and yields ``[]`` too, so the registry-derived facts still serve while the authz lookup is
    down or slow. The estate gate already ran fail-closed before this, so a degraded ``admins`` is a
    display gap, not an authz gap.
    """
    if (client := decisions.wired(settings, client)) is None:
        return []
    try:
        async with asyncio.timeout(_ADMINS_TIMEOUT_SECONDS):
            return await fga_reads.list_users(client, relation="can_administer", obj=f"project:{project}", retry=fga_resilience.ONE_ATTEMPT)
    except (ServiceUnavailableError, TimeoutError):
        log.warning("project_admins_unavailable", extra={"project": project})
        return []


class Tenant(BaseModel):
    """A tenant as the registries know it: its registry record, if one names it, and the warehouses claiming it.

    Attributes:
        project: The tenant's id.
        record: Its registry record; None for a tenant only a warehouse names (pre-registry legacy).
        warehouses: The warehouses whose records claim it.
    """

    model_config = ConfigDict(frozen=True)

    project: str
    record: ProjectRecord | None = None
    warehouses: list[ProjectWarehouse] = Field(default_factory=list)

    def response(self, admins: list[str]) -> ProjectResponse:
        """The tenant as its read answers it, with ``admins`` as its effective admins.

        Its deletion protection is its record's, so a tenant no record names is never protected.
        """
        record = self.record
        return ProjectResponse(
            project=self.project,
            warehouses=self.warehouses,
            admins=admins,
            protected=record is not None and record.is_protected,
            protected_by=record.protected_by if record is not None else None,
            managed_by=GIT if record is not None and record.managed_by == GIT else "self",
        )


async def _load_tenants(settings: CatalogStore) -> dict[str, Tenant]:
    """The tenant map: PROJECT REGISTRY ∪ warehouse-derived (blocking S3/FS IO → threadpool).

    Registry records define the tenants — a fresh project has zero warehouses and must still list.
    Warehouse-derived entries without a registry record are pre-registry legacy: kept visible (the
    reconciler reports them as drift; a listing that hid them would make the drift undiagnosable), and never
    protected, since protection lives on the record."""
    so = settings.storage_options()
    grouped = _group_by_project(await run_in_threadpool(warehouses.list_warehouses, settings.registry_root, so))
    tenants = {project: Tenant(project=project, warehouses=held) for project, held in grouped.items()}
    for record in await run_in_threadpool(project_registry.list_projects, settings.registry_root, so):
        tenants[record.id] = Tenant(project=record.id, record=record, warehouses=grouped.get(record.id, []))
    return tenants


async def _converge_project(settings: CatalogStore, so: StorageOptions, record: ProjectRecord) -> ProjectRecord:
    """The conditional re-POST write (diff2 F1 leg c) — shared by the sequential and lost-race paths.

    Both used to end in an unconditional `put_project` of a record assembled from a read taken earlier
    in the handler, so a `/protection` call landing in that window was silently reverted. The merge now
    happens inside the write, against the record as it actually stands.
    """
    try:
        return await run_in_threadpool(project_registry.upsert_project, settings.registry_root, so, record)
    except RecordMissingError:
        # Raced a concurrent tenant delete: retryable, never a blind re-create — code 14 -> 409, so
        # the caller retries into a clean mint rather than resurrecting a project just deleted.
        raise ConcurrentModificationError(f"project {record.id!r} write raced a concurrent delete; retry") from None
    except project_registry.ProjectBeingDeletedError:
        # Its id is free once the delete has revoked and ended what it held ([[LH-576]]): a tenant made over a delete
        # still running would hold whatever that delete has yet to take away.
        raise ConcurrentModificationError(f"project {record.id!r} is being deleted; create it once its delete has finished") from None


async def _write_project_record(settings: CatalogStore, so: StorageOptions, record: ProjectRecord, *, existing: ProjectRecord | None) -> ProjectRecord:
    """Mint ``record``, or converge the ``existing`` record that stands onto it; the record as written."""
    if existing is not None:
        return await _converge_project(settings, so, record)
    # The tenant MINT is conditional (F1): two concurrent creates of one id both read "absent"; the store refuses the
    # second, which then converges on the winner's identity fields exactly like the sequential idempotent re-POST.
    try:
        await run_in_threadpool(project_registry.create_project_record, settings.registry_root, so, record)
    except RecordExistsError:
        # Lost the mint race — converge on the WINNER, conditionally (diff2 F1 leg c). The merge
        # happens inside the write, against the record as it actually stands, so a protection
        # change landing in this window is preserved instead of clobbered.
        return await _converge_project(settings, so, record)
    return record


@management_router.post("", response_model_exclude_none=True)
async def create_project(
    *,
    context: AuthzContextDep,
    announcer: AnnouncerDep,
    body: CreateProjectRequest,
) -> ProjectResponse:
    """Mint a tenant: registry record + the creator's ``admin`` tuple, in one operation. Estate-admin gated.

    The gate is ``can_create_project`` on the fixed root object: the estate admin, or a provisioning identity
    holding ``operator`` there (ADR 0143). Idempotent: re-POSTing an existing id carries ``created_at``/``created_by`` and the deletion
    protection forward and re-asserts the admin tuple (a duplicate FGA write is a no-op), so a partial-failure retry
    (record written, tuple write failed) converges instead of erroring. The grant is audited and published
    like any other ACL change, only when it was actually written; what the create announces is filed before the record,
    so a create that fails after it still announces it ([[LH-576]])."""
    project_id = body.id
    if not _ID_RE.match(project_id):
        raise InvalidInputError(f"invalid project id {project_id!r}: must match {_ID_RE.pattern}")
    admin = object_grants.project_admin_for(context.token, body.admin)
    await door_gates.require_relation(context, relation="can_create_project", obj=context.settings.fga_root_object)
    record = await establish_project(context.settings, context.client, announcer, project_id=project_id, admin=admin, author=author_of(context.token))
    return Tenant(project=project_id, record=record).response(await _admins_for(context.client, context.settings, project_id))


async def establish_project(
    settings: Settings, client: OpenFgaClient | None, announcer: Announcer, *, project_id: str, admin: str | None, author: Author | None
) -> ProjectRecord:
    """Make project ``project_id``, or converge the one that stands; its record as written.

    What the create door does once its caller is authorized, and what the catalog does for a project git declares
    ([[XC-213]]): the registry record and, where ``admin`` names one, that person's ``admin`` tuple, in one operation.
    Idempotent: an existing id keeps ``created_at``/``created_by`` and its deletion protection and has the admin tuple
    re-asserted. The grant is audited and published like any other ACL change, only when it was actually written; what
    the create announces is filed before the record, so a create that fails after it still announces it ([[LH-576]]).

    Raises:
        ConcurrentModificationError: The project is being deleted, or its write raced a delete.
    """
    so = settings.storage_options()
    existing = await run_in_threadpool(project_registry.get_project, settings.registry_root, so, project_id)
    # Two spellings of one author: the subject the audit trail and the tuples' writer name, and the actor its control
    # events name. A query of the trail by subject finds this create only under the first.
    writer, announced = (author.subject, author.actor) if author is not None else (None, None)
    obj = f"project:{project_id}"
    created = Announcement(
        evidence=RecordHeld(key=project_registry.record_key(project_id)),
        message=ControlMessage(event=CatalogControlEvent(action="project_created", object_type="project", object_id=obj, actor=announced)),
    )
    # `seed_project_admin` writes the admin's grant exactly when authorization is on and an admin is named.
    seeds = author is not None and admin is not None and decisions.wired(settings, client) is not None
    admin_granted = (
        announcing.grant_announcement(
            fga_writes.ClientTuple(user=fga_ids.user_object(admin), relation="admin", object=obj),
            action="grant_added",
            actor=announced,
            extra={"reason": "project_created"},
        )
        if seeds and admin is not None
        else None
    )
    filed = announcer.announcing(door="create_project", target=obj, announcements=[created, *([admin_granted] if admin_granted is not None else [])])
    wanted = ProjectRecord(
        id=project_id,
        created_at=(existing.created_at if existing else None) or datetime.now(UTC).isoformat(),
        created_by=(existing.created_by if existing else None) or (writer or ""),
    )
    async with filed as filing:
        record = await _write_project_record(settings, so, wanted, existing=existing)
        granted = await object_grants.seed_project_admin(client, settings, writer, project=project_id, admin=admin)
        log.info("project_created", extra={"project": project_id, "existing": existing is not None})
        await filing.send(created)
        if granted and admin is not None and admin_granted is not None:
            audit(
                AuditAction.ACCESS_GRANT,
                AuditOutcome.SUCCESS,
                subject=writer,
                resource=obj,
                grantee=fga_ids.user_object(admin),
                relation="admin",
                reason="project_created",
            )
            await filing.send(admin_granted)
    return record


async def _visible_projects(context: AuthzContextDep, *, candidates: list[str]) -> set[str] | None:
    """Which of ``candidates`` (the registry's project ids) this caller may list, or ``None`` for every tenant.

    ``None`` for the estate admin, and with FGA off, as for every gate. Otherwise the candidates are batch-checked on
    ``member`` ([[LH-223]]): the listing never asks OpenFGA which projects the caller reaches, an answer the server stops
    counting at its ceiling. Fail-closed: no client, no caller (``decisions.authorization``) and an OpenFGA outage raise,
    so a caller is never shown more, or less, than their memberships by accident.
    """
    if (authz := decisions.authorization(context)) is None:
        return None
    root = context.settings.fga_root_object
    try:
        observer = await fga_reads.check(authz.client, user=authz.subject, relation="can_list_all_projects", obj=root)
        allowed = None if observer else await authz.batch_check("member", [f"project:{p}" for p in candidates])
    except ServiceUnavailableError:
        audit(AuditAction.LIST_PROJECTS, AuditOutcome.FAILURE, subject=authz.subject, resource=root, reason="authz_unavailable")
        raise
    # The estate-wide listing is the disclosure the root object guards, so its decision is audited like
    # every gate; a member's listing is scoped to their own tenants.
    audit(
        AuditAction.AUTHZ,
        AuditOutcome.ALLOW if observer else AuditOutcome.DENY,
        relation="can_list_all_projects",
        subject=authz.subject,
        resource=root,
        reason="list_projects",
    )
    return None if allowed is None else {p for p in candidates if allowed.get(f"project:{p}")}


@management_router.get("")
async def list_projects(context: AuthzContextDep) -> list[ProjectResponse]:
    """The caller's tenants: every tenant for the estate admin, else the ones the caller is a member of.

    ``admins`` degrades to ``[]`` when FGA is off or the ``list_users`` lookup is unavailable — the registry
    facts still serve; the visibility decision itself fails closed (``_visible_projects``)."""
    tenants = await _load_tenants(context.settings)
    visible = await _visible_projects(context, candidates=sorted(tenants))
    # The per-project admins lookups run CONCURRENTLY: sequential awaits would stack N × the per-call
    # budget on a slow OpenFGA (a brownout, not an outage) and blow past the caller's request deadline;
    # gathered, the worst case is ONE _ADMINS_TIMEOUT_SECONDS regardless of tenant count (each call
    # still degrades to [] on its own expiry — see _admins_for).
    projects = sorted(p for p in tenants if visible is None or p in visible)
    admins = await asyncio.gather(*(_admins_for(context.client, context.settings, project) for project in projects))
    return [tenants[project].response(project_admins) for project, project_admins in zip(projects, admins, strict=True)]


@management_router.get("/{project_id}")
async def get_project(*, project_id: str, context: AuthzContextDep) -> ProjectResponse:
    """One tenant, in the list's shape, for the callers the list shows it to: the estate admin, or a member of it.

    Anyone else is answered the missing project's 404, so the door tells no caller which project ids exist (the rule
    :func:`administered_project` states), and the trail records the refusal on each door it asked.
    """
    if not _ID_RE.match(project_id):
        raise InvalidInputError(f"invalid project id {project_id!r}: must match {_ID_RE.pattern}")
    doors = [("can_list_all_projects", context.settings.fga_root_object), ("member", f"project:{project_id}")]
    try:
        await door_gates.require_any_relation(context, doors=doors)
    except PermissionDeniedError as exc:
        raise TableNotFoundError(f"project not found: {project_id}") from exc
    tenant = (await _load_tenants(context.settings)).get(project_id)
    if tenant is None:
        raise TableNotFoundError(f"project not found: {project_id}")
    return tenant.response(await _admins_for(context.client, context.settings, project_id))


async def administered_project(project_id: str, context: AuthzContextDep) -> ProjectRecord:
    """The record of ``project_id``, for a caller who administers it (``project:<id>#can_administer``).

    The record is read first, since the gate is on the tenant it names. NO EXISTENCE ORACLE (audit #4, the rule the
    warehouse doors follow): a caller who may not administer the project is answered exactly the missing project's 404,
    so nobody can enumerate the estate's tenants by a 404-versus-403 difference, which answering
    ``GET /management/v1/projects`` with the caller's own tenants exists to prevent; an outage or a missing caller still
    answers 503 or 401. A create
    door answers 404 for a missing parent because that is a fix its caller needs; the doors this gates have no such
    teaching to do.

    Args:
        project_id: The project the route's path names.
        context: The request's authorization context: the OpenFGA client, the settings and the caller.

    Returns:
        The project's registry record.

    Raises:
        InvalidInputError: The id is not a project id.
        TableNotFoundError: The project is absent, or its caller does not administer it.
    """
    if not _ID_RE.match(project_id):
        raise InvalidInputError(f"invalid project id {project_id!r}: must match {_ID_RE.pattern}")
    record = await run_in_threadpool(project_registry.get_project, context.settings.registry_root, context.settings.storage_options(), project_id)
    if record is None:
        raise TableNotFoundError(f"project not found: {project_id}")
    try:
        await door_gates.require_relation(context, relation="can_administer", obj=f"project:{project_id}")
    except PermissionDeniedError as exc:
        raise TableNotFoundError(f"project not found: {project_id}") from exc
    return record


async def project_to_delete(project_id: str, context: AuthzContextDep) -> ProjectRecord:
    """The record of ``project_id``, for its delete: a caller who administers it starts one, and once one has started,
    the caller who started it, an admin it has not reached, or the estate's owner finishes it ([[LH-576]]).

    A delete writes who started it on the project's record before its first destructive step, and its revoke then takes
    that caller's own admin grant with the rest, so a retry after a later step failed is authorized by the record. The
    estate's owner, who may watch every tenant, may finish what an admin decided, so no unfinished delete outlives its
    starter. Anyone else is answered the missing project's 404, as :func:`administered_project` answers them.

    Args:
        project_id: The project the route's path names.
        context: The request's authorization context: the OpenFGA client, the settings and the caller.

    Returns:
        The project's record, the delete it names included.

    Raises:
        InvalidInputError: The id is not a project id.
        TableNotFoundError: The project is absent, or its caller may not delete it.
    """
    if not _ID_RE.match(project_id):
        raise InvalidInputError(f"invalid project id {project_id!r}: must match {_ID_RE.pattern}")
    record = await run_in_threadpool(project_registry.get_project_record, context.settings.registry_root, context.settings.storage_options(), project_id)
    if record is None:
        raise TableNotFoundError(f"project not found: {project_id}")
    starter = record.deleting_by
    if starter is not None and starter == current_subject(context.token):
        audit(AuditAction.AUTHZ, AuditOutcome.ALLOW, relation=project_registry.DELETING_BY, subject=starter, resource=f"project:{project_id}")
        return record
    doors = [("can_administer", f"project:{project_id}")]
    if starter is not None:
        doors.append(("can_finish_project_delete", context.settings.fga_root_object))
    try:
        await door_gates.require_any_relation(context, doors=doors)
    except PermissionDeniedError as exc:
        raise TableNotFoundError(f"project not found: {project_id}") from exc
    return record


def project_its_doors_protect(project_id: str, record: Annotated[ProjectRecord, Depends(administered_project)], token: CurrentToken) -> None:
    """Admit a change to the deletion protection of a project the caller administers and git does not declare ([[XC-213]]).

    Raises:
        PermissionDeniedError: Git declares the project, so its declaration owns the protection.
    """
    refuse_what_git_declares(record.managed_by, obj=f"project:{project_id}", token=token)


def project_its_doors_delete(project_id: str, record: Annotated[ProjectRecord, Depends(project_to_delete)], token: CurrentToken) -> ProjectRecord:
    """The record of a project the caller may delete and git does not declare ([[XC-213]]).

    Raises:
        PermissionDeniedError: Git declares the project, so removing it from the declaration comes first.
    """
    refuse_what_git_declares(record.managed_by, obj=f"project:{project_id}", token=token)
    return record


@management_router.post("/{project_id}/protection", response_model_exclude_none=True, dependencies=[Depends(project_its_doors_protect)])
async def set_project_protection(
    *,
    project_id: str,
    body: SetProtectionRequest,
    settings: SettingsDep,
    token: CurrentToken,
    control: ControlEmitterDep,
) -> ProtectionResponse:
    """Arm or clear the project's deletion protection ([[LH-549]]), the lock its delete turns only with ``force=true``.

    Gated on the project's ``can_administer``: arming or clearing the lock on a tenant's destruction is the authority
    that destruction needs, as the delete door asks it. A caller without it is answered the missing project's 404, as
    the delete answers one, so the door tells nobody which tenants exist. The write is conditional on the record as it
    stands. Re-arming an armed project keeps who armed it. Announced as ``project_protected`` or
    ``project_unprotected`` on every call, as the warehouse, namespace and table doors announce theirs.
    \f

    Args:
        project_id: The project to protect or clear, its caller authorized on it (:func:`administered_project`).
        body: The protection to set.
        settings: The catalog's settings.
        token: The verified caller.
        control: Where control events go.

    Returns:
        The project's id and the protection now set.

    Raises:
        TableNotFoundError: The project is absent, its caller does not administer it, or it was deleted before the write.
    """
    actor = f"user:{token.subject}" if token is not None else None
    change = ProtectionChange(protected=body.protected, by=actor or f"user:{ANONYMOUS_SUBJECT}")
    await protect_project(settings, control, project_id, change=change, actor=actor)
    return ProtectionResponse(id=project_id, protected=body.protected)


async def protect_project(settings: CatalogStore, control: ControlEmitter, project_id: str, *, change: ProtectionChange, actor: str | None) -> None:
    """Arm or clear project ``project_id``'s deletion protection and announce it.

    What the protection door does once its caller is authorized, and what the catalog does for a project whose
    protection drifted from what git declares ([[XC-213]]).

    Raises:
        TableNotFoundError: The project's record is gone.
    """
    if await run_in_threadpool(project_registry.set_project_protection, settings.registry_root, settings.storage_options(), project_id, change=change) is None:
        raise TableNotFoundError(f"project not found: {project_id}")
    log.info("project_protection_set", extra={"project": project_id, "protected": change.protected})
    await emit_control(
        control,
        action="project_protected" if change.protected else "project_unprotected",
        object_type="project",
        object_id=f"project:{project_id}",
        actor=actor,
    )


async def _start_delete(project_id: str, record: ProjectRecord, settings: CatalogStore, *, token: CurrentToken, force: bool) -> None:
    """Judge a delete's start, its protection and the project's emptiness, and record it on the project's record.

    Raises:
        NamespaceNotEmptyError: It is protected and ``force`` is false, or it still holds warehouses.
        TableNotFoundError: Its record went meanwhile.
    """
    so = settings.storage_options()
    # Protection state and the warehouse listing below are both tenant DISCLOSURE, so they run only after
    # `project_to_delete` has proven the caller administers this tenant.
    hierarchy.require_not_protected(record.is_protected, kind="project", obj_id=project_id, force=force)

    # A project's contents ARE its warehouse records — the same registry the listing derives tenants from,
    # so what the 409 names is exactly what GET /management/v1/projects/{id} shows. Read whole: a record that cannot be
    # read may be one of this project's own ([[LH-572]]).
    held = sorted(r.id for r in await run_in_threadpool(warehouses.every_warehouse, settings.registry_root, so) if r.project == project_id)
    if held:
        raise NamespaceNotEmptyError(
            f"project '{project_id}' still holds {len(held)} warehouse(s): {', '.join(held)}. "
            f"Delete each one first — DELETE /management/v1/warehouses/{{id}} (its own bucket purge is a separate "
            f"opt-in) — then delete the project. There is deliberately no cascade here: retiring a tenant "
            f"must never be able to destroy its buckets transitively in one request."
        )
    # Recorded before the first destructive step, so whichever step fails, the retry finds the delete and who started it.
    if await run_in_threadpool(project_registry.mark_deleting, settings.registry_root, so, project_id, by=current_subject(token)) is None:
        raise TableNotFoundError(f"project not found: {project_id}")


@management_router.delete("/{project_id}")
async def delete_project(
    *,
    project_id: str,
    keyed: idem.KeyedDoorDep,
    context: AuthzContextDep,
    announcer: AnnouncerDep,
    force: bool = False,
) -> DeleteProjectResponse:
    """Retire a tenant: refuse while it still holds warehouses, else revoke its tuples, end its roles and its maintenance
    policy, and drop its record.

    Gated on ``project:<id>#can_administer``, the tenant's OWN admin bar: the rungs that open its reads, the estate
    observer's and membership, retire nothing (module docstring). Order, fail-closed and each step earning the next:
    id shape (400) → existence and authz (both 404, :func:`project_to_delete`) → deletion protection (409) →
    emptiness (409) → the delete recorded on the project's record → revoke tuples → end its roles → end its maintenance
    policy → delete record → audit + event.

    **A delete that failed part-way is finished by asking again** ([[LH-576]]): the record names the caller who started
    it, whose own admin grant the revoke takes, and :func:`project_to_delete` lets that caller, an admin the revoke has
    not reached, or the estate's owner finish it. Every later step is idempotent, and the protection and emptiness
    were judged when the delete was recorded: from then on the project is absent to every other door
    (``project_registry.get_project``), so no warehouse, role or grant is made for it meanwhile.

    **Its roles go with it** ([[LH-557]]): a role belongs to one project and holds no data, so it is no content to
    empty first. Each is ended as its own delete door ends it (every tuple naming it and on it, then its record), so
    no role of a retired tenant keeps a grant.

    **Emptiness is bottom-up** (`open_hierarchy_lifecycle.md` Decision 3): a project holding warehouses
    refuses 409 and NAMES them — a refusal that does not say what blocks it just moves the search to the
    user. **There is deliberately no ``cascade`` parameter on this route at all.** A project cascade would
    reach warehouses, and a warehouse's own delete can purge a bucket; one request must never be able to
    destroy a tenant's storage transitively. Emptying goes one rung at a time through
    ``DELETE /management/v1/warehouses/{id}``, where the purge is its own separate opt-in.

    ``force=true`` overrides deletion PROTECTION only (Decision 5) — the FGA gate above ran first and runs
    identically with or without it, so forcing cannot delete a project the caller may not administer.
    \f

    Args:
        project_id: The project to retire.
        keyed: The caller's `Idempotency-Key`, when it sent one.
        context: The request's authorization context: the OpenFGA client, the settings and the caller.
        announcer: Where what it announces is filed and sent.
        force: Delete it though it is protected.

    Returns:
        The retired project and how many tuples went with it.

    Raises:
        NamespaceNotEmptyError: It is protected and ``force`` is false, or it still holds warehouses.
        InternalError: A warehouse record is there and cannot be read as one, and nothing was deleted; or a role record,
            and the delete stands recorded for a retry to finish once it is written again.
        ServiceUnavailableError: The store did not answer a read, or OpenFGA could not revoke its tuples; a delete
            recorded by then is finished by a retry.
    """
    # Asked before the record is read: a replay under the caller's key answers the first delete and reads nothing, so a
    # project registered under the id since is never the one a resent delete removes.
    converge = await keyed.begin(target=f"project:{project_id}", takeover="unknown")
    if converge.replay is not None:
        return DeleteProjectResponse.model_validate(converge.replay.body)
    record = project_its_doors_delete(project_id, await project_to_delete(project_id, context), context.token)
    so = context.settings.storage_options()
    # Asked before the delete is recorded, so a deployment whose client is not wired refuses with nothing started.
    authz = decisions.wired(context.settings, context.client)
    if not record.is_being_deleted:
        await _start_delete(project_id, record, context.settings, token=context.token, force=force)
    # The caller who retired the tenant, in the two spellings `establish_project` names its author by: the subject the
    # audit trail and the revoked tuples' writer name, and the actor its control events name. `system:catalog` is the
    # honest writer on an auth-off stack, where there genuinely is no principal, never a stand-in for one we failed to thread.
    writer = context.token.subject if context.token is not None else "system:catalog"
    announced = f"user:{context.token.subject}" if context.token is not None else None
    obj = f"project:{project_id}"
    retiring = _Retiring(project_id=project_id, settings=context.settings, so=so, authz=authz, writer=writer, announced=announced)
    # Whom the revoke reaches is read and filed before it reaches them, so a failure after it still tells them ([[LH-576]]).
    grants = announcing.grant_changes(
        await fga_reads.read_object_tuples(authz, obj) if authz is not None else [], action="grant_revoked", actor=announced, reason="project_deleted"
    )
    deleted_event = CatalogControlEvent(action="project_deleted", object_type="project", object_id=obj, actor=announced)
    deleted = Announcement(evidence=RecordGone(key=project_registry.record_key(project_id)), message=ControlMessage(event=deleted_event))
    async with announcer.announcing(door="delete_project", target=obj, announcements=[deleted, *grants.values()]) as filing:
        removed: list[fga_writes.ClientTuple] = []
        # Tuples FIRST, record second. A revoke that fails (OpenFGA outage → 503) leaves the tenant fully
        # described and re-deletable; the opposite order would strand grants on a project no API can name any
        # more — privilege that nothing in the product can see, let alone clear.
        if authz is not None:
            removed = await fga_writes.revoke_object_tuples(authz, obj, actor=writer, origin="lifecycle_delete")  # noqa: TID251
        await _end_its_roles(retiring, announcer)
        if authz is not None:
            # A role's ending moves its project's `admin_revision` (`project_admins.withdraw`), bookkeeping no grant
            # reads and nobody is told of, so the project's tuples are revoked once more after its last role ended.
            await fga_writes.revoke_object_tuples(authz, obj, actor=writer, origin="lifecycle_delete")  # noqa: TID251
        # The revoke removes every tuple whose OBJECT is `project:<id>` (the admin/member grants), and each role's end every
        # tuple on and naming that role, its `project` edge among them. Tuples where the project is the USER are otherwise
        # `warehouse:X#project@project:<id>`: none can survive here, because the emptiness refusal above proves no warehouse
        # record claims this project, and a warehouse's delete revokes its own tuples (that edge among them) before its
        # record goes.
        await _end_its_records(retiring)

        log.info("project_deleted", extra={"project": project_id, "tuples_revoked": len(removed), "forced": force})
        if authz is not None:
            # Mirrors create_project's grant audit, and only when the revoke really ran: an FGA-off stack must
            # not fabricate a compliance record for a revocation that never happened. The revoke is
            # subject-agnostic by design (it deletes EVERY tuple on the object), so the grantee is the wildcard
            # and `removed` carries the magnitude.
            audit(
                AuditAction.ACCESS_REVOKE,
                AuditOutcome.SUCCESS,
                subject=writer,
                resource=obj,
                grantee="*",
                relation="*",
                removed=len(removed),
                reason="project_deleted",
            )
        await filing.send(deleted)
        # TELL EVERYONE WHO JUST LOST IT. `project_deleted` names the project and nobody in it, so the admins and members who
        # can no longer see their own work would find out by hitting a 403, the failure `grant_revoked` exists to prevent.
        # A structural edge names no person: `grant_changes` keeps a principal (`user:<principal>`) or a userset
        # (`role:x#assignee`, which the lane expands), never a `project`/`parent`/`child` edge whose user is an object.
        await filing.send_grant_changes(grants, removed, action="grant_revoked", actor=announced, reason="project_deleted")
    answer = DeleteProjectResponse(project=project_id, tuples_revoked=len(removed))
    await converge.remember(200, answer)
    return answer


class _Retiring(BaseModel):
    """The project a delete retires, the store its records are in, and who retires it, in both spellings."""

    # `arbitrary_types_allowed` for `authz`: an OpenFgaClient is no pydantic type.
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    project_id: str
    settings: CatalogStore
    so: StorageOptions
    # An OpenFGA client is opaque: what the door was handed is what it uses, a stand-in in a test included.
    authz: SkipValidation[OpenFgaClient | None]
    writer: str
    announced: str | None


async def _end_its_roles(retiring: _Retiring, announcer: Announcer) -> None:
    """End every role of the retiring project, each announced as its own delete door announces it."""
    # Each role is ended as its own delete door ends it, with authorization off too, so no record outlives its project.
    for role in await run_in_threadpool(roles.every_role, retiring.settings.registry_root, retiring.so, retiring.project_id):
        ending = await announcing.role_ending(retiring.authz, role, actor=retiring.announced, reason="project_deleted")
        async with announcer.announcing(door="end_role", target=roles.role_object(role.id), announcements=ending.announcements()) as ending_filed:
            ended = await role_grants.end_role(
                retiring.authz, role, control_root=retiring.settings.registry_root, storage_options=retiring.so, actor=retiring.writer
            )
            await ending.send(ending_filed, ended, actor=retiring.announced, reason="project_deleted")


async def _end_its_records(retiring: _Retiring) -> None:
    """End the retiring project's maintenance policy, label definitions and record, in that order."""
    # Its maintenance policy goes before its record ([[LH-562]]): a project created again under the id is another tenant,
    # and a policy kept for the id would govern that tenant's buckets as soon as it named them.
    await run_in_threadpool(maintenance_policies.delete_policy, retiring.settings.registry_root, retiring.so, "project", key=retiring.project_id)
    # Its label definitions too, for the same reason (ADR 0211). No object of it carries one by now: every object
    # lay in a warehouse of it, and each ended its labels as it went.
    await run_in_threadpool(object_labels.end_definitions, retiring.settings.registry_root, retiring.so, retiring.project_id)
    await run_in_threadpool(project_registry.delete_project_record, retiring.settings.registry_root, retiring.so, retiring.project_id)
