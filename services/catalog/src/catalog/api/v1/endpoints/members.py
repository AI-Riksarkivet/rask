"""Who belongs to one TENANT — read, grant, revoke. (P6)

The gap this closes was the only one in the tenancy audit that blocked real users. ``type project``
has had ``admin``/``member`` rungs and a whole cascade built on them since the beginning, and
exactly two things in the estate could write them: ``seed_project_admin`` at create time, and
``POST /management/v1/access/tuples`` — the raw tuple editor, which is estate-admin gated. So the person who
created a project, and holds ``admin`` on it, could not invite a single colleague into it.

The per-object grant surface could not substitute, and the reason is precise: ``_GRANTABLE_BASE`` in
``access.py`` is the DATA rungs, and ``type project`` defines none of them, so the grantable set on a
``project:`` object is empty and the endpoint would reject any relation a caller named. That is
correct as a guard and wrong as the only path.

Deliberately NOT a generalization of ``access.py``. That surface is keyed on the data-plane rungs and
should stay that way; this one writes ``admin``/``member`` on exactly one project object.

Three routes: an idempotent grant, a revoke that refuses to remove the last administrator, and "revoking what is
not there is a no-op, not a 404".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Path, status
from lance_namespace import ConcurrentModificationError, ServiceUnavailableError

from catalog.api import announcing, fga_deps
from catalog.api.announcing import Announcer, AnnouncerDep
from catalog.api.dependencies import FgaClientDep, SettingsDep
from catalog.api.security import Caller, CurrentToken
from catalog.core.config import Settings
from catalog.schemas import GRANTABLE, GrantRequest, Member, MemberList
from catalog.services import project_admins
from lakehouse.governed import fga
from lakehouse.governed.audit import AuditAction, AuditOutcome, audit


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/management/v1/projects", tags=["members"])

#: The tenant id shape, matching ``CONTROL_ID_RE`` (``core/identifiers.py``) — DNS-safe, 3–63 chars.
#: Restated as a ``Path`` pattern so a malformed id is a 422 at the boundary rather than a 404 from a
#: registry lookup on a name that could never have existed.
ProjectId = Annotated[str, Path(min_length=3, max_length=63, pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")]

#: Losing the last one strands the tenant: nobody inside it could grant, and ``can_administer`` also
#: gates DELETE, so it could be neither administered nor retired without estate intervention. Refusing
#: is what makes ``can_grant_admin: admin`` safe: lock-out protection, the reason a tenant may govern its own admins.
ADMINISTRATIVE: frozenset[str] = frozenset({"admin"})


def _user_string(raw: str) -> str:
    """``gina`` -> ``user:gina``; an object of a model type (``role:…#assignee``) passes through (`fga.grantee_object`)."""
    return fga.grantee_object(raw)


def _direct_grants(tuples: list[Any], obj: str) -> list[Member]:
    """The membership rungs, filtered out of every tuple on the object (``revision`` is bookkeeping)."""
    return [Member(user=t.user, relation=t.relation) for t in tuples if t.object == obj and t.relation in GRANTABLE]


def _client_or_conflict(client: OpenFgaClient | None, settings: Settings) -> OpenFgaClient:
    """The FGA client, or a clear refusal. Membership IS tuples — with authz off there is nothing to
    write, and pretending otherwise would report a grant that never happened.

    Its own message rather than ``fga_deps.require_fga``'s pair: this door answers 503 for BOTH legs
    (off and unwired) on purpose — see the note below — where the access surface answers 406 Unsupported for off.
    """
    if not settings.fga_enabled or client is None:
        # 503, not the 409 this used to be: membership IS tuples, so authz-off is the service being
        # UNAVAILABLE for the operation, not a conflict with anything — and access.py already answers
        # a 5xx for the identical condition. Two doors, one status.
        raise ServiceUnavailableError("authorization is not configured on this deployment — tenant membership is unavailable")
    return client


@router.get("/{project_id}/members")
async def list_members(project_id: ProjectId, client: FgaClientDep, settings: SettingsDep, token: CurrentToken) -> MemberList:
    """Who holds which rung directly on this tenant — gated on ``can_read_assignments``.

    DIRECT grants only. A person who holds a rung through a role is not listed: the role is, and it is revoked
    here; who is in it is the role's assignees door.
    """
    obj = f"project:{project_id}"
    await fga_deps.require_relation(client, settings, token, relation="can_read_assignments", obj=obj)
    if (client := fga_deps.wired(settings, client)) is None:
        # Authz off (local dev): there are no tuples, and an empty list is the honest answer. Inventing
        # names here would put people in front of someone who never granted them.
        return MemberList(members=[])
    tuples = await fga.read_object_tuples(client, obj)
    return MemberList(members=_direct_grants(tuples, obj))


@router.put("/{project_id}/members", status_code=status.HTTP_200_OK)
async def grant_member(
    project_id: ProjectId, payload: GrantRequest, client: FgaClientDep, announcer: AnnouncerDep, settings: SettingsDep, token: CurrentToken
) -> MemberList:
    """Grant one tenant rung to one subject — gated PER RUNG (``can_grant_admin`` / ``can_grant_member``).

    A role is granted only on its own project: one another project owns is refused 400 (code 13), as every grant door
    refuses it (:func:`fga_deps.refuse_a_cross_tenant_role_grant`), since that project's security admins decide who is
    in it ([[LH-557]]).

    Idempotent: re-granting what someone already holds is a no-op. That is not an optimisation — an
    OpenFGA Write is transactional and REJECTS an existing tuple, so without the read this would 400
    on the second call. An ``admin`` is granted through :func:`project_admins.confer`, which asks nothing of the
    caller beyond the gate here.

    The grant is written under a fence of the project, opened before the authority is asked (ADR 0241,
    ``fga_deps.door_fence``): a project deleted meanwhile, and one made again under its id by another tenant, is not the
    project the grant was decided on, and the grant lands on neither. Grants for different people never refuse each
    other: each deletes only its own ticket.
    \f

    Raises:
        PermissionDeniedError: The caller may not grant the rung.
        InvalidInputError: The subject is a role another project owns.
        TableNotFoundError: No project stands under the id, or its delete has begun.
        SettlingError: The project stands and is still being established; ask again after ``Retry-After``.
        ConcurrentModificationError: The project was deleted while the grant ran.
    """
    obj = f"project:{project_id}"
    wired = _client_or_conflict(client, settings)
    async with fga_deps.door_fence(wired, settings, obj, token) as door:
        await fga_deps.require_relation(client, settings, token, relation=f"can_grant_{payload.relation}", obj=obj)
        fences = await fga_deps.require_fence(door, settings)
        return await _grant(wired, announcer, settings, token, project_id, payload, fences)


async def _grant(
    wired: OpenFgaClient, announcer: Announcer, settings: Settings, token: Caller | None, project_id: str, payload: GrantRequest, fences: list[fga.Fence]
) -> MemberList:
    """The members door's grant once its caller is authorized, under ``fences``."""
    obj = f"project:{project_id}"
    user = _user_string(payload.user)
    actor = token.subject if token else "system:catalog"
    await fga_deps.refuse_a_cross_tenant_role_grant(wired, user, fga_type="project", segments=[project_id], settings=settings)

    existing = await fga.read_object_tuples(wired, obj)
    tup = fga.ClientTuple(user=user, relation=payload.relation, object=obj)
    if any(t.object == obj and t.relation == payload.relation and t.user == user for t in existing):
        audit(AuditAction.PROJECT_MEMBER_GRANT, AuditOutcome.SUCCESS, subject=actor, resource=obj, grantee=user, relation=payload.relation)
        return MemberList(members=_direct_grants(existing, obj))
    # TELL THE PERSON, filed before the write so a door failing after it still does ([[LH-576]]). Only a grant that
    # changes the store is announced: a re-grant is a no-op there, and an announcement of an unchanged state would put a
    # row in someone's inbox for something that did not happen.
    told = announcing.grant_announcement(tup, action="grant_added", actor=f"user:{token.subject}" if token else None)
    async with announcer.announcing(door="member_grant", target=obj, announcements=[told]) as filing:
        try:
            if payload.relation in ADMINISTRATIVE:
                if not await project_admins.confer(wired, tup, actor=actor, origin="grant_api", fences=fences):
                    return MemberList(members=_direct_grants(await fga.read_object_tuples(wired, obj), obj))
            elif not await fga.commit_fenced(wired, fences, writes=[tup], actor=actor, origin="grant_api"):
                return MemberList(members=_direct_grants(await fga.read_object_tuples(wired, obj), obj))
        except fga.IncarnationEnded as exc:
            raise ConcurrentModificationError(f"project {project_id!r} was deleted while this grant ran; nothing was granted") from exc
        existing = await fga.read_object_tuples(wired, obj)
        audit(AuditAction.PROJECT_MEMBER_GRANT, AuditOutcome.SUCCESS, subject=actor, resource=obj, grantee=user, relation=payload.relation)
        await filing.send(told)
    return MemberList(members=_direct_grants(existing, obj))


@router.delete("/{project_id}/members", status_code=status.HTTP_200_OK)
async def revoke_member(
    project_id: ProjectId, payload: GrantRequest, client: FgaClientDep, announcer: AnnouncerDep, settings: SettingsDep, token: CurrentToken
) -> MemberList:
    """Revoke one tenant rung — gated identically to granting it.

    REFUSES the last administrator: an ``admin`` after whose removal the project's ``admin`` tuples reach no
    person, directly or through a role (:func:`project_admins.withdraw`). That leaves a tenant nobody inside can
    administer, grant on, or delete — recoverable only by estate intervention, which is exactly the state the API
    exists to avoid. Named rather than discovered later.

    Revoking a rung that is not there is a no-op, not a 404: the caller asked for a state and that
    state already holds.

    Fenced as a grant is (ADR 0241): a revoke decided on a project deleted meanwhile takes nothing from one made again
    under its id, and is answered 409.
    \f

    Raises:
        PermissionDeniedError: The caller may not revoke the rung.
        TableNotFoundError: No project stands under the id, or its delete has begun.
        SettlingError: The project stands and is still being established; ask again after ``Retry-After``.
        ConcurrentModificationError: The removal would leave the project nobody administers, or the project was deleted
            while the revoke ran.
    """
    obj = f"project:{project_id}"
    wired = _client_or_conflict(client, settings)
    async with fga_deps.door_fence(wired, settings, obj, token) as door:
        await fga_deps.require_relation(client, settings, token, relation=f"can_grant_{payload.relation}", obj=obj)
        fences = await fga_deps.require_fence(door, settings)
        try:
            return await _revoke(wired, announcer, token, project_id, payload, fences)
        except fga.IncarnationEnded as exc:
            raise ConcurrentModificationError(f"project {project_id!r} was deleted while this revoke ran; nothing was revoked") from exc


async def _revoke(
    wired: OpenFgaClient, announcer: Announcer, token: Caller | None, project_id: str, payload: GrantRequest, fences: list[fga.Fence]
) -> MemberList:
    """The members door's revoke once its caller is authorized, under ``fences``."""
    obj = f"project:{project_id}"
    user = _user_string(payload.user)
    actor = token.subject if token else "system:catalog"

    tup = fga.ClientTuple(user=user, relation=payload.relation, object=obj)
    if payload.relation not in ADMINISTRATIVE:
        existing = await fga.read_object_tuples(wired, obj)
        if not any(t.object == obj and t.relation == payload.relation and t.user == user for t in existing):
            return MemberList(members=_direct_grants(existing, obj))
    # THE SHARPER HALF. After this the subject can no longer see the project, so no visibility-gated
    # feed could ever tell them — which is precisely why the control lane runs no visibility check and
    # why being NAMED is the targeting. Unannounced, losing tenant access is discovered as a 403 in the
    # middle of work, the failure this lane exists to end; so it is filed before the revoke, and a door that
    # fails after the revoke still tells them ([[LH-576]]). A no-op revoke announces nothing.
    told = announcing.grant_announcement(tup, action="grant_revoked", actor=f"user:{token.subject}" if token else None)
    async with announcer.announcing(door="member_revoke", target=obj, announcements=[told]) as filing:
        if payload.relation in ADMINISTRATIVE:
            if not await project_admins.withdraw(wired, [tup], actor=actor, origin="grant_api", fences=fences):
                return MemberList(members=_direct_grants(await fga.read_object_tuples(wired, obj), obj))
        elif not await fga.commit_fenced(wired, fences, deletes=[tup], actor=actor, origin="grant_api"):
            return MemberList(members=_direct_grants(await fga.read_object_tuples(wired, obj), obj))
        audit(AuditAction.PROJECT_MEMBER_REVOKE, AuditOutcome.SUCCESS, subject=actor, resource=obj, grantee=user, relation=payload.relation)
        await filing.send(told)
    return MemberList(members=_direct_grants(await fga.read_object_tuples(wired, obj), obj))
