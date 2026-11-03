"""Who is in a role, and removing one of them without leaving its project with no direct admin ([[LH-557]]).

A role that holds its project's ``admin`` is part of who administers the project, so emptying it is a change to that set,
arbitrated as the members door's revoke is: the removal moves the project's ``admin_revision`` in the same Write
(:func:`project_admins.next_revision`), so it and any other change to the project's admins, each judged on its own read,
cannot both commit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lance_namespace import ConcurrentModificationError

from catalog.services import project_admins
from catalog.services.roles import ASSIGNEE, assignees_of, role_object
from service_kit.governed import fga


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient


async def role_project(client: OpenFgaClient, role: str) -> tuple[str | None, set[str]]:
    """The project ``role``'s edge names (``project:<id>``), and its direct assignees."""
    tuples = [t for t in await fga.read_object_tuples(client, role) if t.object == role]
    project = next((t.user for t in tuples if t.relation == "project" and t.user.startswith("project:")), None)
    return project, {t.user for t in tuples if t.relation == ASSIGNEE}


def holds_admin(project_tuples: list[fga.ClientTuple], project: str, subject: str) -> bool:
    """Whether ``subject`` holds a direct ``admin`` on ``project`` among ``project_tuples``."""
    return any(t.object == project and t.relation == project_admins.ADMIN and t.user == subject for t in project_tuples)


async def unassign(client: OpenFgaClient, role_id: str, user: str, *, actor: str, origin: fga.TupleOrigin) -> bool:
    """Remove ``user`` from role ``role_id``'s direct assignees; whether this call removed it.

    Raises:
        ConcurrentModificationError: ``user`` is the last direct assignee of a role that is its project's only direct
            admin, or the project's admins changed under both reads.
    """
    role, subject = role_object(role_id), assignees_of(role_id)
    target = fga.ClientTuple(user=user, relation=ASSIGNEE, object=role)
    for _ in range(project_admins.ADMIN_REVOKE_READS):
        project, assigned = await role_project(client, role)
        if user not in assigned:
            return False
        if project is None:
            return await _remove(client, target, actor=actor, origin=origin)
        project_tuples = await fga.read_object_tuples(client, project)
        if not holds_admin(project_tuples, project, subject):
            return await _remove(client, target, actor=actor, origin=origin)
        if assigned == {user} and project_admins.sole_admin(project_tuples, project, subject):
            raise ConcurrentModificationError(f"{user} is the last assignee of {role}, the only direct admin of {project} — assign another first")
        moved, stale = project_admins.next_revision(project_tuples, project)
        if await fga.commit_unless_changed(client, deletes=[target, *stale], writes=[moved], actor=actor, origin=origin):  # noqa: TID251
            return True
    raise ConcurrentModificationError(f"the admins of the project {role} belongs to changed while this removal ran, twice — read them again and retry")


async def _remove(client: OpenFgaClient, target: fga.ClientTuple, *, actor: str, origin: fga.TupleOrigin) -> bool:
    """Delete one assignment of a role that holds no project's admin, which strands nothing; always removed."""
    await fga.delete_tuples(client, [target], actor=actor, origin=origin)  # noqa: TID251
    return True
