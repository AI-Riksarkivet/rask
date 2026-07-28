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

from fastapi import APIRouter, status
from pydantic import BaseModel, Field

from annotator.api.security import CheckerDep, CurrentSubject
from annotator.projects.models import AnnotationProject, LabelSchema, ProjectState
from service_kit.exceptions import ForbiddenError
from service_kit.governed.audit import FAILURE, SUCCESS, audit


router = APIRouter(prefix="/projects", tags=["annotation-projects"])

#: The relation guarding creation, on the tenant. Named once so the endpoint and its tests cannot
#: drift from the model (`service_kit.governed.auth.model.fga`, type `project`).
CREATE_RELATION = "can_create_annotation_project"


class CreateProjectRequest(BaseModel):
    """The create payload. `tenant` is the authz parent, so it is required and never inferred."""

    tenant: str = Field(min_length=1)
    slug: str = Field(min_length=1)
    title: str = ""
    description: str = ""
    label_schema: LabelSchema = Field(default_factory=LabelSchema)
    review_required: bool = True
    lease_seconds: int = Field(default=1800, gt=0)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=AnnotationProject)
async def create_annotation_project(payload: CreateProjectRequest, checker: CheckerDep, subject: CurrentSubject) -> AnnotationProject:
    """Create a project in `draft`. 403 when the caller is not a member of the target tenant.

    `subject` is the **verified** OIDC principal (`annotator.api.security`), not a header the caller
    supplied — every entity below is keyed on who owns or claims it, so a spoofable identity here
    would be a cross-user leak rather than a cosmetic issue.

    Fails CLOSED: any falsy check result denies. A project is born in `draft` — `open` is a separate,
    `can_manage`-gated transition, so creating one never implies it is ready to label.
    """
    parent = f"project:{payload.tenant}"
    allowed = await checker(user=subject, relation=CREATE_RELATION, obj=parent)
    if not allowed:
        audit("annotation_project.create", FAILURE, subject=subject, resource=parent, relation=CREATE_RELATION)
        raise ForbiddenError(f"{subject} lacks {CREATE_RELATION} on {parent}")

    now = datetime.now(UTC)
    project = AnnotationProject(
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
        created_by=subject,
    )
    audit("annotation_project.create", SUCCESS, subject=subject, resource=project.fga_object)
    return project
