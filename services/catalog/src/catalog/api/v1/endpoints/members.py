"""Who belongs to one TENANT — read, grant, revoke. (P6)

The gap this closes was the only one in the tenancy audit that blocked real users. ``type project``
has had ``admin``/``member``/``team`` rungs and a whole cascade built on them since the beginning, and
exactly two things in the estate could write them: ``seed_project_admin`` at create time, and
``POST /v1/access/tuples`` — the raw tuple editor, which is estate-admin gated. So the person who
created a project, and holds ``admin`` on it, could not invite a single colleague into it. Adding a
second admin, adding a member, or attaching a team was a platform ticket.

The per-object grant surface could not substitute, and the reason is precise: ``_GRANTABLE_BASE`` in
``access.py`` is the DATA rungs, and ``type project`` defines none of them, so the grantable set on a
``project:`` object is empty and the endpoint would reject any relation a caller named. That is
correct as a guard and wrong as the only path.

Deliberately NOT a generalization of ``access.py``. That surface is keyed on the data-plane rungs and
should stay that way; this one writes ``admin``/``member`` on exactly one project object.

**Shape copied from ``services/annotator/.../members.py``**, which is the same API one level down
(``annotation_project`` hangs off ``project``) and has been shipping. Same three routes, same
idempotent grant, same never-empty-admin refusal, same "revoking what is not there is a no-op, not a
404". Copying it rather than designing a new one is the point: the estate already decided how this
looks.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

from fastapi import APIRouter, Path, Request, status
from lance_namespace import ConcurrentModificationError, ServiceUnavailableError
from pydantic import BaseModel, Field

from catalog.api import fga_deps
from catalog.api.dependencies import SettingsDep, get_control_emitter
from catalog.api.security import CurrentToken
from service_kit.control_emit import emit_control
from service_kit.governed import fga
from service_kit.governed.audit import SUCCESS, audit


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/projects", tags=["members"])

#: The tenant id shape, matching ``CONTROL_ID_RE`` (``core/identifiers.py``) — DNS-safe, 3–63 chars.
#: Restated as a ``Path`` pattern so a malformed id is a 422 at the boundary rather than a 404 from a
#: registry lookup on a name that could never have existed.
ProjectId = Annotated[str, Path(min_length=3, max_length=63, pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")]

#: The rungs this API grants, most privileged first. ``team`` is absent on purpose: it is the edge
#: that makes every member of a team a project ADMIN (``admin: … or member from team``), so exposing
#: it here would let one request confer admin on a set of people the caller cannot enumerate. Attaching
#: a team stays an estate operation.
Rung = Literal["admin", "member"]
GRANTABLE: tuple[str, ...] = ("admin", "member")

#: Losing the last one strands the tenant: nobody inside it could grant, and ``can_administer`` also
#: gates DELETE, so it could be neither administered nor retired without estate intervention. Refusing
#: is what makes ``can_grant_admin: admin`` safe — it is the pairing Lakekeeper's model calls
#: "lock-out protection" and states as ``project_admin``'s first purpose.
ADMINISTRATIVE: frozenset[str] = frozenset({"admin"})


class Member(BaseModel):
    """One direct grant on this tenant."""

    #: The FGA user string as stored (``user:gina``, ``role:x#assignee``, ``team:y#member``). Verbatim,
    #: because it is what a revoke must send back and a prettified name that cannot round-trip is a trap.
    user: str
    relation: str


class MemberList(BaseModel):
    members: list[Member]
    #: The rungs this API will grant, so a UI does not keep a second copy of the model's ladder.
    grantable: list[str] = Field(default_factory=lambda: list(GRANTABLE))


class GrantRequest(BaseModel):
    #: A bare subject (``gina``) or a full FGA user string. Normalised below — asking a UI to know the
    #: prefix is asking it to know the authorization model.
    user: str = Field(min_length=1, max_length=256)
    relation: Rung


def _user_string(raw: str) -> str:
    """``gina`` -> ``user:gina``; anything already typed (``user:…``, ``role:…#assignee``) passes through."""
    return raw if ":" in raw else f"user:{raw}"


def _direct_grants(tuples: list[Any], obj: str) -> list[Member]:
    """The membership rungs, filtered out of every tuple on the object.

    ``team`` is excluded: it is the parent EDGE, not a person. Listing ``team:eng`` as a member would
    invite someone to revoke the thing that makes every one of its members an admin.
    """
    return [Member(user=t.user, relation=t.relation) for t in tuples if t.object == obj and t.relation in GRANTABLE]


async def _client_or_conflict(request: Request, settings: Any) -> OpenFgaClient:
    """The FGA client, or a clear refusal. Membership IS tuples — with authz off there is nothing to
    write, and pretending otherwise would report a grant that never happened."""
    client = getattr(request.app.state, "fga", None)
    if not settings.fga_enabled or client is None:
        # 503, not the 409 this used to be: membership IS tuples, so authz-off is the service being
        # UNAVAILABLE for the operation, not a conflict with anything — and access.py already answers
        # 503 for the identical condition. Two doors, one status.
        raise ServiceUnavailableError("authorization is not configured on this deployment — tenant membership is unavailable")
    return cast("OpenFgaClient", client)


@router.get("/{project_id}/members")
async def list_members(project_id: ProjectId, request: Request, settings: SettingsDep, token: CurrentToken) -> MemberList:
    """Who holds which rung directly on this tenant — gated on ``can_read_assignments``.

    DIRECT grants only. An admin who holds it through ``member from team`` is not listed, because they
    are not revocable here and a remove button that cannot work is worse than no row. The team edge is
    where that access lives and where it is removed.
    """
    obj = f"project:{project_id}"
    client = getattr(request.app.state, "fga", None)
    await fga_deps.require_relation(client, settings, token, relation="can_read_assignments", obj=obj)
    if not settings.fga_enabled or client is None:
        # Authz off (local dev): there are no tuples, and an empty list is the honest answer. Inventing
        # names here would put people in front of someone who never granted them.
        return MemberList(members=[])
    tuples = await fga.read_object_tuples(cast("OpenFgaClient", client), obj)
    return MemberList(members=_direct_grants(tuples, obj))


@router.put("/{project_id}/members", status_code=status.HTTP_200_OK)
async def grant_member(project_id: ProjectId, payload: GrantRequest, request: Request, settings: SettingsDep, token: CurrentToken) -> MemberList:
    """Grant one tenant rung to one subject — gated PER RUNG (``can_grant_admin`` / ``can_grant_member``).

    Idempotent: re-granting what someone already holds is a no-op. That is not an optimisation — an
    OpenFGA Write is transactional and REJECTS an existing tuple, so without the read this would 400
    on the second call.
    """
    obj = f"project:{project_id}"
    await fga_deps.require_relation(getattr(request.app.state, "fga", None), settings, token, relation=f"can_grant_{payload.relation}", obj=obj)
    client = await _client_or_conflict(request, settings)
    user = _user_string(payload.user)
    actor = token.sub if token else "system:catalog"

    existing = await fga.read_object_tuples(client, obj)
    granted = not any(t.object == obj and t.relation == payload.relation and t.user == user for t in existing)
    if granted:
        await fga.write_tuples(client, [fga.ClientTuple(user=user, relation=payload.relation, object=obj)], actor=actor, origin="grant_api")
        existing = await fga.read_object_tuples(client, obj)
    audit("project.members.grant", SUCCESS, subject=actor, resource=obj, grantee=user, relation=payload.relation)
    # TELL THE PERSON. `access.py` has announced every per-object grant it writes since the control lane
    # existed; this door — one rung UP, and strictly more consequential — wrote the same class of tuple
    # and announced nothing, so being made a project admin arrived in silence.
    #
    # Gated on `granted`, not fired unconditionally: a re-grant is already a no-op on the store, and an
    # announcement of an unchanged state would put a row in someone's inbox for something that did not
    # happen. After the audit, like every other emit — a change that did not commit is never announced.
    if granted:
        await emit_control(
            get_control_emitter(request),
            action="grant_added",
            object_type="grant",
            object_id=obj,
            actor=f"user:{token.sub}" if token else None,
            extra={"relation": payload.relation, "subject": user},
        )
    return MemberList(members=_direct_grants(existing, obj))


@router.delete("/{project_id}/members", status_code=status.HTTP_200_OK)
async def revoke_member(project_id: ProjectId, payload: GrantRequest, request: Request, settings: SettingsDep, token: CurrentToken) -> MemberList:
    """Revoke one tenant rung — gated identically to granting it.

    REFUSES the last ``admin``. Removing it leaves a tenant nobody inside can administer, grant on, or
    delete — recoverable only by estate intervention, which is exactly the state the API exists to
    avoid. Named rather than discovered later.

    Revoking a rung that is not there is a no-op, not a 404: the caller asked for a state and that
    state already holds.
    """
    obj = f"project:{project_id}"
    await fga_deps.require_relation(getattr(request.app.state, "fga", None), settings, token, relation=f"can_grant_{payload.relation}", obj=obj)
    client = await _client_or_conflict(request, settings)
    user = _user_string(payload.user)
    actor = token.sub if token else "system:catalog"

    existing = await fga.read_object_tuples(client, obj)
    if not any(t.object == obj and t.relation == payload.relation and t.user == user for t in existing):
        return MemberList(members=_direct_grants(existing, obj))

    if payload.relation in ADMINISTRATIVE and len([t for t in existing if t.object == obj and t.relation in ADMINISTRATIVE]) <= 1:
        raise ConcurrentModificationError(
            f"{user} holds the only remaining admin on this project — grant another admin first, or nobody will be able to administer or delete it"
        )

    await fga.delete_tuples(client, [fga.ClientTuple(user=user, relation=payload.relation, object=obj)], actor=actor, origin="grant_api")
    audit("project.members.revoke", SUCCESS, subject=actor, resource=obj, grantee=user, relation=payload.relation)
    # THE SHARPER HALF. After this the subject can no longer see the project, so no visibility-gated
    # feed could ever tell them — which is precisely why the control lane runs no visibility check and
    # why being NAMED is the targeting. Unannounced, losing tenant access is discovered as a 403 in the
    # middle of work, the failure this lane exists to end.
    #
    # Unconditional here because the no-op case already returned above: reaching this line means a
    # tuple really was deleted.
    await emit_control(
        get_control_emitter(request),
        action="grant_revoked",
        object_type="grant",
        object_id=obj,
        actor=f"user:{token.sub}" if token else None,
        extra={"relation": payload.relation, "subject": user},
    )
    return MemberList(members=_direct_grants(await fga.read_object_tuples(client, obj), obj))
