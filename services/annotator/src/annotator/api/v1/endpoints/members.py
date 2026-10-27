"""Who may do what on one annotation project — read, grant, revoke.

The roles are the annotator's own (ADR 0171, `annotator.projects.roles`), kept in each project's actor: ``owner``
implies ``manager`` implies ``reviewer`` implies ``annotator`` implies ``viewer``. A project's creator is its first
owner; this is the surface that adds everyone else, so a collaborative project needs no shared account.

Reads are `can_manage`-gated too. Who has access to a thing is itself sensitive — it names the people worth
phishing — and a viewer has no business enumerating them.

A person is told about a grant on the annotator's own channel, the project they can now open: the lakehouse's
notifications report the lakehouse's own state changes, and a tool's work is the tool's to announce.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, cast
from urllib.parse import unquote

from fastapi import APIRouter, Path, Query, status
from pydantic import BaseModel, Field

from annotator.api.security import CheckerDep, CurrentSubject, SettingsDep
from annotator.audit_actions import AnnotatorAuditAction
from annotator.projects.machines import IllegalTransition
from annotator.projects.roles import GRANTABLE, MemberGrant, Rung
from service_kit.exceptions import ConflictError, ForbiddenError, ValidationError
from service_kit.governed.audit import AuditOutcome, audit
from service_kit.governed.principal import is_principal


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["members"])

ProjectId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class MemberList(BaseModel):
    #: Each direct grant, as stored: its `user` is what a revoke sends back.
    members: list[MemberGrant]
    #: The roles this API will grant, so a UI does not hardcode a second copy of the ladder.
    grantable: list[str] = Field(default_factory=lambda: list(GRANTABLE))


class GrantRequest(BaseModel):
    #: The person's principal key. A `user:`-prefixed id an earlier reader echoed back is accepted as the key it names.
    user: str = Field(min_length=1, max_length=256)
    relation: Rung


def _person(raw: str, settings: Any) -> str:
    """The principal key a grant names; with authentication on it must be one a person's token can prove."""
    key = unquote(raw.removeprefix("user:"))
    if settings.oidc_enabled and not is_principal(key):
        raise ValidationError(f"{raw!r} is not a principal: '<idp-id>~<subject>'")
    return key


def _project(project_id: str) -> Any:
    """A typed proxy to the project's actor. Lazy: the proxy opens a sidecar channel."""
    from annotator.projects.project_actor import AnnotationProjectActorInterface  # noqa: PLC0415 - deliberate
    from annotator.projects.proxies import typed_proxy  # noqa: PLC0415

    return typed_proxy("AnnotationProjectActor", project_id, AnnotationProjectActorInterface)


async def _require_manage(checker: Any, subject: str, project_id: str, action: AnnotatorAuditAction) -> None:
    obj = f"annotation_project:{project_id}"
    if not await checker(user=subject, relation="can_manage", obj=obj):
        audit(action, AuditOutcome.FAILURE, subject=subject, resource=project_id, relation="can_manage")
        raise ForbiddenError(f"{subject} lacks can_manage on {obj}")


def _listing(answer: dict[str, Any]) -> MemberList:
    return MemberList(members=[MemberGrant.model_validate(m) for m in cast("list[dict[str, str]]", answer["members"])])


@router.get("/{project_id}/members")
async def list_members(project_id: ProjectId, checker: CheckerDep, subject: CurrentSubject) -> MemberList:
    """Who holds which role directly on this project.

    DIRECT grants only. A tenant admin who owns every project of the tenant is not listed, because they are not
    revocable here and showing an entry whose remove button cannot work is worse than not showing it. The tenant is
    where that access lives and where it is removed.
    """
    await _require_manage(checker, subject, project_id, AnnotatorAuditAction.ANNOTATION_PROJECT_MEMBERS_LIST)
    try:
        return _listing(await _project(project_id).members())
    except IllegalTransition as exc:
        raise ConflictError(str(exc)) from exc


@router.put("/{project_id}/members", status_code=status.HTTP_200_OK)
async def grant_member(project_id: ProjectId, payload: GrantRequest, checker: CheckerDep, subject: CurrentSubject, settings: SettingsDep) -> MemberList:
    """Grant one role to one person. Idempotent — re-granting what someone already has is a no-op."""
    await _require_manage(checker, subject, project_id, AnnotatorAuditAction.ANNOTATION_PROJECT_MEMBERS_GRANT)
    try:
        answer = await _project(project_id).grant_member({"user": _person(payload.user, settings), "relation": payload.relation})
    except IllegalTransition as exc:
        raise ConflictError(str(exc)) from exc
    audit(AnnotatorAuditAction.ANNOTATION_PROJECT_MEMBERS_GRANT, AuditOutcome.SUCCESS, subject=subject, resource=project_id, relation=payload.relation)
    return _listing(answer)


@router.delete("/{project_id}/members", status_code=status.HTTP_200_OK)
async def revoke_member(
    project_id: ProjectId,
    user: Annotated[str, Query(min_length=1, max_length=256)],
    relation: Rung,
    checker: CheckerDep,
    subject: CurrentSubject,
    settings: SettingsDep,
) -> MemberList:
    """Revoke one role from one person.

    `user` + `relation` ride as QUERY PARAMS, not a body: a DELETE body has no defined semantics (RFC 9110) and some
    intermediaries strip or refuse it, which would make revoke fail only behind certain infra.

    REFUSES the last owner or manager, in the actor's own turn so a concurrent revoke cannot slip past it: removing the
    final one leaves the project administrable only by a tenant admin — which, on a tenant whose admin has moved on,
    is nobody. Revoking a role nobody holds is a no-op rather than a 404: the caller asked for a state and that state
    already holds.
    """
    await _require_manage(checker, subject, project_id, AnnotatorAuditAction.ANNOTATION_PROJECT_MEMBERS_REVOKE)
    try:
        answer = await _project(project_id).revoke_member({"user": _person(user, settings), "relation": relation})
    except IllegalTransition as exc:
        raise ConflictError(str(exc)) from exc
    if answer.get("changed"):
        audit(AnnotatorAuditAction.ANNOTATION_PROJECT_MEMBERS_REVOKE, AuditOutcome.SUCCESS, subject=subject, resource=project_id, relation=relation)
    return _listing(answer)
