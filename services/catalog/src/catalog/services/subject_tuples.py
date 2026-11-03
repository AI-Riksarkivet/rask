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

from catalog.services import project_admins, role_membership
from catalog.services.roles import ASSIGNEE
from service_kit.governed import fga


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient


#: OpenFGA's largest Read page.
PAGE_SIZE: Final = 100


def _holds_directly(type_definition: dict[str, object], user_type: str, relation: str | None) -> bool:
    """Whether some relation of ``type_definition`` names ``user_type`` (as ``user_type#relation``) as a direct subject."""
    metadata = type_definition.get("metadata") or {}
    relations = (metadata.get("relations") if isinstance(metadata, dict) else None) or {}
    return any(
        t.get("type") == user_type and t.get("relation") == relation and not t.get("wildcard")
        for meta in relations.values()
        for t in meta.get("directly_related_user_types") or []
    )


@lru_cache
def types_holding(user_type: str, relation: str | None) -> tuple[str, ...]:
    """The types with a relation holding a ``user_type`` subject directly (``user_type#relation`` when ``relation`` is
    given), never through a wildcard alone."""
    return tuple(sorted(td["type"] for td in fga.load_model()["type_definitions"] if _holds_directly(td, user_type, relation)))


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


def _is_role_assignment(t: fga.ClientTuple) -> bool:
    return t.relation == ASSIGNEE and t.object.startswith("role:")


async def _project_stranded_by(client: OpenFgaClient, user: str, t: fga.ClientTuple) -> str | None:
    """The project that revoking ``t`` from ``user`` would leave with no direct admin, or ``None``."""
    if _is_project_admin(t):
        project, admin = t.object, user
    elif _is_role_assignment(t):
        project, assigned = await role_membership.role_project(client, t.object)
        if project is None or assigned != {user}:
            return None
        admin = f"{t.object}#{ASSIGNEE}"
    else:
        return None
    return project if project_admins.sole_admin(await fga.read_object_tuples(client, project), project, admin) else None


async def refuse_stranding(client: OpenFgaClient, user: str, naming: list[fga.ClientTuple], *, who: str) -> None:
    """Refuse (409, code 14) ending ``user`` while that would leave a project with no direct admin: ``user`` is its only
    direct ``admin``, or the last assignee of a role that is. ``naming`` is :func:`tuples_naming`'s answer for ``user``.

    Raises:
        ConcurrentModificationError: Ending ``user`` would strand a project; nothing was revoked.
    """
    stranded: set[str] = set()
    for t in naming:
        if (project := await _project_stranded_by(client, user, t)) is not None:
            stranded.add(project)
    if stranded:
        raise ConcurrentModificationError(
            f"{who} holds the only direct admin of {', '.join(sorted(stranded))} — grant another admin first; nothing was revoked"
        )


async def revoke_naming(client: OpenFgaClient, user: str, naming: list[fga.ClientTuple], *, actor: str, origin: fga.TupleOrigin) -> list[fga.ClientTuple]:
    """Revoke every tuple in ``naming``; the tuples removed. A project ``admin`` and a role assignment each go through the
    store-arbitrated revoke of the door that grants it (:func:`project_admins.revoke_admin`, :func:`role_membership.unassign`)."""
    admins = [t for t in naming if _is_project_admin(t)]
    assignments = [t for t in naming if _is_role_assignment(t)]
    rest = [fga.ClientTuple(user=t.user, relation=t.relation, object=t.object) for t in naming if not (_is_project_admin(t) or _is_role_assignment(t))]
    removed: list[fga.ClientTuple] = []
    for t in admins:
        revoked, _ = await project_admins.revoke_admin(client, t.object, user, actor=actor, origin=origin)
        if revoked:
            removed.append(fga.ClientTuple(user=user, relation=t.relation, object=t.object))
    for t in assignments:
        if await role_membership.unassign(client, t.object.removeprefix("role:"), user, actor=actor, origin=origin):
            removed.append(fga.ClientTuple(user=user, relation=t.relation, object=t.object))
    if rest:
        removed.extend(await fga.delete_tuples(client, rest, actor=actor, origin=origin))  # noqa: TID251
    return removed
