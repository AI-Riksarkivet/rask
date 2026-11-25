"""Every direct tuple naming one subject, and how a door ending a subject revokes them without stranding a project.

OpenFGA's Read takes a user only beside an object type ("the object type field is required", v1.18.3), so a reader of
everything granted to a subject asks each type the model lets hold it directly: a principal (``user:<key>``) where a
relation names ``[user]``, a userset (``role:<id>#assignee``) where one names ``[role#assignee]``. The offboarding door
ends a principal this way ([[LH-233]]), and the role delete door a role ([[LH-557]]).

Ending a subject never leaves a project whose ``admin`` reaches nobody: not by revoking its own ``admin``, and not by
emptying any role of a chain the project's ``admin`` reaches its people through ([[LH-597]]).
"""

from functools import lru_cache
from typing import TYPE_CHECKING

from lance_namespace import ConcurrentModificationError

from catalog.services import project_admins
from lakehouse.governed import fga_model, fga_reads, fga_writes


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient


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
    return tuple(sorted(td["type"] for td in fga_model.load_model()["type_definitions"] if _holds_directly(td, user_type, relation)))


async def tuples_naming(client: OpenFgaClient, user: str) -> list[fga_writes.ClientTuple]:
    """Every direct tuple naming ``user`` (``user:<key>`` or ``<type>:<id>#<relation>``), page by page."""
    subject, _, relation = user.partition("#")
    found: list[fga_writes.ClientTuple] = []
    for fga_type in types_holding(subject.split(":", 1)[0], relation or None):
        found.extend(await fga_reads.read_subject_tuples(client, user, fga_type))
    return found


def _decides_who_administers(t: fga_writes.ClientTuple) -> bool:
    return project_admins.is_admin(t) or project_admins.is_assignment(t)


async def refuse_stranding(
    client: OpenFgaClient, user: str, naming: list[fga_writes.ClientTuple], *, who: str, permit: project_admins.Permit | None = None
) -> None:
    """Refuse (409, code 14) ending ``user`` while that would leave a project whose ``admin`` reaches nobody, judged on
    every chain of roles ``user`` stands in (:meth:`project_admins.Administration.survives`). ``naming`` is
    :func:`tuples_naming`'s answer for ``user``. ``permit`` is asked first, with the projects ``user`` administers or
    passes their ``admin`` on in.

    Raises:
        ConcurrentModificationError: Ending ``user`` would strand a project; nothing was revoked.
    """
    deciding = [t for t in naming if _decides_who_administers(t)]
    scope = await project_admins.administrations(client, deciding)
    if permit is not None:
        await permit(project_admins.touched(scope, deciding))
    gone = frozenset((user, t.object) for t in deciding)
    if stranded := sorted(project for project, administration in scope.items() if not administration.survives(gone)):
        raise ConcurrentModificationError(f"{who} is the last administrator {', '.join(stranded)} reaches — grant another admin first; nothing was revoked")


async def revoke_naming(
    client: OpenFgaClient,
    user: str,
    naming: list[fga_writes.ClientTuple],
    *,
    actor: str,
    origin: fga_writes.TupleOrigin,
    permit: project_admins.Permit | None = None,
) -> list[fga_writes.ClientTuple]:
    """Revoke every tuple in ``naming``; the tuples removed. A project ``admin`` and a role assignment go through the
    store-arbitrated removal every door changing who administers a project takes (:func:`project_admins.withdraw`),
    which asks ``permit`` on the read it judges."""
    arbitrated = [fga_writes.ClientTuple(user=user, relation=t.relation, object=t.object) for t in naming if _decides_who_administers(t)]
    rest = [fga_writes.ClientTuple(user=t.user, relation=t.relation, object=t.object) for t in naming if not _decides_who_administers(t)]
    removed = await project_admins.withdraw(client, arbitrated, actor=actor, origin=origin, permit=permit)
    if rest:
        removed.extend(await fga_writes.delete_tuples(client, rest, actor=actor, origin=origin))  # noqa: TID251
    return removed
