"""Revoking a project's direct ``admin``, which never leaves a project without one ([[LH-434]]).

A project with no direct admin can be governed through none of its own doors: nobody inside it can grant, and
``can_administer`` also gates its delete. Two doors revoke an admin tuple, the members door and the principal delete
door ([[LH-233]]), and both go through :func:`revoke_admin`, so neither can strand a tenant the other protects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from lance_namespace import ConcurrentModificationError

from service_kit.governed import fga


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient


ADMIN: Final = "admin"
#: How many times a revoke reads the project's admins: once, and once more after another revoke moved them.
ADMIN_REVOKE_READS: Final = 2


def revision_number(subject: str) -> int:
    """The ``n`` of a ``revision:<n>`` subject; ``0`` for one that names no number, which the next revision outgrows."""
    _, _, number = subject.partition(":")
    return int(number) if number.isdigit() else 0


def sole_admin(tuples: list[fga.ClientTuple], obj: str, user: str) -> bool:
    """Whether ``user`` holds the only direct ``admin`` on ``obj`` among ``tuples``."""
    admins = {t.user for t in tuples if t.object == obj and t.relation == ADMIN}
    return admins == {user}


async def revoke_admin(client: OpenFgaClient, obj: str, user: str, *, actor: str, origin: fga.TupleOrigin) -> tuple[bool, list[fga.ClientTuple]]:
    """Delete ``user``'s direct ``admin`` on project ``obj``: whether this call removed it, and the project's tuples after.

    Revoking what is not held is a no-op. The last direct admin is refused (409, code 14). The count is a read, so two
    revokes of two admins each read two: the admin tuple is deleted and the project's ``admin_revision`` moved in one
    Write that the store refuses whole once another revoke moved the revision, and the loser reads again and judges the
    admins it finds.

    Raises:
        ConcurrentModificationError: ``user`` is the project's last direct admin, or the admins changed under both reads.
    """
    target = fga.ClientTuple(user=user, relation=ADMIN, object=obj)
    for _ in range(ADMIN_REVOKE_READS):
        existing = await fga.read_object_tuples(client, obj)
        if not any(t.object == obj and t.relation == ADMIN and t.user == user for t in existing):
            return False, existing
        if sole_admin(existing, obj, user):
            raise ConcurrentModificationError(
                f"{user} holds the only remaining admin on {obj} — grant another admin first, or nobody will be able to administer or delete it"
            )
        revisions = [t for t in existing if t.object == obj and t.relation == fga.ADMIN_REVISION]
        following = 1 + max((revision_number(t.user) for t in revisions), default=0)
        moved = fga.ClientTuple(user=f"revision:{following}", relation=fga.ADMIN_REVISION, object=obj)
        stale = [fga.ClientTuple(user=t.user, relation=t.relation, object=obj) for t in revisions]
        if await fga.commit_unless_changed(client, deletes=[target, *stale], writes=[moved], actor=actor, origin=origin):  # noqa: TID251
            return True, await fga.read_object_tuples(client, obj)
    raise ConcurrentModificationError(f"the admins of {obj} changed while this revoke ran, twice — read them again and retry")
