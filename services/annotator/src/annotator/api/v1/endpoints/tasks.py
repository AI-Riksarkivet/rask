"""Task endpoints — the CVAT/Label-Studio loop: send, assign, claim, save, submit, review.

Each route authorizes the ONE `can_*` its event requires, on the **verified** subject, then invokes
the task actor. The permission is not hardcoded here: `annotator.projects.machines` already carries
it on the edge (`TASK_EDGES[(state, event)] -> (target, permission)`), so the op→privilege map has a
single home and a route cannot drift from the model.

Two rules the machine cannot express, enforced here because they need the task's own data:

- **Self-review is forbidden.** A reviewer may not accept their own submission (§5.2). The events are
  named in `SELF_REVIEW_FORBIDDEN` so the rule is discoverable from the machine; the check needs
  `task.submitted_by`, which only the actor holds.
- **The lease holder is the only one who may save or submit.** `can_annotate` says you may annotate
  in this project; holding the claim says you may annotate THIS task right now.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Final, cast

from fastapi import APIRouter, Path, Request, status
from pydantic import BaseModel, Field

from annotator.api.security import CheckerDep, CurrentSubject
from annotator.projects.actor import AnnotationTaskActorInterface
from annotator.projects.imports import shapes_from_ipc
from annotator.projects.machines import (
    FROZEN_PROJECT_STATES,
    HOLDER_OR_MANAGER,
    LEASE_HOLDER_ONLY,
    SELF_REVIEW_FORBIDDEN,
    TASK_EDGES,
    IllegalTransition,
    task_transition,
)
from annotator.projects.models import Link, ProjectState, Shape, TaskState
from annotator.projects.ontology import LabelOntology
from service_kit.exceptions import ConflictError, ForbiddenError, NotFoundError
from service_kit.governed.audit import FAILURE, SUCCESS, audit


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["annotation-tasks"])

#: Edges whose `TASK_EDGES` permission is `None` — caused by the system (an actor reminder), never by
#: a principal. They are REFUSED on the HTTP surface rather than waved through: "no permission
#: required" is a statement about the actor's internal caller, and treating it as "no permission
#: checked" would let anyone POST `lease_expired` to strip another annotator's claim.
SYSTEM_ONLY_EVENTS: Final[frozenset[str]] = frozenset(e for (_s, e), (_t, p) in TASK_EDGES.items() if p is None)

TaskId = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class FireRequest(BaseModel):
    """One event against a task. `event` must be an edge in `TASK_EDGES`.

    Deliberately narrow. It carries NO project/tenant — the authorization object is read from the
    task's own record, so a caller cannot name the object its permission is checked against — and no
    `review_required`, which is captured on the task at send time from the project; a caller able to
    pass it would submit with review waived and self-accept.
    """

    event: str = Field(min_length=1)
    #: Optional override; absent means "the project's lease", captured on the task at send. A
    #: hardcoded default HERE would silently outvote the project's configuration on every claim.
    lease_seconds: int | None = Field(default=None, gt=0, le=86_400)
    #: `assign` only. The user a MANAGER is assigning the task to — this is what makes the edge a
    #: distribution mechanism rather than a self-claim. Safe as a request field because it names the
    #: RECIPIENT, not the caller: the caller's own authority is still `can_manage`, checked against
    #: the task's project.
    assignee: str | None = Field(default=None, max_length=128)
    #: `request_changes` only — the reviewer's reason, appended as a `ReviewNote`.
    message: str = Field(default="", max_length=4000)
    shape_ids: list[str] = Field(default_factory=list, max_length=500)


class SaveDraftRequest(BaseModel):
    shapes: list[Shape] = Field(default_factory=list)
    #: The typed edges drawn between those shapes. Declared HERE and not only on `Draft`, because a
    #: field this model does not name is dropped in silence: pydantic ignores unknown keys, so the
    #: client sent `links`, the actor was built to store them, and the endpoint between them
    #: forwarded shapes alone. Nothing errored — and on a task whose ontology declares a REQUIRED
    #: relation the submit then read an always-empty list and refused work that was actually done.
    links: list[Link] = Field(default_factory=list)
    base_revision: int | None = None
    origin: str = "human"


def _proxy(task_id: str) -> AnnotationTaskActorInterface:
    """A typed proxy to one task's actor. Imported lazily: `ActorProxy` opens a sidecar channel, so
    importing it at module scope would make this module require daprd just to be read.

    `cast`, not a suppression: `ActorProxy` dispatches `__getattr__` over the wire, so it satisfies
    the interface structurally but cannot declare it. Naming the interface as the return type is what
    keeps a typo in a method name a type error here rather than a 404 from the sidecar.
    """
    from annotator.projects.proxies import typed_proxy  # noqa: PLC0415 - deliberate, see docstring

    return cast(AnnotationTaskActorInterface, typed_proxy("AnnotationTaskActor", task_id, AnnotationTaskActorInterface))


async def _authorize(checker: Any, subject: str, permission: str, project_id: str, what: str) -> None:
    """Check one relation on the ANNOTATION PROJECT, fail closed, and audit either way.

    The object is `annotation_project:<project_id>`, because that is the type on which
    `service_kit.governed.auth.model.fga` defines `can_claim` / `can_annotate` / `can_review` /
    `can_manage`. Checking them on `project:<tenant>` — as this did until 2026-07-28 — asks OpenFGA
    for a relation that type does not define, which fails closed and makes the entire task plane
    return 403 the moment FGA is switched on. Only `can_create_annotation_project` lives on the
    tenant, because at create time the child does not exist yet.

    The project id comes from the TASK's own record, never from the request, so a caller cannot
    choose which object its permission is evaluated against.
    """
    obj = f"annotation_project:{project_id}"
    if not await checker(user=subject, relation=permission, obj=obj):
        audit(what, FAILURE, subject=subject, resource=obj, relation=permission)
        raise ForbiddenError(f"{subject} lacks {permission} on {obj}")


async def _refuse_if_project_frozen(project_id: str, event: str) -> None:
    """§5.2 rule 5 — nothing escapes a published project.

    Once the project is publishing, published or archived, EVERY task transition is rejected:
    provenance is frozen with the artifact, and a task that moved after the publish would describe a
    dataset that no longer matches it. Costs one actor read per task event, which is the honest price
    of a guarantee that would otherwise be documented and unenforced.
    """
    from annotator.api.v1.endpoints.project_events import _project_proxy  # noqa: PLC0415 - import cycle

    project = await _project_proxy(project_id).get()
    if project is None:
        return  # an orphaned task; the transition's own preconditions still apply
    state = ProjectState(project["state"])
    if state in FROZEN_PROJECT_STATES:
        raise ConflictError(f"project {project_id} is {state} — {event} is rejected; provenance is frozen with the published artifact")


async def _refuse_second_replica(task_id: str, current: dict[str, Any], recipient: str) -> None:
    """Consensus v1's independence rule, enforced where claims happen.

    Sibling ids are DETERMINISTIC (`{group}-r{k}`, k ≤ the project's `consensus_n`), so the guard
    reads each sibling's own actor — no index, ≤4 extra reads — and refuses when the recipient
    already holds or has already worked any sibling. The refusal NAMES the replica, so the 409 is
    actionable rather than a mystery."""
    from annotator.api.v1.endpoints.project_events import _project_proxy  # noqa: PLC0415 - import cycle

    group = str(current["replica_of"])
    project = await _project_proxy(str(current["project_id"])).get()
    consensus_n = int((project or {}).get("consensus_n") or 1)
    for k in range(1, consensus_n + 1):
        sibling_id = f"{group}-r{k}"
        if sibling_id == task_id:
            continue
        sibling = await _proxy(sibling_id).get()
        if sibling is None:
            continue
        if recipient in (sibling.get("assignee"), sibling.get("submitted_by")):
            raise ConflictError(f"one replica per annotator per group: {recipient} already holds or worked replica {sibling_id} of group {group}")


@router.post("/{task_id}/events", status_code=status.HTTP_200_OK)
async def fire_task_event(task_id: TaskId, payload: FireRequest, checker: CheckerDep, subject: CurrentSubject) -> dict[str, Any]:
    """Drive one task transition.

    The permission comes from the transition table, so adding an edge to `TASK_EDGES` automatically
    gates it — there is no per-route ladder here to forget to update.
    """
    if payload.event in SYSTEM_ONLY_EVENTS:
        raise ForbiddenError(f"{payload.event} is caused by the system, not by a principal, and cannot be fired over HTTP")

    actor = _proxy(task_id)
    current = await actor.get()
    if current is None:
        raise ConflictError(f"task {task_id} has not been sent into a project")

    state = TaskState(current["state"])
    try:
        _target, permission = task_transition(state, payload.event)
    except IllegalTransition as exc:
        raise ConflictError(str(exc)) from exc
    if permission is None:  # unreachable via SYSTEM_ONLY_EVENTS, kept so a new None edge fails closed
        raise ForbiddenError(f"{payload.event} requires no principal permission and is not exposed")

    project_id = str(current["project_id"])
    await _authorize(checker, subject, permission, project_id, f"task.{payload.event}")

    await _refuse_if_project_frozen(project_id, payload.event)

    # Consensus v1: one replica per annotator per group — the same person labeling two replicas of
    # one item is one opinion counted twice, which defeats the point of independent replicas.
    if payload.event in ("claim", "assign") and current.get("replica_of"):
        recipient = str(payload.assignee or subject) if payload.event == "assign" else subject
        await _refuse_second_replica(task_id, current, recipient)

    # Rules the table cannot express, because they depend on the task's own rows.
    if payload.event in SELF_REVIEW_FORBIDDEN and current.get("submitted_by") == subject:
        audit(f"task.{payload.event}", FAILURE, subject=subject, resource=task_id, reason="self_review")
        raise ForbiddenError("a reviewer may not review their own submission")
    holder = current.get("assignee")
    if payload.event in LEASE_HOLDER_ONLY and holder not in (None, subject):
        # §5.2 rule 2 — "even a manager". A manager who wants the task must `release` and re-`assign`.
        raise ForbiddenError(f"task {task_id} is held by {holder}")
    # §5.2 — `release` is the documented escape hatch for a task pinned to someone unavailable, so a
    # manager may take it; anyone else may not break another annotator's claim.
    if (
        payload.event in HOLDER_OR_MANAGER
        and holder not in (None, subject)
        and not await checker(user=subject, relation="can_manage", obj=f"annotation_project:{project_id}")
    ):
        raise ForbiddenError(f"task {task_id} is held by {holder} — releasing it needs can_manage")

    try:
        updated = await actor.fire(
            {
                "event": payload.event,
                # `actor` is always the VERIFIED caller. `assignee` is the recipient a manager names
                # on `assign` — a different thing, and conflating them is what made `assign` a
                # self-claim. `review_required` is deliberately absent: it lives on the task.
                "actor": subject,
                "lease_seconds": payload.lease_seconds,
                "assignee": payload.assignee,
                "message": payload.message,
                "shape_ids": payload.shape_ids,
            }
        )
    except IllegalTransition as exc:  # lost a race — the state moved between our read and the call
        raise ConflictError(str(exc)) from exc

    audit(f"task.{payload.event}", SUCCESS, subject=subject, resource=task_id)
    return updated


@router.get("/{task_id}")
async def get_task(task_id: TaskId, checker: CheckerDep, subject: CurrentSubject) -> dict[str, Any]:
    """Read one task. The task is fetched FIRST to learn which project to authorize against, then the
    check runs before anything is returned — the fetch is not the disclosure, the return is."""
    task = await _proxy(task_id).get()
    if task is None:
        raise ConflictError(f"task {task_id} has not been sent into a project")
    await _authorize(checker, subject, "can_view", str(task["project_id"]), "task.view")
    return task


@router.put("/{task_id}/draft", status_code=status.HTTP_200_OK)
async def save_draft(task_id: TaskId, payload: SaveDraftRequest, checker: CheckerDep, subject: CurrentSubject) -> dict[str, Any]:
    """Replace the whole shape set in one keyed write. `base_revision` is the etag — a mismatch is a
    409, which is how two tabs of one annotator are stopped from silently clobbering each other."""
    actor = _proxy(task_id)
    current = await actor.get()
    if current is None:
        raise ConflictError(f"task {task_id} has not been sent into a project")
    await _authorize(checker, subject, "can_annotate", str(current["project_id"]), "task.save_draft")

    # A draft is only writable while the task is CLAIMED. Without this an ACCEPTED task's shapes
    # could be rewritten after review — and during a publish — which would put annotations into the
    # lakehouse that no reviewer ever saw. `TASK_EDGES` already says `save_draft` is legal only from
    # CLAIMED; this is the HTTP surface honouring the machine instead of writing around it.
    await _refuse_if_project_frozen(str(current["project_id"]), "save_draft")
    if TaskState(current["state"]) is not TaskState.CLAIMED:
        raise ConflictError(f"task {task_id} is {current['state']} — a draft is only writable while the task is claimed")
    if current.get("assignee") not in (None, subject):
        raise ForbiddenError(f"task {task_id} is held by {current['assignee']}")

    try:
        draft = await actor.save_draft(
            {
                "task_id": task_id,
                "project_id": str(current["project_id"]),
                "author": subject,
                "shapes": [s.model_dump(mode="json") for s in payload.shapes],
                "links": [link.model_dump(mode="json") for link in payload.links],
                "base_revision": payload.base_revision,
                "origin": payload.origin,
            }
        )
    except IllegalTransition as exc:
        raise ConflictError(str(exc)) from exc

    audit("task.save_draft", SUCCESS, subject=subject, resource=task_id)
    return draft


@router.post("/{task_id}/import", status_code=status.HTTP_200_OK)
async def import_annotations(task_id: TaskId, request: Request, checker: CheckerDep, subject: CurrentSubject) -> dict[str, Any]:
    """Import annotations made elsewhere into this task's draft, as Arrow IPC.

    ONE format — the canonical annotations schema — because the estate already has one and three
    parsers inside the service would be three things that rot. Converting COCO or YOLO into it is a
    `scripts/` concern. See `annotator.projects.imports`.

    **APPENDS to the draft; it does not replace it.** The alternative was a whole-draft replace,
    which matches how `save_draft` stores (a draft is one keyed write), and it was rejected: an
    import onto a task somebody had already worked would silently destroy that work, with no undo
    anywhere in the actor. Appending is never destructive, composes (importing twice gives two sets,
    visibly), and leaves duplicates removable on the canvas — a recoverable annoyance instead of an
    unrecoverable loss.

    The same state rules as a draft save, and for the same reason: importing is annotating. A task
    that is not CLAIMED by this caller cannot receive one, so an import can no more write into an
    already-reviewed task than a manual edit can.
    """
    actor = _proxy(task_id)
    current = await actor.get()
    if current is None:
        raise ConflictError(f"task {task_id} has not been sent into a project")
    await _authorize(checker, subject, "can_annotate", str(current["project_id"]), "task.import")

    await _refuse_if_project_frozen(str(current["project_id"]), "import")
    if TaskState(current["state"]) is not TaskState.CLAIMED:
        raise ConflictError(f"task {task_id} is {current['state']} — annotations can only be imported into a claimed task")
    if current.get("assignee") not in (None, subject):
        raise ForbiddenError(f"task {task_id} is held by {current['assignee']}")

    existing = await actor.get_draft() or {}
    existing_shapes: list[dict[str, Any]] = list(existing.get("shapes") or [])
    existing_links: list[dict[str, Any]] = list(existing.get("links") or [])
    taken = {str(s.get("shape_id")) for s in existing_shapes if s.get("shape_id")}

    # The ontology CAPTURED on the task, not one the client sends — the caller must not be able to
    # supply the rules it is judged by. An absent/unparseable ontology means "constrains nothing",
    # which is the same posture every other surface takes.
    ontology = None
    raw_ontology = current.get("ontology")
    if raw_ontology:
        try:
            parsed = LabelOntology.model_validate(raw_ontology)
        except Exception:  # noqa: BLE001 - an unreadable rule is not a reason to 500 the import
            logger.warning("task %s carries an ontology this service cannot parse", task_id)
        else:
            ontology = parsed if parsed.constrains else None

    payload = await request.body()
    shapes, links = shapes_from_ipc(payload, ontology=ontology, taken_ids=taken)

    try:
        draft = await actor.save_draft(
            {
                "task_id": task_id,
                "project_id": str(current["project_id"]),
                "author": subject,
                "shapes": existing_shapes + [s.model_dump(mode="json") for s in shapes],
                "links": existing_links + [link.model_dump(mode="json") for link in links],
                # Read from the draft we just appended to, so a save that lands in between is a 409
                # rather than a silent overwrite — the same guarantee two tabs already get.
                "base_revision": existing.get("revision"),
                "origin": "import",
            }
        )
    except IllegalTransition as exc:
        raise ConflictError(str(exc)) from exc

    audit("task.import", SUCCESS, subject=subject, resource=task_id)
    return {"imported": len(shapes), "links": len(links), "draft": draft}


@router.get("/{task_id}/draft")
async def get_draft(task_id: TaskId, checker: CheckerDep, subject: CurrentSubject) -> dict[str, Any]:
    actor = _proxy(task_id)
    task = await actor.get()
    if task is None:
        raise ConflictError(f"task {task_id} has not been sent into a project")
    await _authorize(checker, subject, "can_view", str(task["project_id"]), "task.view_draft")
    draft = await actor.get_draft()
    if draft is None:
        raise NotFoundError(f"task {task_id} has no draft")
    return draft
