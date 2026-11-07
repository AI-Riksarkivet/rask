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
* While the role holds its project's ``admin``, changing who is in it asks ``can_grant_admin`` on the project, the rung
  the members door asks to make an admin, so a security admin cannot make admins through a role.
* A role that is its project's only direct admin keeps an assignee, and is not ended (409, code 14), as the members door
  keeps a project's last admin ([[LH-434]]).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, status
from lance_namespace import InvalidInputError, NamespaceAlreadyExistsError, PermissionDeniedError, TableNotFoundError
from starlette.concurrency import run_in_threadpool

from catalog.api import fga_deps
from catalog.api.dependencies import ControlEmitterDep, FgaClientDep, SettingsDep
from catalog.api.security import CurrentToken, current_subject
from catalog.api.v1.endpoints.members import ProjectId
from catalog.core.identifiers import require_names
from catalog.schemas import AssigneeRequest, Role, RoleCreateRequest, RoleDeleted, RoleList, RoleRecord
from catalog.services import projects as project_registry
from catalog.services import role_grants, role_membership, roles
from lakehouse.control_emit import emit_control
from lakehouse.governed import fga
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit
from lakehouse.records import RecordExistsError


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient

    from catalog.api.security import Caller
    from catalog.core.config import Settings


log = logging.getLogger(__name__)

router = APIRouter(prefix="/management/v1/projects", tags=["roles"])


async def _project_gate(*, client: OpenFgaClient | None, settings: Settings, token: Caller | None, project_id: str, relation: str) -> OpenFgaClient:
    """The client, once the caller holds ``relation`` on an existing ``project_id``; else the missing project's 404."""
    try:
        await fga_deps.require_relation(client, settings, token, relation=relation, obj=f"project:{project_id}")
    except PermissionDeniedError as exc:
        raise TableNotFoundError(f"project not found: {project_id}") from exc
    if await run_in_threadpool(project_registry.get_project, settings.registry_root, settings.storage_options(), project_id) is None:
        raise TableNotFoundError(f"project not found: {project_id}")
    return fga_deps.require_fga(settings, client, feature="roles")


async def _role_gate(
    *, client: OpenFgaClient | None, settings: Settings, token: Caller | None, project_id: str, role_id: uuid.UUID, relation: str
) -> tuple[OpenFgaClient, RoleRecord]:
    """The client and the role, once the caller holds ``relation`` on standing role ``role_id`` of ``project_id``; else
    the missing role's 404."""
    missing = TableNotFoundError(f"role not found: {role_id}")
    try:
        await fga_deps.require_relation(client, settings, token, relation=relation, obj=roles.role_object(role_id))
    except PermissionDeniedError as exc:
        raise missing from exc
    if (role := await run_in_threadpool(roles.find_role, settings.registry_root, settings.storage_options(), project_id, role_id)) is None:
        raise missing
    return fga_deps.require_fga(settings, client, feature="roles"), role


def _announced_actor(token: Caller | None) -> str | None:
    return f"user:{token.subject}" if token is not None else None


async def _assignees(client: OpenFgaClient, role_id: uuid.UUID) -> list[str]:
    obj = roles.role_object(role_id)
    return sorted(t.user for t in await fga.read_object_tuples(client, obj) if t.object == obj and t.relation == roles.ASSIGNEE)


@router.post("/{project_id}/roles", status_code=status.HTTP_201_CREATED)
async def create_role(
    project_id: ProjectId, body: RoleCreateRequest, client: FgaClientDep, control: ControlEmitterDep, settings: SettingsDep, token: CurrentToken
) -> Role:
    """Mint a role in the project, gated on ``can_create_role``: its ``project`` edge, then its record.

    A name a standing role of the project holds is refused 409 (code 2). The edge is written first, and removed again
    when the record cannot be written, so a role is listed only once it can be governed; an edge left by a request that
    died between the two names a uuid no record holds, which grants nothing. The role starts with no assignee: filling
    it is the security admins' (``can_manage_assignees``), a different authority from minting it.
    \f

    Raises:
        InvalidInputError: The name is not a name (ADR 0173 §4).
        TableNotFoundError: The project is absent, or its caller may not create a role in it.
        NamespaceAlreadyExistsError: A standing role of the project holds the name.
    """
    require_names([body.name])
    wired = await _project_gate(client=client, settings=settings, token=token, project_id=project_id, relation="can_create_role")
    actor = current_subject(token)
    record = RoleRecord(id=uuid.uuid4(), project=project_id, name=body.name, description=body.description, created_at=datetime.now(UTC), created_by=actor)
    obj = roles.role_object(record.id)
    edge = fga.ClientTuple(user=f"project:{project_id}", relation="project", object=obj)
    await fga.write_tuples(wired, [edge], actor=actor, origin="create")  # noqa: TID251
    try:
        await run_in_threadpool(roles.create_role_record, settings.registry_root, settings.storage_options(), record)
    except RecordExistsError:
        await fga.delete_tuples(wired, [edge], actor=actor, origin="create")  # noqa: TID251
        raise NamespaceAlreadyExistsError(f"project {project_id!r} already holds a role named {body.name!r}") from None
    except BaseException:
        await fga.delete_tuples(wired, [edge], actor=actor, origin="create")  # noqa: TID251
        raise
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


@router.get("/{project_id}/roles")
async def list_roles(project_id: ProjectId, client: FgaClientDep, settings: SettingsDep, token: CurrentToken) -> RoleList:
    """The project's standing roles, ordered by name, gated on ``can_list_roles``; a role's assignees are its own read."""
    await _project_gate(client=client, settings=settings, token=token, project_id=project_id, relation="can_list_roles")
    records = await run_in_threadpool(roles.list_roles, settings.registry_root, settings.storage_options(), project_id)
    return RoleList(roles=[Role(**record.model_dump()) for record in records])


@router.get("/{project_id}/roles/{role_id}")
async def get_role(project_id: ProjectId, role_id: uuid.UUID, client: FgaClientDep, settings: SettingsDep, token: CurrentToken) -> Role:
    """One role and its direct assignees, gated on the role's ``can_read``."""
    wired, role = await _role_gate(client=client, settings=settings, token=token, project_id=project_id, role_id=role_id, relation="can_read")
    return Role(**role.model_dump(), assignees=await _assignees(wired, role.id))


@router.delete("/{project_id}/roles/{role_id}")
async def delete_role(
    project_id: ProjectId, role_id: uuid.UUID, client: FgaClientDep, control: ControlEmitterDep, settings: SettingsDep, token: CurrentToken
) -> RoleDeleted:
    """End a role, gated on its ``can_delete``: every tuple naming it and on it, and its record (:func:`role_grants.end_role`).

    Refused 409 (code 14) while ending it would leave a project with no direct admin, before anything is revoked. An
    ending that fails part-way leaves the role listed and deletable, so calling the door again finishes it.
    \f

    Raises:
        TableNotFoundError: The role is absent, or its caller may not delete it.
        ConcurrentModificationError: Ending it would strand a project.
    """
    wired, role = await _role_gate(client=client, settings=settings, token=token, project_id=project_id, role_id=role_id, relation="can_delete")
    actor = current_subject(token)
    removed = await role_grants.end_role(wired, role, control_root=settings.registry_root, storage_options=settings.storage_options(), actor=actor)
    audit(AuditAction.ROLE_DELETE, AuditOutcome.SUCCESS, subject=actor, resource=roles.role_object(role.id), project=project_id, removed=len(removed))
    await role_grants.announce_ended(control, role, removed, actor=_announced_actor(token), reason="role_deleted")
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
    if await run_in_threadpool(roles.find_role, settings.registry_root, settings.storage_options(), role.project, nested_id) is None:
        raise InvalidInputError(f"{raw!r} is not a role of project {role.project!r}")
    return user


async def _require_admin_grant(*, client: OpenFgaClient, settings: Settings, token: Caller | None, role: RoleRecord) -> None:
    """Ask ``can_grant_admin`` on ``role``'s project while the role holds that project's ``admin``; a refusal answers the
    missing role's 404, as the role's own gate does."""
    project = f"project:{role.project}"
    if not role_membership.holds_admin(await fga.read_object_tuples(client, project), project, roles.assignees_of(role.id)):
        return
    try:
        await fga_deps.require_relation(client, settings, token, relation="can_grant_admin", obj=project)
    except PermissionDeniedError as exc:
        raise TableNotFoundError(f"role not found: {role.id}") from exc


@router.put("/{project_id}/roles/{role_id}/assignees")
async def assign_role(
    project_id: ProjectId,
    role_id: uuid.UUID,
    body: AssigneeRequest,
    client: FgaClientDep,
    control: ControlEmitterDep,
    settings: SettingsDep,
    token: CurrentToken,
) -> Role:
    """Assign one principal or standing role of the project to the role, gated on its ``can_manage_assignees``, and on
    the project's ``can_grant_admin`` while the role holds the project's ``admin``. Idempotent.
    \f

    Raises:
        TableNotFoundError: The role is absent, or its caller may not manage its assignees.
        InvalidInputError: The assignee is the role itself, neither a principal nor a role, or no standing role of the project.
        PermissionDeniedError: The assignment would raise the caller's own access.
    """
    wired, role = await _role_gate(client=client, settings=settings, token=token, project_id=project_id, role_id=role_id, relation="can_manage_assignees")
    await _require_admin_grant(client=wired, settings=settings, token=token, role=role)
    user = await _assignee(settings=settings, role=role, raw=body.user)
    obj = roles.role_object(role.id)
    if token is not None:
        await fga_deps.refuse_self_elevation(wired, user=token.subject, grantee=user, relation=roles.ASSIGNEE, obj=obj)
    actor = current_subject(token)
    if user not in await _assignees(wired, role.id):
        await fga.write_tuples(wired, [fga.ClientTuple(user=user, relation=roles.ASSIGNEE, object=obj)], actor=actor, origin="grant_api")  # noqa: TID251
        audit(AuditAction.ROLE_ASSIGN, AuditOutcome.SUCCESS, subject=actor, resource=obj, project=project_id, grantee=user)
        await emit_control(
            control,
            action="grant_added",
            object_type="grant",
            object_id=obj,
            actor=_announced_actor(token),
            extra={"relation": roles.ASSIGNEE, "subject": user},
        )
    return Role(**role.model_dump(), assignees=await _assignees(wired, role.id))


@router.delete("/{project_id}/roles/{role_id}/assignees")
async def unassign_role(
    project_id: ProjectId,
    role_id: uuid.UUID,
    body: AssigneeRequest,
    client: FgaClientDep,
    control: ControlEmitterDep,
    settings: SettingsDep,
    token: CurrentToken,
) -> Role:
    """Remove one assignee from the role, gated as an assignment is; removing one it does not hold is a no-op.

    Refused 409 (code 14) for the last assignee of a role that is its project's only direct admin
    (:func:`role_membership.unassign`, arbitrated by the store as the members door's revoke is).
    \f

    Raises:
        TableNotFoundError: The role is absent, or its caller may not manage its assignees.
        ConcurrentModificationError: The removal would leave the project with no direct admin.
    """
    wired, role = await _role_gate(client=client, settings=settings, token=token, project_id=project_id, role_id=role_id, relation="can_manage_assignees")
    await _require_admin_grant(client=wired, settings=settings, token=token, role=role)
    user = fga.grantee_object(body.user)
    obj = roles.role_object(role.id)
    actor = current_subject(token)
    if await role_membership.unassign(wired, str(role.id), user, actor=actor, origin="grant_api"):
        audit(AuditAction.ROLE_UNASSIGN, AuditOutcome.SUCCESS, subject=actor, resource=obj, project=project_id, grantee=user)
        await emit_control(
            control,
            action="grant_revoked",
            object_type="grant",
            object_id=obj,
            actor=_announced_actor(token),
            extra={"relation": roles.ASSIGNEE, "subject": user},
        )
    return Role(**role.model_dump(), assignees=await _assignees(wired, role.id))
