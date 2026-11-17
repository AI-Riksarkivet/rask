"""Who administers a project, and changing that without leaving a project nobody administers ([[LH-434]], [[LH-597]]).

A project's ``admin`` is held by people and by roles, and a role's assignees are people and other roles, so who
administers a project is every person its ``admin`` tuples reach through any chain of roles
(:class:`Administration`). A project whose ``admin`` reaches nobody can be governed through none of its own doors:
nobody inside it can grant, and ``can_administer`` also gates its delete. Counting ``admin`` tuples does not answer
that: a role holding ``admin`` may reach nobody.

Every door that changes a project's ``admin`` tuples or who is in a role goes through :func:`confer` or
:func:`withdraw`: the members door, the role doors, the principal delete door ([[LH-233]]), a role's ending
([[LH-557]]) and the declaration converge. Each judges the administration it read and commits in one Write that moves
the project's token (``revision``, ADR 0241), which the store refuses whole once another such change moved it, so no two of them,
each judged on its own read, both commit.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Final

from lance_namespace import ConcurrentModificationError, InvalidInputError
from pydantic import BaseModel, ConfigDict

from catalog.services.roles import ASSIGNEE
from lakehouse.governed import fga


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient


ADMIN: Final = "admin"
#: How many times a change reads the administrations it touches: once, and once more after another change moved them.
READS: Final = 2
#: The tuples OpenFGA takes in one Write, writes and deletes together (its default ``maxTuplesPerWrite``).
_TUPLES_PER_WRITE: Final = 100

#: Asked with the projects whose administration a change touches; raises to refuse the caller.
type Permit = Callable[[list[str]], Awaitable[None]]


def is_admin(t: fga.ClientTuple) -> bool:
    """Whether ``t`` is a project's ``admin`` tuple."""
    return t.relation == ADMIN and t.object.startswith("project:")


def is_assignment(t: fga.ClientTuple) -> bool:
    """Whether ``t`` assigns its subject to a role."""
    return t.relation == ASSIGNEE and t.object.startswith("role:")


def _role_of(subject: str) -> str | None:
    """The role whose assignees the userset ``subject`` names (``role:<id>#assignee``), or ``None`` for any other subject."""
    role, _, relation = subject.partition("#")
    return role if relation == ASSIGNEE and role.startswith("role:") else None


def next_revision(tuples: list[fga.ClientTuple], obj: str) -> tuple[list[fga.ClientTuple], list[fga.ClientTuple]]:
    """The token a Write changing project ``obj``'s administration writes, and the one it deletes: none of either for a
    project whose token no longer stands (a delete retired it, ADR 0241), where nothing is granted and a removal arbitrates
    with nobody.

    A Write carrying both commits only while the token is the one ``tuples`` were read at (:func:`fga.commit_unless_changed`),
    so two changes to who administers a project, each judged on one read, cannot both commit.
    """
    current = fga.standing_incarnation(tuples, obj)
    if current is None:
        return [], []
    return [current.moved().token(obj)], [current.token(obj)]


#: What a change takes away: grants, each ``(subject, object)``.
type Withdrawn = frozenset[tuple[str, str]]


class Administration(BaseModel):
    """One project's ``admin`` tuples and every role they reach, as one pass read them.

    ``tuples`` are the project's own, read before any role, so a Write moving the revision among them is refused once
    any other change to this administration committed after that read.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    project: str
    tuples: list[fga.ClientTuple]
    #: Each role the project's ``admin`` reaches, and its direct assignees.
    assignees: dict[str, frozenset[str]]

    def bears(self, role: str) -> bool:
        """Whether the project's ``admin`` reaches the assignees of ``role``, so being in it is administering the project."""
        return role in self.assignees

    def _reached(self, without: Withdrawn) -> set[tuple[str, str]]:
        """Each grant ``(subject, object)`` the project's ``admin`` passes through once ``without`` is gone. A role is
        walked once, so roles assigned to each other in a ring end the walk."""
        pending = [(t.user, self.project) for t in self.tuples if t.object == self.project and t.relation == ADMIN]
        reached: set[tuple[str, str]] = set()
        walked: set[str] = set()
        while pending:
            subject, obj = pending.pop()
            if (subject, obj) in without:
                continue
            reached.add((subject, obj))
            role = _role_of(subject)
            if role is None or role in walked:
                continue
            walked.add(role)
            pending.extend((assignee, role) for assignee in self.assignees.get(role, ()))
        return reached

    def survives(self, without: Withdrawn) -> bool:
        """Whether the project is still administered once ``without`` is gone: the change takes nothing its ``admin``
        passes through, or a person is still reached."""
        left = self._reached(without)
        return left == self._reached(frozenset()) or any(subject.startswith("user:") for subject, _ in left)


async def _on(client: OpenFgaClient, obj: str, relation: str) -> set[str]:
    return {t.user for t in await fga.read_object_tuples(client, obj) if t.object == obj and t.relation == relation}


async def administration(client: OpenFgaClient, project: str) -> Administration:
    """Read ``project``'s administration: its own tuples first, then each role its ``admin`` reaches, each once."""
    tuples = await fga.read_object_tuples(client, project)
    assignees: dict[str, frozenset[str]] = {}
    pending = [role for t in tuples if t.object == project and t.relation == ADMIN and (role := _role_of(t.user)) is not None]
    while pending:
        role = pending.pop()
        if role in assignees:
            continue
        assignees[role] = frozenset(await _on(client, role, ASSIGNEE))
        pending.extend(nested for assignee in assignees[role] if (nested := _role_of(assignee)) is not None)
    return Administration(project=project, tuples=tuples, assignees=assignees)


async def _projects_of(client: OpenFgaClient, role: str) -> list[str]:
    """The project ``role`` belongs to, and each project whose ``admin`` a role holding ``role`` among its assignees,
    through any chain, is granted. Each role is asked once."""
    projects = sorted(user for user in await _on(client, role, "project") if user.startswith("project:"))
    pending, asked = [role], set[str]()
    while pending:
        current = pending.pop()
        if current in asked:
            continue
        asked.add(current)
        subject = f"{current}#{ASSIGNEE}"
        projects.extend(t.object for t in await fga.read_subject_tuples(client, subject, "project") if is_admin(t))
        pending.extend(t.object for t in await fga.read_subject_tuples(client, subject, "role") if is_assignment(t))
    return projects


async def administrations(client: OpenFgaClient, targets: list[fga.ClientTuple]) -> dict[str, Administration]:
    """The administration of every project a change to ``targets`` (``admin`` tuples and role assignments) can touch,
    by project: an ``admin`` tuple's own project, and for an assignment its role's project and every project that role
    administers through the roles it is nested in."""
    projects: list[str] = []
    for target in targets:
        projects.extend([target.object] if is_admin(target) else await _projects_of(client, target.object))
    return {project: await administration(client, project) for project in dict.fromkeys(projects)}


def touched(scope: dict[str, Administration], targets: list[fga.ClientTuple]) -> list[str]:
    """The projects of ``scope`` whose administration ``targets`` change: an ``admin`` tuple's, and those whose
    ``admin`` reaches an assignment's role."""
    return sorted(project for project, held in scope.items() if any(t.object == project or held.bears(t.object) for t in targets))


async def _held(client: OpenFgaClient, targets: list[fga.ClientTuple]) -> list[fga.ClientTuple]:
    """Those of ``targets`` the store holds now."""
    held = {obj: await fga.read_object_tuples(client, obj) for obj in dict.fromkeys(t.object for t in targets)}
    return [t for t in targets if any(h.user == t.user and h.relation == t.relation and h.object == t.object for h in held[t.object])]


def _revisions(scope: dict[str, Administration]) -> tuple[list[fga.ClientTuple], list[fga.ClientTuple]]:
    """The revision tuples a Write changing every administration in ``scope`` writes, and those it deletes."""
    moves = [next_revision(held.tuples, project) for project, held in scope.items()]
    return [t for moved, _ in moves for t in moved], [t for _, replaced in moves for t in replaced]


def _fits(scope: dict[str, Administration], targets: list[fga.ClientTuple]) -> bool:
    """Whether changing ``targets`` and moving every revision in ``scope`` is one Write the store takes."""
    moved, stale = _revisions(scope)
    return len(targets) + len(moved) + len(stale) <= _TUPLES_PER_WRITE


def _refuse_a_change_past_one_write(scope: dict[str, Administration], target: fga.ClientTuple) -> None:
    if not _fits(scope, [target]):
        raise InvalidInputError(
            f"{target.object} administers {len(scope)} projects, more than one change can arbitrate — remove it from the roles holding their admin first"
        )


async def _commit(
    client: OpenFgaClient,
    scope: dict[str, Administration],
    *,
    deletes: list[fga.ClientTuple],
    writes: list[fga.ClientTuple],
    actor: str,
    origin: fga.TupleOrigin,
) -> bool:
    """Apply the change in one Write that moves the revision of every project in ``scope``; whether it committed."""
    moved, stale = _revisions(scope)
    return await fga.commit_unless_changed(client, deletes=[*deletes, *stale], writes=[*writes, *moved], actor=actor, origin=origin)  # noqa: TID251


async def confer(
    client: OpenFgaClient,
    target: fga.ClientTuple,
    *,
    actor: str,
    origin: fga.TupleOrigin,
    permit: Permit | None = None,
    incarnation: str | None = None,
) -> bool:
    """Write ``target``, a project's ``admin`` tuple or a role assignment; whether this call wrote it.

    ``permit`` is asked with the projects the tuple makes its subject an administrator of, on the read the Write is
    judged by: a role nested beneath one holding a project's ``admin`` confers that ``admin``, however deep. Writing
    what is held is a no-op. ``incarnation`` is the project's its caller was authorized on (ADR 0241): an ``admin`` tuple
    is written only while that incarnation's token stands on its project, in the Write that moves it.

    Raises:
        ConcurrentModificationError: The administrations the tuple touches changed under both reads.
        InvalidInputError: The tuple touches more administrations than one Write moves.
        fga.IncarnationEnded: ``target``'s project is not the incarnation its caller was authorized on.
    """
    for _ in range(READS):
        scope = await administrations(client, [target])
        if incarnation is not None and is_admin(target):
            standing = fga.standing_incarnation(scope[target.object].tuples, target.object)
            if standing is None or standing.incarnation != incarnation:
                raise fga.IncarnationEnded(f"{target.object} is not the project this grant was decided on: it was deleted")
        if permit is not None:
            await permit(touched(scope, [target]))
        if await _held(client, [target]):
            return False
        _refuse_a_change_past_one_write(scope, target)
        if await _commit(client, scope, deletes=[], writes=[target], actor=actor, origin=origin):
            return True
    raise ConcurrentModificationError(f"who administers the project of {target.object} changed while this grant ran, twice — read again and retry")


async def withdraw(
    client: OpenFgaClient, targets: list[fga.ClientTuple], *, actor: str, origin: fga.TupleOrigin, permit: Permit | None = None
) -> list[fga.ClientTuple]:
    """Delete ``targets``, project ``admin`` tuples and role assignments; those this call removed.

    Removing what is not held is a no-op. A removal after which a project's ``admin`` reaches no person is refused
    (409, code 14), whatever ``admin`` tuples are left. ``permit`` is asked as :func:`confer` asks it. Targets whose
    removal and revisions are more than one Write takes are removed in halves, each judged on the administrations the
    half before it left.

    Raises:
        ConcurrentModificationError: The removal would leave a project nobody administers, or the administrations it
            touches changed under both reads.
        InvalidInputError: One target touches more administrations than one Write moves.
    """
    for _ in range(READS):
        scope = await administrations(client, targets)
        if permit is not None:
            await permit(touched(scope, targets))
        held = await _held(client, targets)
        if not held:
            return []
        if not _fits(scope, held):
            if len(held) == 1:
                _refuse_a_change_past_one_write(scope, held[0])
            half = len(held) // 2
            first = await withdraw(client, held[:half], actor=actor, origin=origin, permit=permit)
            return [*first, *await withdraw(client, held[half:], actor=actor, origin=origin, permit=permit)]
        gone = frozenset((t.user, t.object) for t in held)
        if stranded := sorted(project for project, standing in scope.items() if not standing.survives(gone)):
            raise ConcurrentModificationError(
                f"{', '.join(sorted({t.user for t in held}))} is the last administrator {', '.join(stranded)} reaches — "
                "grant another admin first, or nobody will be able to administer or delete it"
            )
        if await _commit(client, scope, deletes=held, writes=[], actor=actor, origin=origin):
            return held
    raise ConcurrentModificationError("who administers the project changed while this removal ran, twice — read again and retry")
