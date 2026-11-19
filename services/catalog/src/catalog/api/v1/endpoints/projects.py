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
(:func:`get_project`); create asks ``can_create_project`` on the root through ``fga_deps.require_relation``.
``/management/v1/projects`` matches no resource prefix in
``fga_deps.authorize``, so the router-wide gate only sets the 401/503 floor; every decision here is
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
from collections.abc import Awaitable, Callable, Collection
from datetime import UTC, datetime
from functools import partial
from typing import Annotated, NoReturn

from fastapi import APIRouter, Depends
from fastapi.concurrency import run_in_threadpool
from lance_namespace import (
    ConcurrentModificationError,
    InvalidInputError,
    NamespaceAlreadyExistsError,
    NamespaceNotEmptyError,
    PermissionDeniedError,
    ServiceUnavailableError,
    TableNotFoundError,
)
from openfga_sdk import OpenFgaClient
from pydantic import BaseModel, ConfigDict, Field

from catalog.api import announcing, fga_deps
from catalog.api.announcing import Announcer, AnnouncerDep
from catalog.api.declared import Author, author_of, refuse_what_git_declares
from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, SettingsDep
from catalog.api.security import CurrentToken, current_subject
from catalog.core.config import Settings
from catalog.schemas import (
    CreateProjectRequest,
    DeleteProjectResponse,
    ProjectResponse,
    ProjectWarehouse,
    ProtectionResponse,
    SetProtectionRequest,
)
from catalog.services import object_labels, role_grants, roles, tenancy, warehouses
from catalog.services import projects as project_registry
from catalog.services.announcement_intents import Announcement, ControlMessage, RecordGone, RecordHeld
from catalog.services.control_records import DELETING_BY, GIT, MANAGED_BY, STATE, UUID, ProtectionChange
from lakehouse import maintenance_policies, protection
from lakehouse.control_emit import ControlEmitter, emit_control
from lakehouse.control_events import CatalogControlEvent
from lakehouse.governed import fga
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit
from lakehouse.governed.deps import ANONYMOUS_SUBJECT
from lakehouse.governed.tenancy import CREATING, LIVE, PROJECT, new_uuid, require_an_id, warehouses_of
from lakehouse.naming import CONTROL_ID_RE
from lakehouse.ns_errors import PartiallyApplied
from lakehouse.records import RecordExistsError
from lakehouse.storage_options import StorageOptions


log = logging.getLogger(__name__)

router = APIRouter(prefix="/management/v1/projects", tags=["projects"])

# The DNS-safe id shape the whole control plane enforces — a project id doubles as an FGA object id and a
# registry filename, so a malformed one is rejected up front. SHARED (`catalog.core.identifiers`): this
# `\Z` anchor was fixed here first and the two other copies kept the `$` that lets "acme\n" through.
_ID_RE = CONTROL_ID_RE


def _listed(records: list[dict[str, str]]) -> list[ProjectWarehouse]:
    """``records`` as a tenant's listing names its warehouses, sorted by id for a deterministic response. ``status``
    defaults to ``"active"`` for a record written before the lifecycle feature."""
    return sorted(
        (ProjectWarehouse(id=str(r["id"]), bucket=str(r.get("bucket") or ""), status=str(r.get("status") or "active")) for r in records), key=lambda w: w.id
    )


# Per-project budget for the display-only admins lookup. A caller gives the whole request a few seconds; a slow (not
# down) OpenFGA must never spend that N-projects-over: one attempt (no retries — the wrapper's default
# bounded-retry ladder alone can exceed the budget) under a tight deadline, degrading to [] on expiry.
_ADMINS_TIMEOUT_SECONDS = 2.0


async def _admins_for(client: OpenFgaClient | None, settings: Settings, project: dict[str, str] | None) -> list[str]:
    """The project's effective ``can_administer`` subjects via ``fga.list_users`` (#51), or ``[]``.

    Degrades on an outage: FGA off yields ``[]`` outright (on with no client wired it is the 503 every gate answers); an OpenFGA outage
    (``ServiceUnavailableError``) or a lookup blowing the per-call ``_ADMINS_TIMEOUT_SECONDS`` budget is
    logged and yields ``[]`` too, so the registry-derived facts still serve while the authz lookup is
    down or slow. The estate gate already ran fail-closed before this, so a degraded ``admins`` is a
    display gap, not an authz gap. A tenant no project record holds (``project`` ``None``) has no creation to ask on.
    """
    if project is None or (client := fga_deps.wired(settings, client)) is None:
        return []
    try:
        async with asyncio.timeout(_ADMINS_TIMEOUT_SECONDS):
            return await fga.list_users(client, relation="can_administer", obj=tenancy.object_of_record(PROJECT, project), retry_attempts=1)
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
    record: dict[str, str] | None = None
    warehouses: list[ProjectWarehouse] = Field(default_factory=list)

    def response(self, admins: list[str]) -> ProjectResponse:
        """The tenant as its read answers it, with ``admins`` as its effective admins.

        Its deletion protection is its record's, so a tenant no record names is never protected.
        """
        record = self.record or {}
        return ProjectResponse(
            project=self.project,
            warehouses=self.warehouses,
            admins=admins,
            protected=protection.is_protected(record),
            protected_by=record.get("protected_by"),
            managed_by=GIT if record.get(MANAGED_BY) == GIT else "self",
        )


class Registries(BaseModel):
    """The project and warehouse registries as one read found them, from which the tenant map is built."""

    model_config = ConfigDict(frozen=True)

    projects: list[dict[str, str]]
    warehouses: list[dict[str, str]]

    def ids(self) -> list[str]:
        """Every tenant id: each project record's, and each a warehouse record names with no project record."""
        return sorted({str(r["id"]) for r in self.projects} | {str(r.get("project")) for r in self.warehouses})

    def tenants(self, chosen: Collection[str] | None = None) -> dict[str, Tenant]:
        """The tenant map, of the ``chosen`` ids alone (every one for ``None``): PROJECT REGISTRY ∪ warehouse-derived.

        Registry records define the tenants — a fresh project has zero warehouses and must still list. Warehouse-derived
        entries without a registry record are pre-registry legacy: kept visible (the reconciler reports them as drift; a
        listing that hid them would make the drift undiagnosable), and never protected, since protection lives on the
        record. A tenant's warehouses are its live creation's (``tenancy.warehouses_of``, ADR 0245), judged only for the
        tenants chosen, so a caller shown none of them is answered before any record is judged.
        """
        wanted = (lambda project: True) if chosen is None else (lambda project: project in chosen)
        registered = {str(record["id"]): record for record in self.projects}
        unregistered: dict[str, list[dict[str, str]]] = {}
        for r in self.warehouses:
            if str(r.get("project")) not in registered and wanted(str(r.get("project"))):
                unregistered.setdefault(str(r.get("project")), []).append(r)
        tenants = {project: Tenant(project=project, warehouses=_listed(held)) for project, held in unregistered.items()}
        for project, record in registered.items():
            if wanted(project):
                # A warehouse record an earlier creation of the project left is no warehouse of this one (ADR 0245).
                tenants[project] = Tenant(project=project, record=record, warehouses=_listed(warehouses_of(self.warehouses, record[UUID])))
        return tenants


async def _registries(settings: Settings) -> Registries:
    """The project and warehouse registries, read now (blocking S3/FS IO → threadpool)."""
    so = settings.storage_options()
    projects = await run_in_threadpool(project_registry.list_projects, settings.registry_root, so)
    return Registries(projects=projects, warehouses=await run_in_threadpool(warehouses.list_warehouses, settings.registry_root, so))


async def _write_project_record(settings: Settings, so: StorageOptions, record: dict[str, str], *, existing: dict[str, str] | None) -> dict[str, str]:
    """Mint ``record``, or converge the ``existing`` record that stands onto it; the record as written.

    A re-POST converges onto the creation it read, under the ETag of a read that finds it (diff2 F1 leg c, ADR 0245). A
    mint is conditional on absence (F1), and one that lost the race wrote nothing: the id exists, which the spec answers
    409 (``NamespaceAlreadyExistsError``), and the loser seeds and announces nothing, since the uuid it minted names no
    record (review T9).

    Raises:
        NamespaceAlreadyExistsError: A concurrent create made the project first.
        ConcurrentModificationError: The project is being deleted, or the creation the re-POST read ended meanwhile.
    """
    if existing is None:
        try:
            await run_in_threadpool(project_registry.create_project_record, settings.registry_root, so, record)
        except RecordExistsError:
            raise NamespaceAlreadyExistsError(f"project {record['id']!r} was made by a concurrent create; nothing was made by this one") from None
        return record
    try:
        written = await run_in_threadpool(partial(project_registry.upsert_project, settings.registry_root, so, record, uuid=existing[UUID]))
    except project_registry.ProjectBeingDeletedError:
        # Its id is free once the delete has revoked and ended what it held ([[LH-576]]): a tenant made over a delete
        # still running would hold whatever that delete has yet to take away.
        raise ConcurrentModificationError(f"project {record['id']!r} is being deleted; create it once its delete has finished") from None
    if written is None:
        # Raced a concurrent tenant delete: retryable, never a blind re-create — code 14 -> 409, so the caller retries
        # into a clean mint rather than resurrecting a project just deleted.
        raise ConcurrentModificationError(f"project {record['id']!r} write raced a concurrent delete; retry")
    return written


@router.post("", response_model_exclude_none=True)
async def create_project(
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
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
    admin = fga_deps.project_admin_for(token, body.admin)
    await fga_deps.require_relation(client, settings, token, relation="can_create_project", obj=settings.fga_root_object)
    record = await establish_project(settings, client, announcer, project_id=project_id, admin=admin, author=author_of(token))
    return Tenant(project=project_id, record=record).response(await _admins_for(client, settings, record))


async def _lost_creation(settings: Settings, client: OpenFgaClient | None, project_id: str, record: dict[str, str]) -> NoReturn:
    """Answer a create (or a re-POST) whose flip found its creation neither creating nor live: it announces nothing and
    answers what stands (ADR 0245, third review).

    A creation still standing is being deleted, and its delete decides what its tuples become. One no record names any
    more ended under the create, so what the create wrote on it (its admin's grant, landing after the delete's revoke) is
    taken back now, the whole object revoked as the converge tick's sweep of an ended creation revokes it (a uuid is
    never minted again, so nothing else can hold it); one this cannot take back the sweep takes.

    Raises:
        NamespaceAlreadyExistsError: Another creation of the id stands now: the create lost the id, as one that lost the race
            for it does.
        ConcurrentModificationError: The creation is being deleted, or no creation of the id stands; make it again.
    """
    standing = await run_in_threadpool(project_registry.get_project_record, settings.registry_root, settings.storage_options(), project_id)
    log.warning("project_creation_lost", extra={"project": project_id, "uuid": record[UUID], "standing": standing.get(UUID) if standing else None})
    if standing is not None and standing.get(UUID) == record[UUID]:
        raise ConcurrentModificationError(f"project {project_id!r} is being deleted; create it once its delete has finished")
    try:
        await fga_deps.end_object(client, settings, tenancy.object_of_record(PROJECT, record), token=None)
    except ServiceUnavailableError:
        log.warning("lost_creation_grant_left", extra={"project": project_id, "uuid": record[UUID]})
    if standing is not None:
        raise NamespaceAlreadyExistsError(f"project {project_id!r} was deleted and made again while this request ran; nothing was made by this one")
    raise ConcurrentModificationError(f"project {project_id!r} was deleted while it was made; make it again")


async def establish_project(
    settings: Settings, client: OpenFgaClient | None, announcer: Announcer, *, project_id: str, admin: str | None, author: Author | None
) -> dict[str, str]:
    """Make project ``project_id``, or converge the one that stands; its record as written.

    What the create door does once its caller is authorized, and what the catalog does for a project git declares
    ([[XC-213]]): the registry record and, where ``admin`` names one, that person's ``admin`` tuple, in one operation.
    Idempotent: an existing id keeps ``created_at``/``created_by`` and its deletion protection and has the admin tuple
    re-asserted. The grant is audited and published like any other ACL change, only when it was actually written; what
    the create announces is filed once its record is written, before its seed, so a create that fails after it still
    announces it ([[LH-576]]).

    In the order ADR 0245 sets: the record first, ``creating``, naming the uuid this creation mints and the admin it
    seeds; then the admin's grant on that uuid; then the record flips ``live``, and only then does any door resolve it.
    A create that dies between is finished by the converge tick from what the record names. A re-POST of a project still
    ``creating`` finishes it the same way.

    Raises:
        InvalidInputError: ``project_id`` has the shape of a uuid (ADR 0245).
        NamespaceAlreadyExistsError: A concurrent create made the project first; this one made nothing.
        ConcurrentModificationError: The project is being deleted, or its write raced a delete.
    """
    try:
        require_an_id(project_id, what="project id")
    except ValueError as exc:
        raise InvalidInputError(str(exc)) from exc
    so = settings.storage_options()
    existing = await run_in_threadpool(project_registry.get_project_record, settings.registry_root, so, project_id)
    actor = author.actor if author is not None else None
    target = f"project:{project_id}"
    # The creation this create mints, or the one a re-POST finds, which its merge keeps.
    uuid = existing[UUID] if existing else new_uuid()
    obj = tenancy.object_of_record(PROJECT, {UUID: uuid})
    created = Announcement(
        # Landed only where the record holds THIS creation live: a record the id names for another creation is no
        # evidence of this one, and a create whose creation ended under it announces nothing.
        evidence=RecordHeld(key=project_registry.record_key(project_id), fields={UUID: uuid, STATE: LIVE}),
        message=ControlMessage(event=CatalogControlEvent(action="project_created", object_type="project", object_id=target, actor=actor, extra={UUID: uuid})),
    )
    # `seed_project_admin` writes the admin's grant exactly when authorization is on and an admin is named.
    seeds = author is not None and admin is not None and fga_deps.wired(settings, client) is not None
    admin_granted = (
        announcing.grant_announcement(
            fga.ClientTuple(user=fga.user_object(admin), relation="admin", object=obj),
            action="grant_added",
            actor=actor,
            object_name=project_id,
            object_uuid=uuid,
            extra={"reason": "project_created"},
        )
        if seeds and admin is not None
        else None
    )
    record = {
        "id": project_id,
        UUID: uuid,
        STATE: CREATING,
        "created_at": (existing.get("created_at") if existing else None) or datetime.now(UTC).isoformat(),
        "created_by": (existing.get("created_by") if existing else None) or (author.subject if author is not None else ""),
        **({"admin": admin} if admin is not None else {}),
    }
    # The record first, and only then the announcement is filed: a create that lost the race to the record has nothing
    # to announce, and its uuid names no record (review T9).
    record = await _write_project_record(settings, so, record, existing=existing)
    filed = announcer.announcing(door="create_project", target=target, announcements=[created, *([admin_granted] if admin_granted is not None else [])])
    async with filed as filing:
        granted = await fga_deps.seed_project_admin(client, settings, author.subject if author is not None else None, project=record, admin=admin)
        flipped = await run_in_threadpool(project_registry.set_live, settings.registry_root, so, project_id, record[UUID])
        if flipped is None or flipped.get(STATE) != LIVE:
            # The creation this create wrote on ended under it (or another tenant made the id again): it is lost, as a
            # create that lost the race for its id is (ADR 0245, third review). It announces nothing, takes back the grant
            # it wrote on the creation that ended, and answers what stands.
            await _lost_creation(settings, client, project_id, record)
        record = flipped
        log.info("project_created", extra={"project": project_id, "existing": existing is not None})
        await filing.send(created)
        if granted and admin is not None and admin_granted is not None:
            audit(
                AuditAction.ACCESS_GRANT,
                AuditOutcome.SUCCESS,
                subject=actor,
                resource=obj,
                project=project_id,
                grantee=fga.user_object(admin),
                relation="admin",
                reason="project_created",
            )
            await filing.send(admin_granted)
    return record


async def _visible_projects(client: OpenFgaClient | None, settings: Settings, token: CurrentToken, candidates: dict[str, str]) -> set[str] | None:
    """Which of ``candidates`` (the registry's live project ids, each to the object of its creation) this caller may
    list, or ``None`` for every tenant.

    ``None`` for the estate admin, and with FGA off, as for every gate. Otherwise the candidates are batch-checked on
    ``member`` ([[LH-223]]): the listing never asks OpenFGA which projects the caller reaches, an answer the server stops
    counting at its ceiling. Fail-closed: no client, no caller (``fga_deps.authorization``) and an OpenFGA outage raise,
    so a caller is never shown more, or less, than their memberships by accident.
    """
    if (authz := fga_deps.authorization(settings, client, token)) is None:
        return None
    root = settings.fga_root_object
    try:
        observer = await fga.check(authz.client, user=authz.subject, relation="can_list_all_projects", obj=root)
        allowed = None if observer else await authz.batch_check("member", list(candidates.values()))
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
    return None if allowed is None else {p for p, obj in candidates.items() if allowed.get(obj)}


@router.get("")
async def list_projects(settings: SettingsDep, token: CurrentToken, client: FgaClientDep) -> list[ProjectResponse]:
    """The caller's tenants: every tenant for the estate admin, else the ones the caller is a member of.

    ``admins`` degrades to ``[]`` when FGA is off or the ``list_users`` lookup is unavailable — the registry
    facts still serve; the visibility decision itself fails closed (``_visible_projects``)."""
    registries = await _registries(settings)
    visible = await _visible_projects(client, settings, token, {str(r["id"]): tenancy.object_of_record(PROJECT, r) for r in registries.projects})
    tenants = registries.tenants(visible)
    # The per-project admins lookups run CONCURRENTLY: sequential awaits would stack N × the per-call
    # budget on a slow OpenFGA (a brownout, not an outage) and blow past the caller's request deadline;
    # gathered, the worst case is ONE _ADMINS_TIMEOUT_SECONDS regardless of tenant count (each call
    # still degrades to [] on its own expiry — see _admins_for).
    projects = sorted(tenants)
    admins = await asyncio.gather(*(_admins_for(client, settings, tenants[project].record) for project in projects))
    return [tenants[project].response(project_admins) for project, project_admins in zip(projects, admins, strict=True)]


@router.get("/{project_id}")
async def get_project(project_id: str, settings: SettingsDep, token: CurrentToken, client: FgaClientDep) -> ProjectResponse:
    """One tenant, in the list's shape, for the callers the list shows it to: the estate admin, or a member of it.

    Anyone else is answered the missing project's 404, so the door tells no caller which project ids exist (the rule
    :func:`administered_project` states), and the trail records the refusal on each door it asked.
    """
    if not _ID_RE.match(project_id):
        raise InvalidInputError(f"invalid project id {project_id!r}: must match {_ID_RE.pattern}")
    await fga_deps.on_tenant(
        settings,
        PROJECT,
        project_id,
        gate=_as_absent(
            project_id,
            lambda obj, label: fga_deps.require_any_relation(
                client, settings, token, doors=[("can_list_all_projects", settings.fga_root_object), ("member", obj)], labels={obj: label}
            ),
        ),
    )
    tenant = (await _registries(settings)).tenants({project_id}).get(project_id)
    if tenant is None:
        raise TableNotFoundError(f"project not found: {project_id}")
    return tenant.response(await _admins_for(client, settings, tenant.record))


def _as_absent(project_id: str, require: Callable[[str, str], Awaitable[None]]) -> Callable[[str, str], Awaitable[None]]:
    """``require`` as a gate of the one gate function (``fga_deps.on_tenant``) that answers a refused caller the missing
    project's 404, so a door that discloses a tenant's record tells nobody which project ids exist."""

    async def gate(obj: str, label: str) -> None:
        try:
            await require(obj, label)
        except PermissionDeniedError as exc:
            raise TableNotFoundError(f"project not found: {project_id}") from exc

    return gate


async def administered_project(project_id: str, settings: SettingsDep, token: CurrentToken, client: FgaClientDep) -> dict[str, str]:
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
        settings: The catalog's settings.
        token: The verified caller.
        client: The OpenFGA client deciding.

    Returns:
        The project's registry record.

    Raises:
        InvalidInputError: The id is not a project id.
        TableNotFoundError: The project is absent, or its caller does not administer it.
    """
    if not _ID_RE.match(project_id):
        raise InvalidInputError(f"invalid project id {project_id!r}: must match {_ID_RE.pattern}")
    gate = _as_absent(project_id, lambda obj, label: fga_deps.require_relation(client, settings, token, relation="can_administer", obj=obj, label=label))
    return (await fga_deps.on_tenant(settings, PROJECT, project_id, gate=gate)).record


async def project_to_delete(project_id: str, settings: SettingsDep, token: CurrentToken, client: FgaClientDep) -> dict[str, str]:
    """The record of ``project_id``, for its delete: a caller who administers it starts one, and once one has started,
    the caller who started it, an admin it has not reached, or the estate's owner finishes it ([[LH-576]]).

    A delete writes who started it on the project's record before its first destructive step, and its revoke then takes
    that caller's own admin grant with the rest, so a retry after a later step failed is authorized by the record. The
    estate's owner, who may watch every tenant, may finish what an admin decided, so no unfinished delete outlives its
    starter. Anyone else is answered the missing project's 404, as :func:`administered_project` answers them.

    Args:
        project_id: The project the route's path names.
        settings: The catalog's settings.
        token: The verified caller.
        client: The OpenFGA client deciding.

    Returns:
        The project's record, the delete it names included.

    Raises:
        InvalidInputError: The id is not a project id.
        TableNotFoundError: The project is absent, or its caller may not delete it.
    """
    if not _ID_RE.match(project_id):
        raise InvalidInputError(f"invalid project id {project_id!r}: must match {_ID_RE.pattern}")
    record = await run_in_threadpool(project_registry.get_project_record, settings.registry_root, settings.storage_options(), project_id)
    if record is None or record.get(STATE) == CREATING:
        raise TableNotFoundError(f"project not found: {project_id}")
    # Only a delete past its point of no return has revoked anyone, so only finishing one is authorized by the record;
    # taking over a mark short of it is a new delete, asked of the project's admin (ADR 0245, second review S1).
    starter = None if tenancy.judges(record) else record.get(DELETING_BY)
    if starter is not None and starter == current_subject(token):
        audit(AuditAction.AUTHZ, AuditOutcome.ALLOW, relation=DELETING_BY, subject=starter, resource=f"project:{project_id}")
        return record
    doors = [("can_administer", tenancy.object_of_record(PROJECT, record))]
    if starter is not None:
        doors.append(("can_finish_project_delete", settings.fga_root_object))
    try:
        await fga_deps.require_any_relation(client, settings, token, doors=doors)
    except PermissionDeniedError as exc:
        raise TableNotFoundError(f"project not found: {project_id}") from exc
    return record


def project_its_doors_protect(project_id: str, record: Annotated[dict[str, str], Depends(administered_project)], token: CurrentToken) -> dict[str, str]:
    """The record of a project the caller administers and git does not declare, for a change to its deletion protection
    ([[XC-213]]).

    Raises:
        PermissionDeniedError: Git declares the project, so its declaration owns the protection.
    """
    refuse_what_git_declares(record, obj=f"project:{project_id}", token=token)
    return record


def project_its_doors_delete(project_id: str, record: Annotated[dict[str, str], Depends(project_to_delete)], token: CurrentToken) -> dict[str, str]:
    """The record of a project the caller may delete and git does not declare ([[XC-213]]).

    Raises:
        PermissionDeniedError: Git declares the project, so removing it from the declaration comes first.
    """
    refuse_what_git_declares(record, obj=f"project:{project_id}", token=token)
    return record


ProjectToDeleteDep = Annotated[dict[str, str], Depends(project_its_doors_delete)]


@router.post("/{project_id}/protection", response_model_exclude_none=True)
async def set_project_protection(
    project_id: str,
    body: SetProtectionRequest,
    record: Annotated[dict[str, str], Depends(project_its_doors_protect)],
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
        project_id: The project to protect or clear.
        body: The protection to set.
        record: Its live record, its caller authorized on its creation (:func:`administered_project`).
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
    await protect_project(settings, control, project_id, uuid=record[UUID], change=change, actor=actor)
    return ProtectionResponse(id=project_id, protected=body.protected)


async def protect_project(settings: Settings, control: ControlEmitter, project_id: str, *, uuid: str, change: ProtectionChange, actor: str | None) -> None:
    """Arm or clear the deletion protection of project ``project_id``'s creation ``uuid`` and announce it.

    What the protection door does once its caller is authorized on that creation, and what the catalog does for a project
    whose protection drifted from what git declares ([[XC-213]]). The write is conditional on a read that finds that
    creation live (ADR 0245), so a change held across the project's delete lands on no project made again under its id.

    Raises:
        TableNotFoundError: The id no longer names that creation live.
    """
    protect = partial(project_registry.set_project_protection, settings.registry_root, settings.storage_options(), project_id, change, uuid=uuid)
    if await run_in_threadpool(protect) is None:
        raise TableNotFoundError(f"project not found: {project_id}")
    log.info("project_protection_set", extra={"project": project_id, "protected": change.protected})
    await emit_control(
        control,
        action="project_protected" if change.protected else "project_unprotected",
        object_type="project",
        object_id=f"project:{project_id}",
        actor=actor,
    )


def _warehouses_of(records: list[dict[str, str]], project: dict[str, str]) -> list[str]:
    """The ids of the warehouses among ``records``, in every state, made in the creation ``project`` names
    (``tenancy.warehouses_of``): one still creating counts (ADR 0245, ``CountsCreating``)."""
    return sorted(str(r["id"]) for r in warehouses_of(records, project[UUID]))


async def _require_deletable(project_id: str, record: dict[str, str], settings: Settings, *, force: bool) -> None:
    """The checks a delete runs before its mark, each refusing with nothing changed: its protection and its emptiness.

    Raises:
        NamespaceNotEmptyError: It is protected and ``force`` is false, or it still holds warehouses.
    """
    # Protection state and the warehouse listing below are both tenant DISCLOSURE, so they run only after
    # `project_to_delete` has proven the caller administers this tenant.
    fga_deps.require_not_protected(record, kind="project", obj_id=project_id, force=force)
    # A project's contents ARE its warehouse records — the same registry the listing derives tenants from,
    # so what the 409 names is exactly what GET /management/v1/projects/{id} shows. Read whole: a record that cannot be
    # read may be one of this project's own ([[LH-572]]).
    held = _warehouses_of(await run_in_threadpool(warehouses.every_warehouse, settings.registry_root, settings.storage_options()), record)
    if held:
        raise NamespaceNotEmptyError(
            f"project '{project_id}' still holds {len(held)} warehouse(s): {', '.join(held)}. "
            f"Delete each one first — DELETE /management/v1/warehouses/{{id}} (its own bucket purge is a separate "
            f"opt-in) — then delete the project. There is deliberately no cascade here: retiring a tenant "
            f"must never be able to destroy its buckets transitively in one request."
        )


@router.delete("/{project_id}")
async def delete_project(
    project_id: str,
    record: ProjectToDeleteDep,
    settings: SettingsDep,
    token: CurrentToken,
    client: FgaClientDep,
    announcer: AnnouncerDep,
    force: bool = False,
) -> DeleteProjectResponse:
    """Retire a tenant: refuse while it still holds warehouses, else revoke its tuples, end its roles, its maintenance
    policy and its label definitions, and end its record.

    Gated on ``project:<id>#can_administer``, the tenant's OWN admin bar: the rungs that open its reads, the estate
    observer's and membership, retire nothing (module docstring).

    **The order, and the point of no return** (ADR 0245, review T2 and T3). id shape (400) → existence and authz (both
    404, :func:`project_to_delete`) → the record read, the checks judged on that read, each refusing with nothing
    changed: deletion protection (409) and emptiness (409) → the MARK, written under that read's ETag (409 where the
    record changed since, nothing changed) → the warehouses read again (a warehouse whose record its create wrote before
    the mark, `ChildrenBeforeMark`); one found refuses 409 → the POINT OF NO RETURN stamped on the record → revoke its
    tuples → end its roles → end its maintenance policy → end its label definitions → END its record → audit + event.
    From the mark to the stamp nothing irreversible is done, so ANY failure there, a refusal or a store or OpenFGA not
    answering, puts the record back live (``tenancy.marked``); where that put-back fails too, the answer (503) says the
    project is left marked, and the converge tick puts it back once the delete's run is older than its bound. Past the
    stamp the delete either finishes or leaves the project marked, and the same delete asked again, or the converge tick,
    finishes it (503 ``partial``). A delete asked again over a mark short of its stamp is a new delete, judged on every
    check and on the caller's own authorization (``tenancy.judges``).

    **A delete acts only on the creation it marked.** Its tuples are its uuid's, its roles, policy and label definitions
    those naming its uuid, each removed under the ETag of a read that found it so, and its end is conditional on a read
    that found this delete's own mark: a delete taken over answers 409, and one held across its project's end and the id
    made again answers 404, ending nothing of the tenant that holds the id now.

    **A delete that failed part-way is finished by asking again** ([[LH-576]]): the record names the caller who started
    it, whose own admin grant the revoke takes, and :func:`project_to_delete` lets that caller, an admin the revoke has
    not reached, or the estate's owner finish it. From the mark on the project is absent to every other door, so no
    warehouse, role or grant is made for it meanwhile.

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
        record: Its registry record, its caller authorized on it (:func:`project_to_delete`).
        settings: The catalog's settings.
        token: The verified caller.
        client: The OpenFGA client its tuples are revoked through.
        announcer: Where what it announces is filed and sent.
        force: Delete it though it is protected.

    Returns:
        The retired project and how many tuples went with it.

    Raises:
        NamespaceNotEmptyError: It is protected and ``force`` is false, or it still holds warehouses; it is left live.
        ConcurrentModificationError: Its record changed while the delete was judged, nothing changed; or another delete
            of it took the delete over.
        TableNotFoundError: The id no longer names the creation the delete was asked of.
        InternalError: A warehouse record is there and cannot be read as one, and nothing was deleted.
        PartiallyApplied: A step past the point of no return failed; the same delete asked again finishes it.
    """
    return await remove_project(project_id, record, settings, token, client, announcer, force=force)


async def remove_project(
    project_id: str,
    record: dict[str, str],
    settings: Settings,
    token: CurrentToken,
    client: OpenFgaClient | None,
    announcer: Announcer,
    *,
    force: bool,
    finishing: str | None = None,
) -> DeleteProjectResponse:
    """The delete of project ``project_id`` whose record ``record`` is, once its caller is authorized (:func:`delete_project`).

    ``finishing`` is the converge tick's: the delete run its listing found past its point of no return, which the record
    read here must still carry, so the tick finishes that delete and never begins one (``tenancy.judged``).

    Raises:
        tenancy.NothingToFinish: ``finishing`` names a run the record no longer carries past its point of no return.
    """
    so = settings.storage_options()
    target = f"project:{project_id}"
    # Asked before the delete is recorded, so a deployment whose client is not wired refuses with nothing started.
    authz = fga_deps.wired(settings, client)
    obj = tenancy.object_of_record(PROJECT, record)
    delete = tenancy.DeleteRun.of(PROJECT, record)
    judged = await tenancy.judged(settings, delete, finishing=finishing)
    # Judged again over a mark short of its point of no return: taking it over is a new delete (``ChecksTakeOver``).
    if tenancy.judges(judged.record):
        await _require_deletable(project_id, judged.record, settings, force=force)
    async with tenancy.marked(settings, delete, judged, by=current_subject(token)) as marking:
        # Marked `deleting` from here (ADR 0245): no door resolves it, so nothing more is granted or made in it. A
        # warehouse whose create wrote its record before the mark is found by this read, which comes after it; one whose
        # record came after the mark finds the project gone when its create resolves it again, and ends itself. Either
        # way none stands under a project creation that ended (`ChildUnderLiveParent`). Nothing irreversible has been
        # done, so this refusal, or any failure before the stamp, puts the project back live.
        if late := _warehouses_of(await run_in_threadpool(warehouses.every_warehouse, settings.registry_root, so), record):
            raise NamespaceNotEmptyError(
                f"project '{project_id}' acquired warehouse(s) {', '.join(late)} while its delete began; delete each, then the project"
            )
        await marking.pass_no_return()
    actor = f"user:{token.subject}" if token is not None else None
    # Whom the revoke reaches is read and filed before it reaches them, so a failure after it still tells them ([[LH-576]]).
    grants = announcing.grant_changes(
        await fga.read_object_tuples(authz, obj) if authz is not None else [], action="grant_revoked", actor=actor, reason="project_deleted"
    )
    deleted_event = CatalogControlEvent(action="project_deleted", object_type="project", object_id=target, actor=actor, extra={UUID: record[UUID]})
    deleted = Announcement(evidence=RecordGone(key=project_registry.record_key(project_id)), message=ControlMessage(event=deleted_event))
    async with announcer.announcing(door="delete_project", target=target, announcements=[deleted, *grants.values()]) as filing:
        try:
            removed = await _end_the_creation(delete, record, settings, token, authz, announcer)
        except (tenancy.DeleteGone, tenancy.DeleteTakenOver):
            raise
        except Exception as exc:
            # Past the point of no return the project stays marked, absent to every door, and the same delete asked
            # again finishes it: every step is idempotent and acts on this creation alone.
            raise PartiallyApplied(
                f"project {project_id!r} is past its point of no return and was PARTIALLY deleted; re-issue this call to finish it.",
                problem_extra={"project": project_id, "partial": True},
            ) from exc
        log.info("project_deleted", extra={"project": project_id, "tuples_revoked": len(removed), "forced": force})
        if authz is not None:
            # Mirrors create_project's grant audit, and only when the revoke really ran: an FGA-off stack must
            # not fabricate a compliance record for a revocation that never happened. The revoke is
            # subject-agnostic by design (it deletes EVERY tuple on the object), so the grantee is the wildcard
            # and `removed` carries the magnitude.
            audit(
                AuditAction.ACCESS_REVOKE,
                AuditOutcome.SUCCESS,
                subject=actor,
                resource=obj,
                project=project_id,
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
        await filing.send_grant_changes(grants, removed, action="grant_revoked", actor=actor, reason="project_deleted")
    return DeleteProjectResponse(project=project_id, tuples_revoked=len(removed))


async def _end_the_creation(
    delete: tenancy.DeleteRun, record: dict[str, str], settings: Settings, token: CurrentToken, authz: OpenFgaClient | None, announcer: Announcer
) -> list[fga.ClientTuple]:
    """A delete's irreversible steps, past its point of no return, each acting on the creation it marked alone (its uuid's
    tuples, the roles, policy and label definitions naming its uuid, each removed under the ETag of a read that found it
    so), and its end conditional on a read that found this delete's own mark; the tuples revoked.

    Raises:
        tenancy.DeleteGone: The id no longer names the creation.
        tenancy.DeleteTakenOver: Another delete of it took the delete over.
    """
    so = settings.storage_options()
    actor = f"user:{token.subject}" if token is not None else None
    project_uuid = delete.uuid
    # Tuples FIRST, record last. A revoke that fails (OpenFGA outage → 503) leaves the tenant fully described and
    # re-deletable; the opposite order would strand grants on a project no API can name any more — privilege that nothing
    # in the product can see, let alone clear. Every tuple on its creation and every tuple naming it (a role's `project`
    # edge, a warehouse's tenancy edge). A door that wrote on it after this read wrote on a dead object nothing resolves
    # to, which the converge tick sweeps once the creation is ended (ADR 0245).
    removed = await fga_deps.end_object(authz, settings, tenancy.object_of_record(PROJECT, record), token=token)
    # Each role made under this creation is ended as its own delete door ends it, with authorization off too, so no record
    # outlives its project; a role of a creation made again under the id is that tenant's.
    for role in await run_in_threadpool(partial(roles.every_role, settings.registry_root, so, project_uuid=project_uuid)):
        ending = await announcing.role_ending(authz, role, actor=actor, reason="project_deleted")
        async with announcer.announcing(door="end_role", target=roles.role_object(role.id), announcements=ending.announcements()) as ending_filed:
            ended = await role_grants.end_role(authz, role, control_root=settings.registry_root, storage_options=so, actor=current_subject(token))
            await ending.send(ending_filed, ended, actor=actor, reason="project_deleted")
    # Its maintenance policy goes before its record ([[LH-562]]): a project created again under the id is another tenant.
    # Every record of a project is filed under its creation's uuid (ADR 0245, third review), so this delete ends its own
    # creation's keyspace and reaches nothing another creation of the id holds.
    await run_in_threadpool(partial(maintenance_policies.end_project_policy, settings.registry_root, so, project_uuid=project_uuid))
    # Its label definitions too, for the same reason (ADR 0211). No object of it carries one by now: every object lay in a
    # warehouse of it, and each ended its labels as it went.
    await run_in_threadpool(partial(object_labels.end_definitions, settings.registry_root, so, project_uuid=project_uuid))
    await tenancy.end(settings, delete)
    return removed
