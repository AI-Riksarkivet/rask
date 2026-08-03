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
from typing import TYPE_CHECKING, Annotated, Any, cast

from fastapi import APIRouter, Path, Query, status
from pydantic import BaseModel, Field

from annotator.api.security import CheckerDep, CurrentSubject, FgaClientDep
from annotator.projects.machines import legal_project_events
from annotator.projects.models import AnnotationProject, LabelSchema, ProjectState
from service_kit.exceptions import ForbiddenError, NotFoundError
from service_kit.governed import fga
from service_kit.governed.audit import FAILURE, SUCCESS, audit


if TYPE_CHECKING:
    from openfga_sdk import OpenFgaClient


router = APIRouter(prefix="/projects", tags=["annotation-projects"])

#: The relation guarding creation, on the tenant. Named once so the endpoint and its tests cannot
#: drift from the model (`service_kit.governed.auth.model.fga`, type `project`).
CREATE_RELATION = "can_create_annotation_project"

#: The relation guarding the tenant's project LIST, on the tenant. `member` is the rung
#: `can_create_annotation_project` already keys on, and `viewer: … or member from tenant` makes a
#: member a viewer of every project below — so member-gates-the-list and can_view-gates-the-item
#: agree by construction.
LIST_RELATION = "member"

ProjectId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class CreateProjectRequest(BaseModel):
    """The create payload. `tenant` is the authz parent, so it is required and never inferred."""

    tenant: str = Field(min_length=1)
    slug: str = Field(min_length=1)
    title: str = ""
    description: str = ""
    #: Annotator-facing labeling instructions (how to label) — distinct from `description` (what/why).
    instructions: str = Field(default="", max_length=20_000)
    label_schema: LabelSchema = Field(default_factory=LabelSchema)
    review_required: bool = True
    lease_seconds: int = Field(default=1800, gt=0)
    #: Consensus v1 — create-only by design: `send` derives the replica count from it and the
    #: claim guard enumerates siblings with it, so changing it mid-flight would orphan replicas.
    consensus_n: int = Field(default=1, ge=1, le=5)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=AnnotationProject)
async def create_annotation_project(payload: CreateProjectRequest, checker: CheckerDep, subject: CurrentSubject, fga_client: FgaClientDep) -> AnnotationProject:
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
        instructions=payload.instructions,
        label_schema=payload.label_schema,
        review_required=payload.review_required,
        lease_seconds=payload.lease_seconds,
        consensus_n=payload.consensus_n,
        state=ProjectState.DRAFT,
        created_at=now,
        updated_at=now,
        created_by=subject,
    )
    # PERSIST it. Until 2026-07-28 this endpoint built the model, audited, and returned it — so the
    # single entry point into the whole plane was a no-op that reported 201, and the very next call
    # (`POST /projects/<id>/items`) answered 409 "annotation project does not exist".
    stored = await _create_actor(project.project_id).create(project.model_dump(mode="json"))

    # REGISTER it in the tenant index — the actor `GET /projects` lists through. Synchronous and
    # NOT best-effort: an unregistered project is invisible to the landing forever, and both halves
    # are idempotent, so a failure here is answered by retrying the create, not by a repair job.
    await _tenant_actor(payload.tenant).register({"project_id": project.project_id})

    # SEED OWNERSHIP. Two tuples, and both are load-bearing: `owner@user:<subject>` so the creator can
    # manage what they made, and the `tenant` edge to `project:<tenant>` so `owner: … or admin from
    # tenant` and `viewer: … or member from tenant` resolve. Without them every rung on this object is
    # unreachable — including door 1 of the publish crossing, which would deny for everyone forever,
    # creator included. The relation is `tenant`, NOT `parent`: this type spells its parent edge
    # differently from every governed type, and writing `parent` yields a tuple no rule reads.
    if fga_client is not None:
        await fga.grant_on_create(
            cast("OpenFgaClient", fga_client),
            user_sub=subject,
            resource="annotation_project",
            obj_id=project.project_id,
            actor=subject,
            origin="annotator",
            parent_object=f"project:{payload.tenant}",
            parent_relation="tenant",
        )

    audit("annotation_project.create", SUCCESS, subject=subject, resource=project.fga_object)
    return AnnotationProject.model_validate(stored)


@router.get("")
async def list_projects(tenant: Annotated[str, Query(min_length=1)], checker: CheckerDep, subject: CurrentSubject) -> dict[str, Any]:
    """The tenant's projects — A1's landing read.

    Gated on tenant MEMBERSHIP, and a refusal is a 403, never an empty 200: "you may not look"
    and "there is nothing here" are different answers, and collapsing them hides the former.
    Fans out to each project's own actor for the document; an id whose actor holds no state (a
    lost partition) is skipped rather than taking the whole landing down.
    """
    parent = f"project:{tenant}"
    if not await checker(user=subject, relation=LIST_RELATION, obj=parent):
        audit("annotation_project.list", FAILURE, subject=subject, resource=parent, relation=LIST_RELATION)
        raise ForbiddenError(f"{subject} lacks {LIST_RELATION} on {parent}")

    listing = await _tenant_actor(tenant).list_projects()
    projects: list[dict[str, Any]] = []
    for project_id in listing["project_ids"]:
        doc = await _create_actor(str(project_id)).get()
        if doc is not None:
            projects.append(doc)
    return {"projects": projects, "total": len(projects)}


@router.get("/{project_id}")
async def get_project(project_id: ProjectId, checker: CheckerDep, subject: CurrentSubject) -> dict[str, Any]:
    """One project plus its LEGAL EVENTS — the read A1's detail page renders.

    `legal_events` comes from `machines.legal_project_events`, i.e. from the transition tables
    themselves: the UI renders what the backend supplies and never hardcodes a second copy of the
    machine that drifts. Each event carries the permission that gates it, so the UI can explain a
    disabled action — while the ACTUAL gate stays server-side on the event POST.
    """
    if not await checker(user=subject, relation="can_view", obj=f"annotation_project:{project_id}"):
        audit("annotation_project.get", FAILURE, subject=subject, resource=project_id, relation="can_view")
        raise ForbiddenError(f"{subject} lacks can_view on annotation_project:{project_id}")
    doc = await _create_actor(project_id).get()
    if doc is None:
        raise NotFoundError(f"annotation project {project_id} does not exist")
    project = AnnotationProject.model_validate(doc)
    return {"project": doc, "legal_events": legal_project_events(project.state)}


def _create_actor(project_id: str) -> Any:
    """Lazy, for the same reason every other proxy here is: the proxy opens a sidecar channel.
    `typed_proxy` translates Python names to the interface's wire names — a raw `ActorProxy`
    dispatches only the wire names and raises `AttributeError` on these very calls in-cluster."""
    from annotator.projects.project_actor import AnnotationProjectActorInterface  # noqa: PLC0415 - deliberate
    from annotator.projects.proxies import typed_proxy  # noqa: PLC0415

    return typed_proxy("AnnotationProjectActor", project_id, AnnotationProjectActorInterface)


def _tenant_actor(tenant: str) -> Any:
    """The tenant-projects index actor — same lazy typed-proxy pattern."""
    from annotator.projects.proxies import typed_proxy  # noqa: PLC0415 - deliberate
    from annotator.projects.tenant_actor import TenantProjectsActorInterface  # noqa: PLC0415

    return typed_proxy("TenantProjectsActor", tenant, TenantProjectsActorInterface)
