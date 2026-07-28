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

from typing import Annotated, Any, Final, cast

from fastapi import APIRouter, Path, status
from pydantic import BaseModel, Field

from annotator.api.security import CheckerDep, CurrentSubject
from annotator.projects.actor import AnnotationTaskActorInterface
from annotator.projects.machines import SELF_REVIEW_FORBIDDEN, TASK_EDGES, IllegalTransition, task_transition
from annotator.projects.models import Shape, TaskState
from service_kit.exceptions import ConflictError, ForbiddenError, NotFoundError
from service_kit.governed.audit import FAILURE, SUCCESS, audit


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
    lease_seconds: int = Field(default=1800, gt=0, le=86_400)


class SaveDraftRequest(BaseModel):
    shapes: list[Shape] = Field(default_factory=list)
    base_revision: int | None = None
    origin: str = "human"


def _proxy(task_id: str) -> AnnotationTaskActorInterface:
    """A typed proxy to one task's actor. Imported lazily: `ActorProxy` opens a sidecar channel, so
    importing it at module scope would make this module require daprd just to be read.

    `cast`, not a suppression: `ActorProxy` dispatches `__getattr__` over the wire, so it satisfies
    the interface structurally but cannot declare it. Naming the interface as the return type is what
    keeps a typo in a method name a type error here rather than a 404 from the sidecar.
    """
    from dapr.actor import ActorId, ActorProxy  # noqa: PLC0415 - deliberate, see docstring

    proxy = ActorProxy.create("AnnotationTaskActor", ActorId(task_id), AnnotationTaskActorInterface)
    return cast(AnnotationTaskActorInterface, proxy)


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

    # Rules the table cannot express, because they depend on the task's own rows.
    if payload.event in SELF_REVIEW_FORBIDDEN and current.get("submitted_by") == subject:
        audit(f"task.{payload.event}", FAILURE, subject=subject, resource=task_id, reason="self_review")
        raise ForbiddenError("a reviewer may not review their own submission")
    if payload.event in {"save_draft", "submit"} and current.get("assignee") not in (None, subject):
        raise ForbiddenError(f"task {task_id} is held by {current['assignee']}")

    try:
        updated = await actor.fire({"event": payload.event, "actor": subject, "lease_seconds": payload.lease_seconds})
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
                "base_revision": payload.base_revision,
                "origin": payload.origin,
            }
        )
    except IllegalTransition as exc:
        raise ConflictError(str(exc)) from exc

    audit("task.save_draft", SUCCESS, subject=subject, resource=task_id)
    return draft


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
