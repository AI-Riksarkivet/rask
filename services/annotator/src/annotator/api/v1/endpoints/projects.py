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

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Final, cast

from fastapi import APIRouter, Path, Query, status
from pydantic import BaseModel, Field

from annotator.api.security import CheckerDep, CurrentSubject, FgaClientDep
from annotator.api.v1.responses import LegalEvent, ProjectDetail, ProjectListing
from annotator.projects.machines import legal_project_events
from annotator.projects.models import AnnotationProject, ProjectState
from annotator.projects.ontology import LabelOntology
from service_kit.exceptions import ConflictError, ForbiddenError, NotFoundError
from service_kit.governed import fga
from service_kit.governed.audit import FAILURE, SUCCESS, audit


if TYPE_CHECKING:
    from openfga_sdk import OpenFgaClient


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["annotation-projects"])

#: The relation guarding creation, on the tenant. Named once so the endpoint and its tests cannot
#: drift from the model (`service_kit.governed.auth.model.fga`, type `project`).
CREATE_RELATION = "can_create_annotation_project"

#: The relation guarding the tenant's project LIST, on the tenant. `member` is the rung
#: `can_create_annotation_project` already keys on, and `viewer: … or member from tenant` makes a
#: member a viewer of every project below — so member-gates-the-list and can_view-gates-the-item
#: agree by construction.
LIST_RELATION = "member"

#: How many project-actor reads the landing keeps in flight at once. One actor id PER project, so
#: these genuinely parallelise rather than queueing on a single actor's turn lock. Matches the send
#: path's seed fan-out cap (`_ACTOR_FANOUT`) and the publish path's (`_COLLECT_FANOUT`); the bound
#: exists because the alternative is one sidecar channel per project in the tenant.
_LISTING_FANOUT: Final[int] = 16

ProjectId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class CreateProjectRequest(BaseModel):
    """The create payload. `tenant` is the authz parent, so it is required and never inferred."""

    tenant: str = Field(min_length=1)
    slug: str = Field(min_length=1)
    title: str = ""
    description: str = ""
    #: Annotator-facing labeling instructions (how to label) — distinct from `description` (what/why).
    instructions: str = Field(default="", max_length=20_000)
    review_required: bool = True
    lease_seconds: int = Field(default=1800, gt=0)
    #: Consensus v1 — create-only by design: `send` derives the replica count from it and the
    #: claim guard enumerates siblings with it, so changing it mid-flight would orphan replicas.
    consensus_n: int = Field(default=1, ge=1, le=5)
    #: The whole task definition — taxonomy, per-class tools, attributes, relations. Validated by
    #: its own model (including the cross-checks that the old `label_schema` + `template` pair had
    #: nothing to run); absent means an unconstrained project, which is the pre-ontology behaviour.
    ontology: LabelOntology = Field(default_factory=LabelOntology)


@router.post("", status_code=status.HTTP_201_CREATED)
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
        review_required=payload.review_required,
        lease_seconds=payload.lease_seconds,
        consensus_n=payload.consensus_n,
        ontology=payload.ontology,
        state=ProjectState.DRAFT,
        created_at=now,
        updated_at=now,
        created_by=subject,
    )
    # PERSIST it. Until 2026-07-28 this endpoint built the model, audited, and returned it — so the
    # single entry point into the whole plane was a no-op that reported 201, and the very next call
    # (`POST /projects/<id>/items`) answered 409 "annotation project does not exist".
    stored = await _create_actor(project.project_id).create(project.model_dump(mode="json"))

    # THE OTHER TWO WRITES, COMPENSATED. Create is three writes in sequence and it used to protect
    # none of them, on the stated grounds that "a failure here is answered by retrying the create,
    # not by a repair job". That was false in the way that matters: `project_id` is minted per
    # attempt (`default_factory=new_id`), so a retry creates a SECOND project and ORPHANS the first
    # — as a document nothing lists and nobody may open, or (once the register landed) as a row on
    # the landing page that denies every check for everyone, creator included.
    #
    # So a failure past this point undoes what it wrote, in reverse, and then fails. The
    # compensations are idempotent and `discard` refuses anything but an empty draft, which is what
    # keeps this a create saga rather than a delete door (docs/DECISIONS.md "The Python estate audit" ANN-10).
    registered = False
    try:
        # REGISTER it in the tenant index — the actor `GET /projects` lists through. Synchronous and
        # NOT best-effort: an unregistered project is invisible to the landing forever.
        await _tenant_actor(payload.tenant).register({"project_id": project.project_id})
        registered = True

        # SEED OWNERSHIP. Two tuples, and both are load-bearing: `owner@user:<subject>` so the creator
        # can manage what they made, and the `tenant` edge to `project:<tenant>` so `owner: … or admin
        # from tenant` and `viewer: … or member from tenant` resolve. Without them every rung on this
        # object is unreachable — including door 1 of the publish crossing, which would deny for
        # everyone forever, creator included. The relation is `tenant`, NOT `parent`: this type spells
        # its parent edge differently from every governed type, and writing `parent` yields a tuple no
        # rule reads.
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
    except Exception:
        await _undo_create(project.project_id, payload.tenant, registered=registered)
        # `reason`, not `relation`: the create was AUTHORIZED — a later write in the sequence
        # failed. Stamping the door here would read as a denial that never happened.
        audit("annotation_project.create", FAILURE, subject=subject, resource=project.fga_object, reason="compensated")
        raise

    audit("annotation_project.create", SUCCESS, subject=subject, resource=project.fga_object)
    return AnnotationProject.model_validate(stored)


async def _undo_create(project_id: str, tenant: str, *, registered: bool) -> None:
    """Roll the create's writes back, in reverse, leaving nothing behind.

    Every step is best-effort and logged: this runs because something already failed, and the
    ORIGINAL failure is the one the caller must see — a compensation that raises would replace the
    real reason with a second-order one. What a failed compensation leaves is exactly the orphan
    that existed before, now with a log line naming it.
    """
    if registered:
        try:
            await _tenant_actor(tenant).unregister({"project_id": project_id})
        except Exception:
            logger.exception("could not un-register project %s from tenant %s after a failed create — it will list without owner tuples", project_id, tenant)
    try:
        await _create_actor(project_id).discard({})
    except Exception:
        logger.exception("could not discard project %s after a failed create — its document is orphaned in the actor state store", project_id)


@router.get("")
async def list_projects(tenant: Annotated[str, Query(min_length=1)], checker: CheckerDep, subject: CurrentSubject) -> ProjectListing:
    """The tenant's projects — A1's landing read.

    Gated on tenant MEMBERSHIP, and a refusal is a 403, never an empty 200: "you may not look"
    and "there is nothing here" are different answers, and collapsing them hides the former.
    Fans out to each project's own actor for the document, with concurrency bounded by
    `_LISTING_FANOUT`; an id whose actor holds no state (a lost partition) is skipped rather than
    taking the whole landing down.

    This is the estate's hottest actor fan-out — one call per page load — and each id addresses a
    DIFFERENT actor, so the reads are independent. Awaited in series the landing's wall clock is one
    sidecar round-trip per project in the tenant.
    """
    parent = f"project:{tenant}"
    if not await checker(user=subject, relation=LIST_RELATION, obj=parent):
        audit("annotation_project.list", FAILURE, subject=subject, resource=parent, relation=LIST_RELATION)
        raise ForbiddenError(f"{subject} lacks {LIST_RELATION} on {parent}")

    listing = await _tenant_actor(tenant).list_projects()
    gate = asyncio.Semaphore(_LISTING_FANOUT)

    async def _document(project_id: str) -> dict[str, Any] | None:
        async with gate:
            return await _create_actor(project_id).get()

    # `gather` preserves INPUT order, so the landing renders in the tenant index's order. It also
    # propagates the first failure rather than collecting it, which is deliberate: a listing that
    # silently omits a project the caller may see is indistinguishable from one that was deleted, so
    # an unreachable actor must be an error and not a shorter page.
    docs = await asyncio.gather(*(_document(str(project_id)) for project_id in listing["project_ids"]))
    projects = [doc for doc in docs if doc is not None]
    return ProjectListing(projects=[AnnotationProject.model_validate(doc) for doc in projects], total=len(projects))


@router.get("/{project_id}")
async def get_project(project_id: ProjectId, checker: CheckerDep, subject: CurrentSubject) -> ProjectDetail:
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
    return ProjectDetail(project=project, legal_events=[LegalEvent.model_validate(e) for e in legal_project_events(project.state)])


class UpdateOntologyRequest(BaseModel):
    """The whole ontology, replaced. Not a partial merge — see the route."""

    ontology: LabelOntology


#: Editing the task definition is a MANAGE act, not a labeling one: it changes what every future
#: item promises downstream. Same relation the `open`/`freeze`/`publish` transitions carry.
MANAGE_RELATION = "can_manage"

#: The ontology may be edited only while the project can still receive work. Past `frozen` the
#: answer set is closed and a publish is being prepared against it, so an edit could only either be
#: ignored (every remaining item already captured its copy) or misleading — the run facet would
#: report a taxonomy that no task was ever judged against.
ONTOLOGY_EDITABLE_STATES = frozenset({ProjectState.DRAFT, ProjectState.LABELING})


@router.patch("/{project_id}/ontology")
async def update_project_ontology(
    project_id: ProjectId,
    payload: UpdateOntologyRequest,
    checker: CheckerDep,
    subject: CurrentSubject,
) -> AnnotationProject:
    """Replace a project's ontology. 403 without `can_manage`, 409 once the project is past labeling.

    WHOLE-DOCUMENT replace, deliberately. A partial merge would have to answer "what does an absent
    `classes` mean" — cleared, or unchanged? — and the two readings differ by an entire taxonomy.
    The ontology is also cross-checked as a UNIT (relations must reference declared classes), so a
    merge would have to re-validate the merged result anyway; taking the whole document makes the
    thing validated and the thing stored the same object.

    Items already sent are UNAFFECTED: each captured its own copy at send, so work in review is
    judged by the contract it was issued under. That is the point of the capture, and it is what
    makes editing safe enough to allow during `labeling` at all.
    """
    obj = f"annotation_project:{project_id}"
    if not await checker(user=subject, relation=MANAGE_RELATION, obj=obj):
        audit("annotation_project.update_ontology", FAILURE, subject=subject, resource=project_id, relation=MANAGE_RELATION)
        raise ForbiddenError(f"{subject} lacks {MANAGE_RELATION} on {obj}")

    actor = _create_actor(project_id)
    doc = await actor.get()
    if doc is None:
        raise NotFoundError(f"annotation project {project_id} does not exist")
    state = AnnotationProject.model_validate(doc).state
    if state not in ONTOLOGY_EDITABLE_STATES:
        raise ConflictError(f"project {project_id} is {state.value} — the ontology is editable only in {sorted(s.value for s in ONTOLOGY_EDITABLE_STATES)}")

    stored = await actor.set_ontology({"ontology": payload.ontology.model_dump(mode="json")})
    audit("annotation_project.update_ontology", SUCCESS, subject=subject, resource=project_id, relation=MANAGE_RELATION)
    return AnnotationProject.model_validate(stored)


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
