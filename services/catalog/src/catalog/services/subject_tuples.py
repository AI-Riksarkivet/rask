"""Every direct tuple naming one subject, and how a door ending a subject revokes them without stranding a project.

OpenFGA's Read takes a user only beside an object type ("the object type field is required", v1.18.3), so a reader of
everything granted to a subject asks each type the model lets hold it directly: a principal (``user:<key>``) where a
relation names ``[user]``, a userset (``role:<id>#assignee``) where one names ``[role#assignee]``. The offboarding door
ends a principal this way ([[LH-233]]), and the role delete door a role ([[LH-557]]).

Ending a subject never leaves a project with no direct admin: not by revoking its own ``admin``, and not by emptying a
role that is the project's only direct admin, of which the subject is the last assignee.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Final

from lance_namespace import ConcurrentModificationError

from catalog.services import project_admins
from catalog.services.roles import ASSIGNEE, assignees_of
from service_kit.governed import fga


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient


#: OpenFGA's largest Read page.
PAGE_SIZE: Final = 100


@lru_cache
def types_holding(user_type: str, relation: str | None) -> tuple[str, ...]:
    """The types with a relation holding a ``user_type`` subject directly (``user_type#relation`` when ``relation`` is
    given), never through a wildcard alone."""
    holding = []
    for td in fga.load_model()["type_definitions"]:
        meta = (td.get("metadata") or {}).get("relations") or {}
        if any(
            t.get("type") == user_type and t.get("relation") == relation and not t.get("wildcard")
            for m in meta.values()
            for t in m.get("directly_related_user_types") or []
        ):
            holding.append(td["type"])
    return tuple(sorted(holding))


async def tuples_naming(client: OpenFgaClient, user: str) -> list[fga.ClientTuple]:
    """Every direct tuple naming ``user`` (``user:<key>`` or ``<type>:<id>#<relation>``), page by page."""
    subject, _, relation = user.partition("#")
    found: list[fga.ClientTuple] = []
    for fga_type in types_holding(subject.split(":", 1)[0], relation or None):
        continuation: str | None = None
        while True:
            page, continuation = await fga.read_tuples(client, user=user, obj=f"{fga_type}:", page_size=PAGE_SIZE, continuation_token=continuation)
            found.extend(page)
            if not continuation:
                break
    return found


def _is_project_admin(t: fga.ClientTuple) -> bool:
    return t.relation == project_admins.ADMIN and t.object.startswith("project:")


async def _role_project(client: OpenFgaClient, role: str) -> tuple[str | None, set[str]]:
    """The project ``role``'s edge names (``project:<id>``), and its direct assignees."""
    tuples = [t for t in await fga.read_object_tuples(client, role) if t.object == role]
    project = next((t.user for t in tuples if t.relation == "project" and t.user.startswith("project:")), None)
    return project, {t.user for t in tuples if t.relation == ASSIGNEE}


async def refuse_stranding(client: OpenFgaClient, user: str, naming: list[fga.ClientTuple], *, who: str) -> None:
    """Refuse (409, code 14) ending ``user`` while that would leave a project with no direct admin: ``user`` is its only
    direct ``admin``, or the last assignee of a role that is. ``naming`` is :func:`tuples_naming`'s answer for ``user``.

    Raises:
        ConcurrentModificationError: Ending ``user`` would strand a project; nothing was revoked.
    """
    stranded = {t.object for t in naming if _is_project_admin(t) and project_admins.sole_admin(await fga.read_object_tuples(client, t.object), t.object, user)}
    for t in naming:
        if not _is_role_assignment(t):
            continue
        project, assigned = await _role_project(client, t.object)
        if (
            project is not None
            and assigned == {user}
            and project_admins.sole_admin(await fga.read_object_tuples(client, project), project, f"{t.object}#{ASSIGNEE}")
        ):
            stranded.add(project)
    if stranded:
        raise ConcurrentModificationError(
            f"{who} holds the only direct admin of {', '.join(sorted(stranded))} — grant another admin first; nothing was revoked"
        )


def _is_role_assignment(t: fga.ClientTuple) -> bool:
    return t.relation == ASSIGNEE and t.object.startswith("role:")


async def revoke_naming(client: OpenFgaClient, user: str, naming: list[fga.ClientTuple], *, actor: str, origin: fga.TupleOrigin) -> list[fga.ClientTuple]:
    """Revoke every tuple in ``naming``; the tuples removed. A project ``admin`` and a role assignment each go through the
    store-arbitrated revoke of the door that grants it (:func:`project_admins.revoke_admin`, :func:`unassign`)."""
    removed: list[fga.ClientTuple] = []
    for t in naming:
        if _is_project_admin(t):
            revoked, _ = await project_admins.revoke_admin(client, t.object, user, actor=actor, origin=origin)
        elif _is_role_assignment(t):
            revoked = await unassign(client, t.object.removeprefix("role:"), user, actor=actor, origin=origin)
        else:
            continue
        if revoked:
            removed.append(fga.ClientTuple(user=user, relation=t.relation, object=t.object))
    arbitrated = [t for t in naming if _is_project_admin(t) or _is_role_assignment(t)]
    if rest := [fga.ClientTuple(user=t.user, relation=t.relation, object=t.object) for t in naming if t not in arbitrated]:
        removed.extend(await fga.delete_tuples(client, rest, actor=actor, origin=origin))  # noqa: TID251
    return removed


async def unassign(client: OpenFgaClient, role_id: str, user: str, *, actor: str, origin: fga.TupleOrigin) -> bool:
    """Remove ``user`` from role ``role_id``'s direct assignees; whether this call removed it.

    Refused (409, code 14) when ``user`` is the last direct assignee of a role that is its project's only direct admin.
    While the role holds the project's ``admin``, the removal moves the project's ``admin_revision`` in the same Write
    (:func:`project_admins.next_revision`), so it and any other change to who administers the project, each judged on its
    own read, cannot both commit; the loser reads again and judges what it finds.

    Raises:
        ConcurrentModificationError: The removal would strand the project, or its admins changed under both reads.
    """
    role, subject = f"role:{role_id}", assignees_of(role_id)
    target = fga.ClientTuple(user=user, relation=ASSIGNEE, object=role)
    for _ in range(project_admins.ADMIN_REVOKE_READS):
        project, assigned = await _role_project(client, role)
        if user not in assigned:
            return False
        project_tuples = await fga.read_object_tuples(client, project) if project is not None else []
        if project is None or not any(t.object == project and t.relation == project_admins.ADMIN and t.user == subject for t in project_tuples):
            await fga.delete_tuples(client, [target], actor=actor, origin=origin)  # noqa: TID251
            return True
        if assigned == {user} and project_admins.sole_admin(project_tuples, project, subject):
            raise ConcurrentModificationError(f"{user} is the last assignee of {role}, the only direct admin of {project} — assign another first")
        moved, stale = project_admins.next_revision(project_tuples, project)
        if await fga.commit_unless_changed(client, deletes=[target, *stale], writes=[moved], actor=actor, origin=origin):  # noqa: TID251
            return True
    raise ConcurrentModificationError(f"the admins of the project {role} belongs to changed while this removal ran, twice — read them again and retry")
