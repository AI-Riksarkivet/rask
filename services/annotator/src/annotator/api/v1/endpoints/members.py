"""Who may do what on one annotation project — read, grant, revoke.

The rungs already exist in the model and are concentric: ``owner`` implies ``manager`` implies
``reviewer`` implies ``annotator`` implies ``viewer``. Nothing here changes the model; this is the
surface over the tuples that were only ever writable by `grant_on_create` and by hand.

Why it is worth having at all: a project's creator got `owner` at create and nobody else could be
added, so every collaborative project needed someone with direct store access. That is the shape
where people end up sharing an account.

Reads are `can_manage`-gated too. Who has access to a thing is itself sensitive — it names the
people worth phishing — and a viewer has no business enumerating them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any, Literal, cast

from fastapi import APIRouter, Path, status
from pydantic import BaseModel, Field

from annotator.api.dependencies import ControlEmitterDep
from annotator.api.security import CheckerDep, CurrentSubject, FgaClientDep
from service_kit.control_emit import emit_control
from service_kit.exceptions import ConflictError, ForbiddenError
from service_kit.governed import fga
from service_kit.governed.audit import FAILURE, SUCCESS, audit


if TYPE_CHECKING:
    from openfga_sdk.client import OpenFgaClient


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["members"])

ProjectId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]

#: The rungs a person can be GRANTED, most privileged first. `viewer` is omitted deliberately: the
#: model already gives it to every member of the owning tenant, so granting it per-project would
#: create tuples that change nothing while implying they do.
Rung = Literal["owner", "manager", "reviewer", "annotator"]
GRANTABLE: tuple[str, ...] = ("owner", "manager", "reviewer", "annotator")

#: Losing every one of these leaves the project manageable only by a tenant admin. Refusing the last
#: one is the difference between "you removed yourself" and "nobody can ever administer this again".
ADMINISTRATIVE: frozenset[str] = frozenset({"owner", "manager"})


class Member(BaseModel):
    """One direct grant on this project."""

    #: The FGA user string as stored, e.g. `user:gina`. Returned verbatim rather than prettified:
    #: it is what a revoke must send back, and a display name that cannot round-trip is a trap.
    user: str
    relation: str


class MemberList(BaseModel):
    members: list[Member]
    #: The rungs this API will grant, so a UI does not hardcode a second copy of the model's ladder.
    grantable: list[str] = Field(default_factory=lambda: list(GRANTABLE))


class GrantRequest(BaseModel):
    #: A bare subject (`gina`) or a full FGA user string (`user:gina`). Normalised below — asking a
    #: UI to know the prefix is asking it to know the authorization model.
    user: str = Field(min_length=1, max_length=256)
    relation: Rung


def _user_string(raw: str) -> str:
    """`gina` -> `user:gina`; anything already typed (`user:…`, `role:…#assignee`) passes through."""
    return raw if ":" in raw else f"user:{raw}"


async def _require_manage(checker: Any, subject: str, project_id: str, action: str) -> str:
    obj = f"annotation_project:{project_id}"
    if not await checker(user=subject, relation="can_manage", obj=obj):
        audit(action, FAILURE, subject=subject, resource=project_id, relation="can_manage")
        raise ForbiddenError(f"{subject} lacks can_manage on {obj}")
    return obj


def _direct_grants(tuples: list[Any], obj: str) -> list[Member]:
    """The membership rungs, filtered out of every tuple on the object.

    `tenant` is excluded: it is the parent EDGE, not a person, and listing `project:acme` as a member
    would invite someone to revoke the thing that makes the whole object resolvable.
    """
    return [Member(user=t.user, relation=t.relation) for t in tuples if t.object == obj and t.relation in GRANTABLE]


@router.get("/{project_id}/members")
async def list_members(project_id: ProjectId, checker: CheckerDep, subject: CurrentSubject, fga_client: FgaClientDep) -> MemberList:
    """Who holds which rung directly on this project.

    DIRECT grants only. A tenant admin who can manage this project through `admin from tenant` is not
    listed, because they are not revocable here and showing an entry whose remove button cannot work
    is worse than not showing it. The tenant edge is where that access lives and where it is removed.
    """
    obj = await _require_manage(checker, subject, project_id, "annotation_project.members.list")
    if fga_client is None:
        # FGA off (local dev). An empty list is honest — there are no tuples — and pretending
        # otherwise would put invented names in front of someone.
        return MemberList(members=[])
    tuples = await fga.read_object_tuples(cast("OpenFgaClient", fga_client), obj)
    return MemberList(members=_direct_grants(tuples, obj))


@router.put("/{project_id}/members", status_code=status.HTTP_200_OK)
async def grant_member(
    project_id: ProjectId, payload: GrantRequest, checker: CheckerDep, subject: CurrentSubject, fga_client: FgaClientDep, control: ControlEmitterDep
) -> MemberList:
    """Grant one rung to one person. Idempotent — re-granting what someone already has is a no-op."""
    obj = await _require_manage(checker, subject, project_id, "annotation_project.members.grant")
    if fga_client is None:
        raise ConflictError("authorization is not configured on this deployment — cannot grant")
    client = cast("OpenFgaClient", fga_client)
    user = _user_string(payload.user)

    existing = await fga.read_object_tuples(client, obj)
    already = any(t.object == obj and t.relation == payload.relation and t.user == user for t in existing)
    if not already:
        # An OpenFGA Write is transactional and REJECTS a tuple that already exists, so the read
        # above is not an optimisation — it is what makes a repeated grant a no-op instead of a 400.
        await fga.write_tuples(
            client,
            [fga.ClientTuple(user=user, relation=payload.relation, object=obj)],
            actor=subject,
            origin="annotator",
        )
        existing = await fga.read_object_tuples(client, obj)
    audit("annotation_project.members.grant", SUCCESS, subject=subject, resource=project_id, relation=payload.relation)
    # TELL THE PERSON — the same lane the catalog's tenant door uses, one rung down. Gated on the
    # write actually happening: a re-grant is already a no-op on the store, and announcing an
    # unchanged state would put a row in someone's inbox for something that did not occur.
    if not already:
        await emit_control(
            control,
            action="grant_added",
            object_type="grant",
            object_id=obj,
            actor=f"user:{subject}",
            extra={"relation": payload.relation, "subject": user},
        )
    return MemberList(members=_direct_grants(existing, obj))


@router.delete("/{project_id}/members", status_code=status.HTTP_200_OK)
async def revoke_member(
    project_id: ProjectId, payload: GrantRequest, checker: CheckerDep, subject: CurrentSubject, fga_client: FgaClientDep, control: ControlEmitterDep
) -> MemberList:
    """Revoke one rung from one person.

    REFUSES the last administrative grant. `owner` and `manager` are the rungs that can grant others;
    removing the final one leaves the project administrable only by a tenant admin — which, on a
    tenant whose admin has moved on, is nobody. That is not a mistake anyone can undo from inside the
    product, so it is refused here and NAMED, rather than discovered later.

    Revoking a rung you do not hold, or that is not there, is a no-op rather than a 404: the caller
    asked for a state and that state already holds.
    """
    obj = await _require_manage(checker, subject, project_id, "annotation_project.members.revoke")
    if fga_client is None:
        raise ConflictError("authorization is not configured on this deployment — cannot revoke")
    client = cast("OpenFgaClient", fga_client)
    user = _user_string(payload.user)

    existing = await fga.read_object_tuples(client, obj)
    present = [t for t in existing if t.object == obj and t.relation == payload.relation and t.user == user]
    if not present:
        return MemberList(members=_direct_grants(existing, obj))

    if payload.relation in ADMINISTRATIVE:
        admins = [t for t in existing if t.object == obj and t.relation in ADMINISTRATIVE]
        if len(admins) <= 1:
            raise ConflictError(
                f"{user} holds the only remaining {payload.relation} on this project — "
                "grant another owner or manager first, or nobody will be able to administer it"
            )

    await fga.delete_tuples(
        client,
        [fga.ClientTuple(user=user, relation=payload.relation, object=obj)],
        actor=subject,
        origin="annotator",
    )
    audit("annotation_project.members.revoke", SUCCESS, subject=subject, resource=project_id, relation=payload.relation)
    # The sharper half, for the reason `grant_revoked` carries everywhere: after this the subject can
    # no longer see the project, so no visibility-gated feed could tell them — being NAMED is the
    # targeting. Unannounced, losing access to an annotation project is discovered by a 403 mid-task.
    await emit_control(
        control,
        action="grant_revoked",
        object_type="grant",
        object_id=obj,
        actor=f"user:{subject}",
        extra={"relation": payload.relation, "subject": user},
    )
    return MemberList(members=_direct_grants(await fga.read_object_tuples(client, obj), obj))
