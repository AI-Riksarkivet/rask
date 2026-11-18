"""A project's roles: mint, list, read, end, and decide who is in one ([[LH-557]]).

A role is the estate's one group: ``role:<uuid>#assignee`` is a subject every grant door takes, so access is granted to
a persona rather than person by person, and a grant to a role reaches each of its assignees (the model resolves it).
A role lives under its project, the tenant the model scopes it to, and what a role is granted goes through the
per-object grant doors as any subject's does.

Who may do what is the model's (``model.fga``): ``can_create_role`` (the project's ``role_creator``), ``can_list_roles``
and a role's ``can_read`` (the project's members and security admins), and a role's ``can_manage_assignees`` and
``can_delete`` (the project's ``security_admin``). A refusal answers the missing project's or role's 404, so no door
tells a caller which ids exist (the rule the project and warehouse doors keep). A name is judged before anything is
asked, since a malformed name tells nothing about the project.

An assignee is a principal or another standing role of the same project; anything else is refused 400 (code 13). Three
rules keep a change of who is in a role from raising anyone's access past what its caller could grant:

* No assignment raises the caller's own access: assigning themselves, or a role they are in, is refused 403 unless they
  already hold the role (:func:`fga_deps.refuse_self_elevation`, the grant doors' own separation of duties).
* While a project's ``admin`` reaches the role, held by it or by a role it is nested beneath however deep, changing who
  is in it asks ``can_grant_admin`` on that project, the rung the members door asks to make an admin, so a security
  admin makes and unmakes no admin through a role ([[LH-597]]). Ending such a role asks it too.
* No change empties the chain a project's ``admin`` reaches its people through: removing the last person it reaches,
  or ending a role it reaches them through, is refused 409 (code 14), as the members door keeps a project administered
  ([[LH-434]]).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, NamedTuple

from fastapi import APIRouter, status
from lance_namespace import InvalidInputError, NamespaceAlreadyExistsError, PermissionDeniedError, TableNotFoundError
from starlette.concurrency import run_in_threadpool

from catalog.api import announcing, fga_deps
from catalog.api.announcing import Announcer, AnnouncerDep
from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, SettingsDep
from catalog.api.security import CurrentToken, current_subject
from catalog.api.v1.endpoints.members import ProjectId
from catalog.core.identifiers import require_names
from catalog.schemas import AssigneeRequest, Role, RoleCreateRequest, RoleDeleted, RoleList, RoleRecord
from catalog.services import project_admins, role_grants, roles, tenancy
from catalog.services.tenancy import Tenant
from lakehouse.control_emit import emit_control
from lakehouse.governed import fga
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit
from lakehouse.governed.tenancy import PROJECT
from lakehouse.records import RecordExistsError


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient

    from catalog.api.security import Caller
    from catalog.core.config import Settings


log = logging.getLogger(__name__)

router = APIRouter(prefix="/management/v1/projects", tags=["roles"])


async def _project_gate(
    *, client: OpenFgaClient | None, settings: Settings, token: Caller | None, project_id: str, relation: str
) -> tuple[OpenFgaClient, Tenant]:
    """The client and the live creation of ``project_id`` (ADR 0245), once the caller holds ``relation`` on it; else the
    missing project's 404."""

    async def gate(obj: str, label: str) -> None:
        try:
            await fga_deps.require_relation(client, settings, token, relation=relation, obj=obj, label=label)
        except PermissionDeniedError as exc:
            raise TableNotFoundError(f"project not found: {project_id}") from exc

    project = await fga_deps.on_tenant(settings, PROJECT, project_id, gate=gate)
    return fga_deps.require_fga(settings, client, feature="roles"), project


class _Gated(NamedTuple):
    """What a role door acts with once its gate admitted the caller: the client, the role, and the live creation of its
    project the role belongs to, which the door resolves again once it has written (``tenancy.require_still``)."""

    client: OpenFgaClient
    role: RoleRecord
    project: Tenant


async def _role_gate(*, client: OpenFgaClient | None, settings: Settings, token: Caller | None, project_id: str, role_id: uuid.UUID, relation: str) -> _Gated:
    """The client, the role and its project's live creation, once the caller holds ``relation`` on standing role
    ``role_id`` of the live creation of ``project_id``; else the missing role's 404. A role a create left across an
    earlier creation's delete is no role of this one (ADR 0245)."""
    missing = TableNotFoundError(f"role not found: {role_id}")
    try:
        await fga_deps.require_relation(client, settings, token, relation=relation, obj=roles.role_object(role_id))
    except PermissionDeniedError as exc:
        raise missing from exc
    if (project := await tenancy.resolved(settings, PROJECT, project_id)) is None:
        raise missing
    so = settings.storage_options()
    if (role := await run_in_threadpool(partial(roles.find_role, settings.registry_root, so, project_id, role_id, project_uuid=project.uuid))) is None:
        raise missing
    return _Gated(fga_deps.require_fga(settings, client, feature="roles"), role, project)


def _announced_actor(token: Caller | None) -> str | None:
    return f"user:{token.subject}" if token is not None else None


async def _assignees(client: OpenFgaClient, role_id: uuid.UUID) -> list[str]:
    obj = roles.role_object(role_id)
    return sorted(t.user for t in await fga.read_object_tuples(client, obj) if t.object == obj and t.relation == roles.ASSIGNEE)


@router.post("/{project_id}/roles", status_code=status.HTTP_201_CREATED)
async def create_role(
    project_id: ProjectId, body: RoleCreateRequest, client: FgaClientDep, control: ControlEmitterDep, settings: SettingsDep, token: CurrentToken
) -> Role:
    """Mint a role in the project, gated on ``can_create_role``: its record, then its ``project`` edge.

    A name a standing role of the project holds is refused 409 (code 2). The role starts with no assignee: filling it is
    the security admins' (``can_manage_assignees``), a different authority from minting it.

    The project's id is resolved to its live creation, and the gate asked there (ADR 0245). The record names that
    creation and is written first; the project is then resolved again, and one deleted meanwhile is answered as deleted
    with the record ended here. The project's delete marks the project before it ends the roles its registry lists, so
    either that listing finds this record or this resolve finds the project gone. The edge names the creation, so one
    written after the project's delete stands on a dead creation, which the converge tick sweeps.
    \f

    Raises:
        InvalidInputError: The name is not a name (ADR 0173 §4).
        TableNotFoundError: The project is absent, its caller may not create a role in it, or it was deleted while the
            role was made.
        NamespaceAlreadyExistsError: A standing role of the project holds the name.
    """
    require_names([body.name])
    wired, project = await _project_gate(client=client, settings=settings, token=token, project_id=project_id, relation="can_create_role")
    record = await _mint_role(wired, settings, project, body, actor=current_subject(token))
    obj = roles.role_object(record.id)
    actor = current_subject(token)
    audit(AuditAction.ROLE_CREATE, AuditOutcome.SUCCESS, subject=actor, resource=obj, project=project_id, name=record.name)
    await emit_control(
        control,
        action="role_created",
        object_type="role",
        object_id=obj,
        actor=_announced_actor(token),
        extra={"project": project_id, "name": record.name},
    )
    return Role(**record.model_dump(), assignees=[])


async def _mint_role(wired: OpenFgaClient, settings: Settings, project: Tenant, body: RoleCreateRequest, *, actor: str) -> RoleRecord:
    """The role's record naming ``project``'s creation, the project resolved again, then its ``project`` edge; the record.

    Raises:
        TableNotFoundError: The project was deleted while the role was made, before or after its edge; its record is ended.
    """
    record = RoleRecord(id=uuid.uuid4(), project=project.id, name=body.name, description=body.description, created_at=datetime.now(UTC), created_by=actor)
    so = settings.storage_options()
    try:
        await run_in_threadpool(partial(roles.create_role_record, settings.registry_root, so, record, project_uuid=str(project.uuid)))
    except RecordExistsError:
        raise NamespaceAlreadyExistsError(f"project {project.id!r} already holds a role named {body.name!r}") from None
    if not await tenancy.still(settings, project):
        await run_in_threadpool(roles.end_role_record, settings.registry_root, so, record)
        raise TableNotFoundError(f"project not found: {project.id} (it was deleted while this role was made)")
    edge = fga.ClientTuple(user=project.obj, relation="project", object=roles.role_object(record.id))
    await fga.write_tuples(wired, [edge], actor=actor, origin="create")  # noqa: TID251
    # Resolved again once the edge is written, as every door writing a tenancy tuple is: a project deleted meanwhile ended
    # the roles its registry listed, and this one's edge stands on the dead creation, which the converge tick sweeps.
    try:
        await tenancy.require_still(settings, project)
    except TableNotFoundError:
        await run_in_threadpool(roles.end_role_record, settings.registry_root, so, record)
        raise
    return record


@router.get("/{project_id}/roles")
async def list_roles(project_id: ProjectId, client: FgaClientDep, settings: SettingsDep, token: CurrentToken) -> RoleList:
    """The project's standing roles, ordered by name, gated on ``can_list_roles``; a role's assignees are its own read. A
    role made in an earlier creation of the project is no role of it (ADR 0245)."""
    _, project = await _project_gate(client=client, settings=settings, token=token, project_id=project_id, relation="can_list_roles")
    records = await run_in_threadpool(partial(roles.list_roles, settings.registry_root, settings.storage_options(), project_id, project_uuid=str(project.uuid)))
    return RoleList(roles=[Role(**record.model_dump()) for record in records])


@router.get("/{project_id}/roles/{role_id}")
async def get_role(project_id: ProjectId, role_id: uuid.UUID, client: FgaClientDep, settings: SettingsDep, token: CurrentToken) -> Role:
    """One role and its direct assignees, gated on the role's ``can_read``."""
    gated = await _role_gate(client=client, settings=settings, token=token, project_id=project_id, role_id=role_id, relation="can_read")
    return Role(**gated.role.model_dump(), assignees=await _assignees(gated.client, gated.role.id))


@router.delete("/{project_id}/roles/{role_id}")
async def delete_role(
    project_id: ProjectId, role_id: uuid.UUID, client: FgaClientDep, announcer: AnnouncerDep, settings: SettingsDep, token: CurrentToken
) -> RoleDeleted:
    """End a role, gated on its ``can_delete``, and on the project's ``can_grant_admin`` while the project's ``admin``
    reaches the role: every tuple naming it and on it, and its record (:func:`role_grants.end_role`).

    Refused 409 (code 14) while ending it would leave a project whose ``admin`` reaches nobody, before anything is revoked. An
    ending that fails part-way leaves the role listed and deletable, so calling the door again finishes it, and what it
    took is announced though the ending failed after it ([[LH-576]]).
    \f

    Raises:
        TableNotFoundError: The role is absent, or its caller may not delete it, or its project was deleted while it ran
            (resolved again once the role is ended, ADR 0245).
        ConcurrentModificationError: Ending it would strand a project.
    """
    wired, role, project = await _role_gate(client=client, settings=settings, token=token, project_id=project_id, role_id=role_id, relation="can_delete")
    actor, announced = current_subject(token), _announced_actor(token)
    ending = await announcing.role_ending(wired, role, actor=announced, reason="role_deleted")
    async with announcer.announcing(door="delete_role", target=roles.role_object(role.id), announcements=ending.announcements()) as filing:
        removed = await role_grants.end_role(
            wired,
            role,
            control_root=settings.registry_root,
            storage_options=settings.storage_options(),
            actor=actor,
            permit=_admin_grant(client=wired, settings=settings, token=token, role=role),
        )
        await tenancy.require_still(settings, project)
        audit(AuditAction.ROLE_DELETE, AuditOutcome.SUCCESS, subject=actor, resource=roles.role_object(role.id), project=project_id, removed=len(removed))
        await ending.send(filing, removed, actor=announced, reason="role_deleted")
    return RoleDeleted(role=role, tuples_revoked=len(removed))


async def _assignee(*, settings: Settings, role: RoleRecord, raw: str) -> str:
    """The OpenFGA user ``raw`` names as an assignee of ``role``: a principal, or another standing role of its project."""
    user = fga.grantee_object(raw)
    if user.startswith("user:"):
        return user
    nested, _, relation = user.partition("#")
    if not nested.startswith("role:") or relation != roles.ASSIGNEE:
        raise InvalidInputError(f"{raw!r} is neither a principal nor a role's assignees ('role:<id>#assignee')")
    nested_id = nested.removeprefix("role:")
    if nested_id == str(role.id):
        raise InvalidInputError(f"role {role.name!r} cannot be its own assignee")
    project = await tenancy.resolved(settings, PROJECT, role.project)
    if (
        project is None
        or await run_in_threadpool(
            partial(roles.find_role, settings.registry_root, settings.storage_options(), role.project, nested_id, project_uuid=project.uuid)
        )
        is None
    ):
        raise InvalidInputError(f"{raw!r} is not a role of project {role.project!r}")
    return user


def _admin_grant(*, client: OpenFgaClient, settings: Settings, token: Caller | None, role: RoleRecord) -> project_admins.Permit:
    """Asks ``can_grant_admin`` on each project whose ``admin`` reaches ``role``; a refusal answers the missing role's
    404, as the role's own gate does."""

    async def permit(projects: list[str]) -> None:
        try:
            for project in projects:
                await fga_deps.require_relation(client, settings, token, relation="can_grant_admin", obj=project)
        except PermissionDeniedError as exc:
            raise TableNotFoundError(f"role not found: {role.id}") from exc

    return permit


@router.put("/{project_id}/roles/{role_id}/assignees")
async def assign_role(
    project_id: ProjectId,
    role_id: uuid.UUID,
    body: AssigneeRequest,
    client: FgaClientDep,
    announcer: AnnouncerDep,
    settings: SettingsDep,
    token: CurrentToken,
) -> Role:
    """Assign one principal or standing role of the project to the role, gated on its ``can_manage_assignees``, and on
    the project's ``can_grant_admin`` while the project's ``admin`` reaches the role. Idempotent.

    The assignment is one Write (:func:`project_admins.confer`), judged on the read that found whether ``admin`` reaches
    the role. A role is named by its uuid and a project by its creation's (ADR 0245), so an assignment decided on a
    project deleted meanwhile lands on that dead creation's ended role, in no project made again under its id.
    \f

    Raises:
        TableNotFoundError: The role is absent, or its caller may not manage its assignees.
        InvalidInputError: The assignee is the role itself, neither a principal nor a role, or no standing role of the project.
        PermissionDeniedError: The assignment would raise the caller's own access.
    """
    gated = await _role_gate(client=client, settings=settings, token=token, project_id=project_id, role_id=role_id, relation="can_manage_assignees")
    return await _assign(gated, announcer, settings, token, body)


async def _assign(gated: _Gated, announcer: Announcer, settings: Settings, token: Caller | None, body: AssigneeRequest) -> Role:
    """The assignment once its caller is authorized; the project resolved again once it is written (ADR 0245)."""
    wired, role, project = gated
    user = await _assignee(settings=settings, role=role, raw=body.user)
    obj = roles.role_object(role.id)
    if token is not None:
        await fga_deps.refuse_self_elevation(wired, user=token.subject, grantee=user, relation=roles.ASSIGNEE, obj=obj)
    actor = current_subject(token)
    tup = fga.ClientTuple(user=user, relation=roles.ASSIGNEE, object=obj)
    told = announcing.grant_announcement(tup, action="grant_added", actor=_announced_actor(token))
    permit = _admin_grant(client=wired, settings=settings, token=token, role=role)
    async with announcer.announcing(door="role_assign", target=obj, announcements=[told]) as filing:
        written = await project_admins.confer(wired, tup, actor=actor, origin="grant_api", permit=permit)
        await tenancy.require_still(settings, project)
        if written:
            audit(AuditAction.ROLE_ASSIGN, AuditOutcome.SUCCESS, subject=actor, resource=obj, project=project.id, grantee=user)
            await filing.send(told)
    return Role(**role.model_dump(), assignees=await _assignees(wired, role.id))


@router.delete("/{project_id}/roles/{role_id}/assignees")
async def unassign_role(
    project_id: ProjectId,
    role_id: uuid.UUID,
    body: AssigneeRequest,
    client: FgaClientDep,
    announcer: AnnouncerDep,
    settings: SettingsDep,
    token: CurrentToken,
) -> Role:
    """Remove one assignee from the role, gated as an assignment is; removing one it does not hold is a no-op.

    Refused 409 (code 14) when the project's ``admin`` would reach nobody without it
    (:func:`project_admins.withdraw`, arbitrated by the store as the members door's revoke is).
    \f

    Raises:
        TableNotFoundError: The role is absent, or its caller may not manage its assignees.
        ConcurrentModificationError: The removal would leave the project nobody administers.
    """
    gated = await _role_gate(client=client, settings=settings, token=token, project_id=project_id, role_id=role_id, relation="can_manage_assignees")
    return await _unassign(gated, announcer, settings, token, body)


async def _unassign(gated: _Gated, announcer: Announcer, settings: Settings, token: Caller | None, body: AssigneeRequest) -> Role:
    """The removal of an assignee once its caller is authorized; the project resolved again once it is written (ADR 0245)."""
    wired, role, project = gated
    user = fga.grantee_object(body.user)
    obj = roles.role_object(role.id)
    actor = current_subject(token)
    tup = fga.ClientTuple(user=user, relation=roles.ASSIGNEE, object=obj)
    told = announcing.grant_announcement(tup, action="grant_revoked", actor=_announced_actor(token))
    permit = _admin_grant(client=wired, settings=settings, token=token, role=role)
    async with announcer.announcing(door="role_unassign", target=obj, announcements=[told]) as filing:
        removed = bool(await project_admins.withdraw(wired, [tup], actor=actor, origin="grant_api", permit=permit))
        await tenancy.require_still(settings, project)
        if removed:
            audit(AuditAction.ROLE_UNASSIGN, AuditOutcome.SUCCESS, subject=actor, resource=obj, project=project.id, grantee=user)
            await filing.send(told)
    return Role(**role.model_dump(), assignees=await _assignees(wired, role.id))
