"""Annotation-project endpoints — slice S4, the create pin.

Create is a **create-on-parent** operation: at authorization time the child does not exist, so the
door is the TENANT (`can_create_annotation_project` on `project:<tenant>`), never the child. Checking
the child would be checking an object with no tuples — which fails open or fails meaningless
depending on the model, and is the mistake `design-create-on-parent` exists to prevent.

The FGA client is injected rather than reached for, so the authorization contract is testable without
a running OpenFGA. Authorization decides *whether*; the state machine in `annotator.projects.machines`
decides *what* — they are deliberately separate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from annotator.projects.models import AnnotationProject, LabelSchema, ProjectState


router = APIRouter(prefix="/projects", tags=["annotation-projects"])

#: The relation guarding creation, on the tenant. Named once so the endpoint and its tests cannot
#: drift from the model (`service_kit.governed.auth.model.fga`, type `project`).
CREATE_RELATION = "can_create_annotation_project"


class FgaChecker(Protocol):
    """The one capability this module needs from OpenFGA — deliberately narrow.

    Narrower than the real client on purpose: the endpoint should not be able to write tuples or
    enumerate objects, and a test double should not have to pretend it can.
    """

    async def __call__(self, *, user: str, relation: str, obj: str) -> bool: ...


class CreateProjectRequest(BaseModel):
    """The create payload. `tenant` is the authz parent, so it is required and never inferred."""

    tenant: str = Field(min_length=1)
    slug: str = Field(min_length=1)
    title: str = ""
    description: str = ""
    label_schema: LabelSchema = Field(default_factory=LabelSchema)
    review_required: bool = True
    lease_seconds: int = Field(default=1800, gt=0)


def get_checker(request: Request) -> FgaChecker:
    """Resolve the FGA checker from app state; overridden in tests via `dependency_overrides`."""
    checker = getattr(request.app.state, "fga_check", None)
    if checker is None:  # pragma: no cover - wiring error, not a runtime path
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "authorization unavailable")
    return checker


def get_principal(request: Request) -> str:
    """The calling principal. Falls back to anonymous, which no tuple grants — fail closed."""
    return getattr(request.state, "principal", None) or "user:anonymous"


CheckerDep = Annotated[FgaChecker, Depends(get_checker)]
PrincipalDep = Annotated[str, Depends(get_principal)]


@router.post("", status_code=status.HTTP_201_CREATED, response_model=AnnotationProject)
async def create_annotation_project(payload: CreateProjectRequest, checker: CheckerDep, principal: PrincipalDep) -> AnnotationProject:
    """Create a project in `draft`. 403 when the caller is not a member of the target tenant.

    Fails CLOSED: any falsy check result denies. A project is born in `draft` — `open` is a separate,
    `can_manage`-gated transition, so creating one never implies it is ready to label.
    """
    parent = f"project:{payload.tenant}"
    allowed = await checker(user=principal, relation=CREATE_RELATION, obj=parent)
    if not allowed:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"{principal} lacks {CREATE_RELATION} on {parent}",
        )

    now = datetime.now(UTC)
    return AnnotationProject(
        tenant=payload.tenant,
        slug=payload.slug,
        title=payload.title,
        description=payload.description,
        label_schema=payload.label_schema,
        review_required=payload.review_required,
        lease_seconds=payload.lease_seconds,
        state=ProjectState.DRAFT,
        created_at=now,
        updated_at=now,
        created_by=principal,
    )
